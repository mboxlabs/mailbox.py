from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Union
import asyncio
import inspect
import uuid

from .delivery import (
    CANCELLED,
    DEFAULT_DELIVERY_SETTLE_TIMEOUT,
    DeliveryResult,
    FATAL,
    RETRIABLE,
    delivery_key,
    should_requeue_after_broadcast,
    subscription_id_of_delivery,
    to_delivery_results,
)
from .message import MailMessage, MailboxStatus, FetchOptions
from .retry import AttemptLedger, RetryPolicy, is_retriable_error, next_retry_delay, resolve_retry_policy

# Type alias for the receive callback. 返回值可以是 None（同步订阅者）或 awaitable。
OnReceiveCallback = Callable[[MailMessage], Any]

# 注册给具体 provider 的订阅监听器：返回本次投递的**最终结算**（含整条重试链）。
DeliveryListener = Callable[[MailMessage], Awaitable[DeliveryResult]]


class Subscription(ABC):
    """
    Represents an active subscription.
    """
    id: str
    address: str

    @property
    @abstractmethod
    def status(self) -> str:
        """'active' | 'closed'"""
        raise NotImplementedError

    @abstractmethod
    async def unsubscribe(self) -> None:
        pass


class AckableMessage:
    """
    Represents a message that can be manually acknowledged or rejected.
    """
    def __init__(
        self,
        message: MailMessage,
        ack_fn: Callable[[], Awaitable[None]],
        nack_fn: Callable[[bool], Awaitable[None]]
    ):
        self.message = message
        self._ack_fn = ack_fn
        self._nack_fn = nack_fn

    async def ack(self) -> None:
        await self._ack_fn()

    async def nack(self, requeue: bool = False) -> None:
        await self._nack_fn(requeue)


class _PendingDelivery:
    """
    一次尚未结算的投递（某个订阅者对某条消息的处理，可能正卡在重试退避里）。
    只有结算之后，广播的发送侧才知道这次投递是成功还是失败。
    """

    __slots__ = ("message", "delivery_id", "subscription_id", "settlement", "timer", "watchdog")

    def __init__(self, message: MailMessage, delivery_id: str, subscription_id: str,
                 settlement: "asyncio.Future"):
        self.message = message
        self.delivery_id = delivery_id
        # 投递身份（`subscription_id + message_id`）。内部 map 键可能带并发序号后缀。
        self.subscription_id = subscription_id
        self.settlement = settlement  # resolve 成 DeliveryResult，永不抛异常
        self.timer: Optional[asyncio.TimerHandle] = None  # 重试退避定时器
        self.watchdog: Optional[asyncio.TimerHandle] = None  # 单次尝试的结算看门狗


class MailboxProvider(ABC):
    """
    Abstract base class for mailbox providers.
    """
    #: 这个 provider 的 `subscribe` 是不是广播（一条消息投递给 N 个订阅者）。
    #:
    #: 广播型（`True`，如 MemoryProvider）：单次投递失败**不再**自动 `nack(requeue)` 整条消息 ——
    #: 已成功订阅者的副作用不该再执行一次。回不回队由发送侧的结算 barrier 按
    #: `should_requeue_after_broadcast()` 统一判定（全部投递都失败才回队）。
    #:
    #: 队列 / 传输型（`False`，默认）：每次投递对应 broker 里的一份 ack，保持原有
    #: per-delivery `nack` 行为，交给传输层重投或死信。
    broadcast_deliveries: bool = False

    def __init__(self, protocol: str, retry: Optional[RetryPolicy] = None):
        if not protocol:
            raise ValueError("Provider protocol cannot be empty.")
        self.protocol = protocol
        self._subscriptions: Dict[str, Any] = {}  # sub_id -> {id, address, handle, on_receive}

        # 订阅失败后的重试策略。`None` 表示不重试（默认）：可重试失败只把消息退回队列，
        # 由 `fetch()` 兜底。重试是 opt-in 的，因为 AMQP / SQS 这类传输自带重投与死信，
        # 库再重投就是双重投递。
        self.retry: Optional[RetryPolicy] = resolve_retry_policy(retry)
        # 按「订阅者 + 消息」记账尝试次数；订阅是广播，不能混在一起算。
        self._attempts = AttemptLedger()
        # 按投递身份记账尚未结算的投递。
        self._pending_deliveries: Dict[str, _PendingDelivery] = {}

        #: 单次投递尝试的结算超时（秒）。`0` / 负数关闭（调用方自己负责长任务）。
        #: 见 `delivery.DEFAULT_DELIVERY_SETTLE_TIMEOUT`。
        self.delivery_settle_timeout: float = DEFAULT_DELIVERY_SETTLE_TIMEOUT

        # 已登记结算 barrier 的消息 id → 登记层数（同 id 并发广播时不能互相抹掉）。
        # 广播型 provider 若没登记 barrier，基类会兜底回队，不让可重试失败静默消失。
        self._broadcast_barriers: Dict[str, int] = {}
        # 已经告过警的「漏接 barrier」消息 id，避免一条消息警 N 次。
        self._barrierless_warned: set = set()
        # 后台任务需要强引用，否则事件循环只持弱引用，任务可能被 GC 掉。
        self._background_tasks: set = set()

    def set_retry_policy(self, policy: Optional[RetryPolicy]) -> None:
        """开启 / 关闭订阅重试。传 `None` 回到默认行为（失败只回队、不自动重投）。"""
        self.retry = resolve_retry_policy(policy)

    async def init(self) -> None:
        """
        Initializes the provider. Override this in subclasses to establish connections.
        """
        pass

    async def close(self) -> None:
        """
        Closes the provider and releases resources.
        This method automatically cancels all active subscriptions.

        注意：`close()` **不**清消息队列——“关掉通道”不等于“销毁在途数据”。要一并清掉积压消息
        用 `Mailbox.stop(purge=True)` 或 `MailboxProvider.clear()`。
        """
        # 1. Clean up all active subscriptions
        if self._subscriptions:
            tasks = []
            # We need to capture IDs to map results back to errors if needed
            sub_ids = list(self._subscriptions.keys())
            for sub_id in sub_ids:
                info = self._subscriptions.get(sub_id)
                if info:
                    tasks.append(self._unsubscribe(sub_id, info['handle']))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, res in enumerate(results):
                    if isinstance(res, Exception):
                        sub_id = sub_ids[i]
                        print(f"[{self.protocol}] Error unsubscribing {sub_id} during close: {res}")

            self._subscriptions.clear()

        # 关闭时取消所有「等待重投」的投递：进程都在收尾，不再把消息重新喂回回调。
        # 取消不等于消息可以消失 —— 见 `_cancel_pending_deliveries`。
        # 正在执行的回调不替它下结论（它可能马上就要成功）：由它自己结算，卡死的由看门狗兜。
        for key in list(self._pending_deliveries.keys()):
            self._cancel_pending_deliveries(subscription_id_of_delivery(key))
        self._attempts.clear()

        # 2. Specific closing logic
        await self._close()

    async def _close(self) -> None:
        """
        Subclass responsibility: Implement specific closing logic here.
        """
        pass

    async def clear(self) -> None:
        """
        丢弃本 provider 持有的积压消息（含 in-flight），不动订阅。

        默认**空实现**：“清空传输”对 AMQP / SQS / IMAP 这类真实 broker 是破坏性运维操作，
        消息也不在本地，基类不能替子类下结论。需要 purge 语义的 provider（如
        `MemoryProvider`）自己覆写。
        """
        # 默认无可清理内容：传输型 provider 的消息存在 broker 里，不在本地。

    def generate_id(self) -> str:
        return str(uuid.uuid4())

    async def send(self, message: MailMessage) -> MailMessage:
        """
        Public send method. Injects headers and delegates to _send.
        """
        # Always inject mbx-sent-at for outgoing messages（不改调用方传进来的对象）
        message_to_send = replace(
            message,
            headers={**message.headers, 'mbx-sent-at': datetime.now(timezone.utc).isoformat()},
        )

        await self._send(message_to_send)
        return message_to_send

    async def subscribe(self, address: str, on_receive: OnReceiveCallback) -> Subscription:
        """
        Public subscribe method. 投递的隐式 ACK、错误处理与重试都由基类驱动，
        然后委托给具体实现的 `_subscribe`。

        注册给具体 provider 的监听器返回一个在**投递最终结算**（含整条重试链）时才 resolve 的
        future（`DeliveryResult`）。广播型 provider 的发送侧靠它做结算判定。
        """
        sub_id = self.generate_id()

        # Call concrete implementation：监听器返回这次投递的最终结算。
        unsubscribe_handle = await self._subscribe(
            address, lambda message: self._start_delivery(sub_id, message)
        )

        # Store subscription info
        self._subscriptions[sub_id] = {
            'id': sub_id,
            'address': address,
            'handle': unsubscribe_handle,
            # 失败处理与重试由基类驱动（见 `_run_delivery`），
            # 所以这里保存用户传入的**原始**回调，不保存「包装后的回调」。
            'on_receive': on_receive,
        }

        return self._create_subscription_object(sub_id)

    async def fetch(self, address: str, options: FetchOptions) -> Union[MailMessage, AckableMessage, None]:
        """
        Public fetch method. Delegates to _fetch.
        """
        return await self._fetch(address, options)

    async def status(self, address: str) -> MailboxStatus:
        """
        Public status method. Delegates to _status or returns unknown.
        """
        return await self._status(address)

    async def ack(self, message: MailMessage) -> None:
        await self._ack(message)

    async def nack(self, message: MailMessage, requeue: bool) -> None:
        await self._nack(message, requeue)

    # --- Abstract Methods ---

    @abstractmethod
    async def _send(self, message: MailMessage) -> None:
        pass

    @abstractmethod
    async def _subscribe(self, address: str, on_receive: DeliveryListener) -> Any:
        """
        Returns an unsubscribe handle (opaque to the base class).

        :param on_receive: 基类包装后的投递入口：返回本次投递的最终结算结果。
        """
        pass

    @abstractmethod
    async def _unsubscribe(self, subscription_id: str, unsubscribe_handle: Any) -> None:
        pass

    @abstractmethod
    async def _fetch(self, address: str, options: FetchOptions) -> Union[MailMessage, AckableMessage, None]:
        pass

    # --- Optional / Helper Methods ---

    async def _status(self, address: str) -> MailboxStatus:
        """
        Subclass responsibility (optional): Performs the actual status query logic.
        未实现时返回 `unknown`，与 TS 端一致。
        """
        return MailboxStatus(
            state="unknown",
            extra={"message": f"The '{self.protocol}' provider does not support status queries."},
        )

    async def _ack(self, message: MailMessage) -> None:
        """Optional: Implement if the provider supports explicit ACK in subscribe mode."""
        pass

    async def _nack(self, message: MailMessage, requeue: bool) -> None:
        """
        Optional: Implement if the provider supports NACK.

        广播型 provider 里它只会在**广播全部投递都结算之后**被调用一次（由发送侧的结算判定决定
        `requeue` 与否），不会再在每个订阅者失败时各回队一次。
        """
        pass

    # --- 投递记账（基类内部） ---

    def _deliver_to_local_subscription(self, subscription_id: str, message: MailMessage) -> Awaitable[DeliveryResult]:
        """
        把一条消息投递给本 provider 的某个**本地订阅者**，走与 `subscribe` 完全相同的投递记账
        （隐式 ack、错误处理、重试、结算）。生产者型 provider（自己组装消息再分发）必须走这里，
        不要直接调用户回调 —— 那样这次投递没人记账，失败也不会回队。
        """
        return self._start_delivery(subscription_id, message)

    def _start_delivery(self, subscription_id: str, message: MailMessage) -> "asyncio.Future":
        """
        投递入口：注册一个待结算投递，然后跑第一次尝试。
        返回的 future 在投递最终结算时 resolve（永不抛异常）—— 广播的 barrier 等的就是它。

        内部记账键由 `_reserve_delivery_key` 分配：`post()` 允许自定义消息 id，同一个订阅者对
        同 id 的并发投递如果共用一个键，后者会覆盖前者的结算入口 —— 那条投递永不结算，
        对应的 barrier 永不落定，消息静默消失。
        """
        key = self._reserve_delivery_key(subscription_id, message.id)
        settlement = self._require_loop().create_future()

        self._pending_deliveries[key] = _PendingDelivery(
            message=message,
            delivery_id=delivery_key(subscription_id, message.id),
            subscription_id=subscription_id,
            settlement=settlement,
        )
        self._run_delivery(subscription_id, message, key)

        return settlement

    def _reserve_delivery_key(self, subscription_id: str, message_id: str) -> str:
        """内部记账键：同 `(subscription_id, message_id)` 的并发投递各自独立。"""
        base = delivery_key(subscription_id, message_id)
        if base not in self._pending_deliveries:
            return base
        suffix = 1
        while True:
            candidate = "{}\x00{}".format(base, suffix)
            if candidate not in self._pending_deliveries:
                return candidate
            suffix += 1

    def _run_delivery(self, subscription_id: str, message: MailMessage, key: str) -> None:
        """
        跑一次尝试。成功 → 隐式 ack + 结算 `ok`；抛错 → 交给 `_handle_delivery_error`。
        """
        info = self._subscriptions.get(subscription_id)
        if info is None:
            # 等待期间被退订 / provider 已关闭：不再调用回调，按取消结算。
            self._settle_and_fallback(key, CANCELLED)
            return

        # 看门狗只给「一次尝试」计时：卡死的回调会拖住整条广播的结算判定（消息既不回队也不死信）。
        self._arm_delivery_watchdog(key)

        on_receive = info['on_receive']
        try:
            attempt = on_receive(message)
        except Exception as error:
            self._handle_delivery_error(subscription_id, message, key, error)
            return

        self._fire_and_forget(self._complete_attempt(subscription_id, message, key, attempt))

    async def _complete_attempt(self, subscription_id: str, message: MailMessage,
                                key: str, attempt: Any) -> None:
        """等订阅者的尝试跑完（同步订阅者直接算完成），然后隐式 ack + 结算。"""
        try:
            if inspect.isawaitable(attempt):
                await attempt
            # On successful execution, implicitly acknowledge the message.
            await self.ack(message)
            self._attempts.reset(subscription_id, message.id)
            self._settle_delivery(key, "ok")
        except asyncio.CancelledError:
            # 尝试任务被取消（例如事件循环收尾）：不替消息下结论，交给兜底路径。
            self._abandon_delivery(key, CANCELLED)
            raise
        except Exception as error:
            self._handle_delivery_error(subscription_id, message, key, error)

    def _handle_delivery_error(self, subscription_id: str, message: MailMessage,
                               key: str, error: BaseException) -> None:
        """
        订阅回调抛错时的统一处理（投递级）。

        默认（`retry is None`）：可重试 → 结算 `retriable`；不可重试 → 结算 `fatal`（死信）。
        开启重试后：可重试且未超上限 → 只重投给失败的那个订阅者（不广播给其他已成功的订阅者，
        否则它们的副作用会被执行两次）；超上限 → 清零台账、调 `_on_retry_exhausted`，再结算 `retriable`。

        广播型 provider 不在这里 `nack`：整条消息回不回队由发送侧在所有投递结算后统一判定
        （见 `should_requeue_after_broadcast`）。没接 barrier 的广播 provider 由
        `_fallback_for_missing_barrier` 兜底（整条回队 + 告警），否则可重试失败就静默消失了。
        非广播型 provider 每次投递对应 broker 的一次 ack，保持原有 per-delivery `nack(requeue=True)`。
        """
        print(f"[{self.protocol}] Error processing message {message.id}: {error}")

        is_retriable = self._is_retriable(error)

        if is_retriable and self.retry is not None:
            attempt = self._attempts.record(subscription_id, message.id)

            if attempt < self.retry.max_attempts and subscription_id in self._subscriptions:
                delay = next_retry_delay(self.retry, attempt)
                pending = self._pending_deliveries.get(key)
                if pending is None:
                    return  # 已经结算（例如退订），不重投
                timer = self._require_loop().call_later(
                    delay, lambda: self._fire_retry(subscription_id, message, key)
                )
                pending.timer = timer
                # 退避等待不是“卡死的尝试”：看门狗只在尝试进行中计时，退避期间关掉，
                # 否则一个比看门狗还长的退避会被自己掐断。
                self._clear_delivery_watchdog(pending)
                return

            if attempt >= self.retry.max_attempts:
                self._attempts.reset(subscription_id, message.id)
                self._on_retry_exhausted(message, attempt)

        if is_retriable and not self.broadcast_deliveries:
            # Crucial: NACK the message first to prevent reprocessing, then handle other tasks.
            self._fire_and_forget(self._nack_with_guard(message, True))

        self._settle_and_fallback(key, RETRIABLE if is_retriable else FATAL)

    def _fire_retry(self, subscription_id: str, message: MailMessage, key: str) -> None:
        """退避到期：重投给失败的那个订阅者。"""
        pending = self._pending_deliveries.get(key)
        if pending is None:
            return  # 退避期间已结算（退订 / 关闭）
        pending.timer = None
        if subscription_id not in self._subscriptions:
            # 等待期间退订了：不重投给已消失的订阅者，按取消结算（消息由兜底路径接手）。
            self._settle_and_fallback(key, CANCELLED)
            return
        self._run_delivery(subscription_id, message, key)

    def _on_retry_exhausted(self, message: MailMessage, attempts: int) -> None:
        """可选钩子：重试次数耗尽（例如写死信队列 / 告警）。
        若这次广播没有任何订阅者成功，消息随后仍会退回队列，由 `fetch()` 兜底。"""
        pass

    def _on_delivery_deadlettered(self, message: MailMessage, failed: List[DeliveryResult]) -> None:
        """
        可选死信钩子：广播已结算，有投递失败但消息**不**回队时用
        （即“部分失败”或“全部不可重试”）。不回队是为了不把已成功者处理过的消息再投一次，
        代价是失败的投递从此隐形 —— 这个钩子就是补上那个可观测性缺口（写死信队列 / 告警）。
        """
        pass

    def _track_broadcast_settlement(self, message: MailMessage,
                                    deliveries: Iterable[Any]) -> None:
        """
        广播的结算 barrier：把 `publish` 返回的每次投递绑成一个后台任务，全部投递
        （含重试链）落定后统一判定要不要 `nack(requeue)`。给广播型 provider 的 `_send` 用。

        不阻塞 `post()`：`post()` 在派发完成时就 resolve，判定在投递结算后才发生。
        “按成功订阅者数 > 0 跳过回队”在 fire-and-forget 广播上会误判 ——
        先失败的订阅者可能比慢而成功的订阅者先结算。
        """
        # 登记 barrier：基类靠它区分「有人在判定」和「provider 漏接了 barrier」。
        # 必须在 `_send` 里同步登记（任何 await 之前），否则同步失败的投递会先看到“无 barrier”。
        self._register_broadcast_barrier(message.id)

        self._fire_and_forget(self._settle_broadcast(message, list(deliveries)))

    async def _settle_broadcast(self, message: MailMessage, deliveries: List[Any]) -> None:
        try:
            settled = await asyncio.gather(
                *(self._as_awaitable(delivery) for delivery in deliveries),
                return_exceptions=True,
            )
            results = to_delivery_results(settled)
            requeue = should_requeue_after_broadcast(results)
            if not requeue:
                failed = [result for result in results if not result.ok]
                if failed:
                    self._on_delivery_deadlettered(message, failed)
            await self.nack(message, requeue)
        except Exception as error:
            print(f"[{self.protocol}] CRITICAL: Failed to settle a broadcast. {error}")
        finally:
            self._release_broadcast_barrier(message.id)

    @staticmethod
    def _as_awaitable(value: Any) -> Any:
        """第三方监听器可能返回非 awaitable 的值；归一成 awaitable 以便 gather。"""
        if inspect.isawaitable(value):
            return value

        async def _already_settled(result: Any) -> Any:
            return result

        return _already_settled(value)

    def _register_broadcast_barrier(self, message_id: str) -> None:
        self._broadcast_barriers[message_id] = self._broadcast_barriers.get(message_id, 0) + 1

    def _release_broadcast_barrier(self, message_id: str) -> None:
        depth = self._broadcast_barriers.get(message_id, 0) - 1
        if depth > 0:
            self._broadcast_barriers[message_id] = depth
        else:
            self._broadcast_barriers.pop(message_id, None)

    def _barrier_depth(self, message_id: str) -> int:
        """这条消息当前有没有正在判定的 barrier（同 id 并发广播时按层数计）。"""
        return self._broadcast_barriers.get(message_id, 0)

    def _fallback_for_missing_barrier(self, message: MailMessage, result: DeliveryResult) -> None:
        """
        广播型 provider 的兜底：投递已结算，但这条消息没有 barrier —— 说明 `_send` 漏接了
        `_track_broadcast_settlement`，没人做回队判定。宁可整条回队让 `fetch()` 兜底
        （“不得静默丢失”），也不能让消息消失；同时告警提示 provider 作者。

        推迟一个事件循环 tick 再判定：`publish` 里同步抛错的投递跑在 `_send` 登记 barrier 之前。
        """
        if result.ok or self._barrier_depth(message.id) > 0:
            return

        try:
            loop = self._require_loop()
        except RuntimeError:
            return
        loop.call_soon(lambda: self._fallback_after_tick(message, result))

    def _fallback_after_tick(self, message: MailMessage, result: DeliveryResult) -> None:
        if self._barrier_depth(message.id) > 0:
            return  # barrier 已登记（同一轮内），判定交给它
        self._warn_missing_barrier(message.id)
        if result.reason == FATAL:
            self._on_delivery_deadlettered(message, [result])
            return
        self._fire_and_forget(self._nack_with_guard(message, True))

    def _warn_missing_barrier(self, message_id: str) -> None:
        if message_id in self._barrierless_warned:
            return
        if len(self._barrierless_warned) > 500:
            self._barrierless_warned.clear()  # 不让告警名单无限增长
        self._barrierless_warned.add(message_id)
        print(
            f"[{self.protocol}] A broadcast delivery of message {message_id} settled without a settlement barrier. "
            "A broadcast provider must call `_track_broadcast_settlement(message, deliveries)` in `_send` "
            "(synchronously, before any await) or retriable failures will fall back to whole-message requeue."
        )

    def _settle_and_fallback(self, key: str, outcome: str) -> None:
        """
        结算一条投递；广播型 provider 若这条消息没接 barrier，走兜底。
        """
        pending = self._pending_deliveries.get(key)
        if pending is None:
            return  # 已结算过
        message = pending.message
        result = self._settle_delivery(key, outcome)
        if result is not None and self.broadcast_deliveries:
            self._fallback_for_missing_barrier(message, result)

    def _abandon_delivery(self, key: str, outcome: str = CANCELLED) -> None:
        """
        放弃一条投递（退避中退订 / 看门狗超时）：按取消结算，消息交给兜底路径。
        """
        pending = self._pending_deliveries.get(key)
        if pending is None:
            return
        result = self._settle_delivery(key, outcome)
        if result is None:
            return
        if self.broadcast_deliveries:
            self._fallback_for_missing_barrier(pending.message, result)
            return
        # 非广播（传输型）：每次投递对应 broker 的一次 ack，交回传输层重投。
        self._fire_and_forget(self._nack_with_guard(pending.message, True))

    # --- 结算看门狗 ---

    def _arm_delivery_watchdog(self, key: str) -> None:
        """
        给当前这次尝试装一个结算看门狗。只在尝试开始时计时，退避等待期间不计时，
        所以不会掐断健康的重试链。
        """
        pending = self._pending_deliveries.get(key)
        if pending is None:
            return
        self._clear_delivery_watchdog(pending)
        timeout = self.delivery_settle_timeout
        if not timeout or timeout <= 0:
            return  # 显式关闭：长任务由调用方自己负责
        pending.watchdog = self._require_loop().call_later(timeout, self._on_delivery_stall, key)

    def _clear_delivery_watchdog(self, pending: _PendingDelivery) -> None:
        if pending.watchdog is not None:
            pending.watchdog.cancel()
            pending.watchdog = None

    def _on_delivery_stall(self, key: str) -> None:
        """
        看门狗到期：回调还在跑（卡死 / 超长）。按取消结算，消息回到兜底路径；
        回调事后成功不会重复结算（见 `_settle_delivery` 的一次性守卫）。
        """
        pending = self._pending_deliveries.get(key)
        if pending is None:
            return
        pending.watchdog = None
        print(
            f"[{self.protocol}] Delivery {pending.delivery_id} did not settle within "
            f"{self.delivery_settle_timeout}s (subscriber callback still running). Settling it as cancelled "
            "so the message can be requeued; raise `delivery_settle_timeout` (0 disables) if this delivery "
            "is expected to take longer."
        )
        self._abandon_delivery(key, CANCELLED)

    def _settle_delivery(self, key: str, outcome: str) -> Optional[DeliveryResult]:
        """
        结算一条投递（幂等：已结算过则直接返回 `None`，不会重复投）。
        """
        pending = self._pending_deliveries.pop(key, None)
        if pending is None:
            return None  # 已结算过（例如退订时按 cancelled 结算）
        if pending.timer is not None:
            pending.timer.cancel()
        self._clear_delivery_watchdog(pending)

        result = DeliveryResult(
            delivery_id=pending.delivery_id,
            subscription_id=pending.subscription_id,
            message_id=pending.message.id,
            ok=outcome == "ok",
            reason=None if outcome == "ok" else outcome,
        )
        if not pending.settlement.done():
            pending.settlement.set_result(result)
        return result

    def _cancel_pending_deliveries(self, subscription_id: str) -> None:
        """
        取消某个订阅者尚未结算的投递（退订 / 关闭）。

        只取消「等待重投」的投递（有退避定时器）。正在执行的回调不替它下结论：
        提前结算成 `cancelled` 会被 barrier 误判为“无人成功”→ 整条消息回队 →
        已经执行过的副作用被 `fetch()` 消费者再跑一次（重复投递，换了个入口）。
        卡死的回调由看门狗兜底。
        """
        prefix = delivery_key(subscription_id, "")
        for key in list(self._pending_deliveries.keys()):
            if not key.startswith(prefix):
                continue
            pending = self._pending_deliveries.get(key)
            if pending is None or pending.timer is None:
                continue  # 正在跑的回调：由它自己结算
            pending.timer.cancel()
            pending.timer = None
            self._abandon_delivery(key, CANCELLED)

    @property
    def pending_delivery_count(self) -> int:
        """当前尚未结算的投递数量（诊断 / 测试用）。"""
        return len(self._pending_deliveries)

    def _is_retriable(self, error: BaseException) -> bool:
        """判定错误是否可重试（实现在 `retry.is_retriable_error`，三端共用同一判定）。"""
        return is_retriable_error(error)

    async def _nack_with_guard(self, message: MailMessage, requeue: bool) -> None:
        try:
            await self.nack(message, requeue)
        except Exception as nack_error:
            print(f"[{self.protocol}] CRITICAL: Failed to NACK message {message.id}. "
                  f"Risk of message duplication. {nack_error}")

    def _fire_and_forget(self, coro: Any) -> Optional[asyncio.Task]:
        """跑后台任务并持强引用（事件循环只持弱引用，否则任务可能被 GC 掉）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 没有运行中的事件循环（例如同步的关闭路径）
            if inspect.iscoroutine(coro):
                coro.close()
            return None
        task = loop.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        try:
            return asyncio.get_running_loop()
        except RuntimeError as exc:  # pragma: no cover - 误用保护
            raise RuntimeError(
                f"[{self.protocol}] delivery bookkeeping requires a running event loop"
            ) from exc

    def _create_subscription_object(self, sub_id: str) -> Subscription:
        provider = self

        class ConcreteSubscription(Subscription):
            def __init__(self):
                info = provider._subscriptions.get(sub_id) or {}
                self.id = sub_id
                self.address = info.get('address')

            @property
            def status(self) -> str:
                return 'active' if sub_id in provider._subscriptions else 'closed'

            async def unsubscribe(self) -> None:
                if sub_id in provider._subscriptions:
                    info = provider._subscriptions[sub_id]
                    del provider._subscriptions[sub_id]
                    provider._cancel_pending_deliveries(sub_id)
                    await provider._unsubscribe(sub_id, info['handle'])

        return ConcreteSubscription()
