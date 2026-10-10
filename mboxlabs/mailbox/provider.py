from abc import ABC, abstractmethod
from typing import Any, Callable, Awaitable, Optional, Union, Dict
from datetime import datetime, timezone
import asyncio
import uuid

from .message import MailMessage, MailboxStatus, FetchOptions
from .error import MailboxError
from .retry import AttemptLedger, RetryPolicy, is_retriable_error, next_retry_delay, resolve_retry_policy

# Type alias for the receive callback
OnReceiveCallback = Callable[[MailMessage], Awaitable[None]]

class Subscription(ABC):
    """
    Represents an active subscription.
    """
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

class MailboxProvider(ABC):
    """
    Abstract base class for mailbox providers.
    """
    def __init__(self, protocol: str, retry: Optional[RetryPolicy] = None):
        self.protocol = protocol
        self._subscriptions: Dict[str, Any] = {} # Map sub_id to internal handle/info

        # 订阅失败后的重试策略。`None` 表示不重试（默认）：可重试失败只把消息退回队列，
        # 由 `fetch()` 兜底。重试是 opt-in 的，因为 AMQP / SQS 这类传输自带重投与死信，
        # 库再重投就是双重投递。
        self.retry: Optional[RetryPolicy] = resolve_retry_policy(retry)
        # 按「订阅者 + 消息」记账尝试次数；订阅是广播，不能混在一起算。
        self._attempts = AttemptLedger()
        # sub_id -> { timer_handle: message }，用于退订 / 关闭时取消待重投。
        self._pending_retries: Dict[str, Dict[Any, MailMessage]] = {}

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

        # 关闭时取消所有待重投：进程都在收尾，不再把消息重新喂回回调。
        for sub_id in list(self._pending_retries.keys()):
            self._cancel_pending_retries(sub_id)
        self._attempts.clear()

        # 2. Specific closing logic
        await self._close()

    async def _close(self) -> None:
        """
        Subclass responsibility: Implement specific closing logic here.
        """
        pass

    def generate_id(self) -> str:
        return str(uuid.uuid4())

    async def send(self, message: MailMessage) -> MailMessage:
        """
        Public send method. Injects headers and delegates to _send.
        """
        # Inject mbx-sent-at
        if 'mbx-sent-at' not in message.headers:
            message.headers['mbx-sent-at'] = datetime.now(timezone.utc).isoformat()

        await self._send(message)
        return message

    async def subscribe(self, address: str, on_receive: OnReceiveCallback) -> Subscription:
        """
        Public subscribe method. Wraps callback for implicit ACK and delegates to _subscribe.
        """
        sub_id = self.generate_id()

        async def wrapped_on_receive(message: MailMessage):
            try:
                await on_receive(message)
                # Implicit ACK
                await self.ack(message)
                self._attempts.reset(sub_id, message.id)
            except Exception as e:
                await self._handle_receive_error(e, message, sub_id)

        # Call concrete implementation
        unsubscribe_handle = await self._subscribe(address, wrapped_on_receive)

        # Store subscription info
        self._subscriptions[sub_id] = {
            'id': sub_id,
            'address': address,
            'handle': unsubscribe_handle,
            # 基类重投时要调的就是这个包装后的回调（会再次 ack/nack），
            # 而不是用户原始回调 —— 否则重试绕过了错误处理。
            'wrapped_on_receive': wrapped_on_receive,
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
    async def _subscribe(self, address: str, on_receive: OnReceiveCallback) -> Any:
        """
        Returns an unsubscribe handle (opaque to the base class).
        """
        pass

    @abstractmethod
    async def _unsubscribe(self, subscription_id: str, unsubscribe_handle: Any) -> None:
        pass

    @abstractmethod
    async def _fetch(self, address: str, options: FetchOptions) -> Union[MailMessage, AckableMessage, None]:
        pass

    @abstractmethod
    async def _status(self, address: str) -> MailboxStatus:
        pass

    # --- Optional / Helper Methods ---

    async def _ack(self, message: MailMessage) -> None:
        """Optional: Implement if the provider supports explicit ACK in subscribe mode."""
        pass

    async def _nack(self, message: MailMessage, requeue: bool) -> None:
        """Optional: Implement if the provider supports NACK."""
        pass

    async def _handle_receive_error(self, error: Exception, message: MailMessage,
                                    subscription_id: Optional[str] = None) -> None:
        """
        订阅回调抛错时的统一处理。

        默认（`retry is None`）：可重试 → `nack(requeue=True)` 退回队列；不可重试 → 丢弃。
        开启重试后：可重试且未超上限 → 只重投给失败的那个订阅者（不广播给其他已成功的订阅者，
        否则它们的副作用会被执行两次）；超上限 → 清零台账、调 `_on_retry_exhausted`，
        再退回队列交给 `fetch()` 兜底。
        """
        print(f"[{self.protocol}] Error processing message {message.id}: {error}")

        is_retriable = self._is_retriable(error)
        info = self._subscriptions.get(subscription_id) if subscription_id else None

        if is_retriable and self.retry is not None and info is not None:
            attempt = self._attempts.record(subscription_id, message.id)

            if attempt < self.retry.max_attempts and subscription_id in self._subscriptions:
                self._schedule_retry(subscription_id, info['wrapped_on_receive'], message,
                                     next_retry_delay(self.retry, attempt))
                return

            if attempt >= self.retry.max_attempts:
                self._attempts.reset(subscription_id, message.id)
                self._on_retry_exhausted(message, attempt)

        # 与 TS 端一致：可重试错误 NACK(requeue=True)，让支持回队的 Provider
        # 把消息退回队列；非可重试错误直接当死信丢弃，避免无限重试同一条坏消息。
        await self.nack(message, requeue=is_retriable)

    def _schedule_retry(self, subscription_id: str, wrapped_on_receive, message: MailMessage,
                        delay: float) -> None:
        """定时重投给失败的那个订阅者（不重新广播）。"""
        pending = self._pending_retries.setdefault(subscription_id, {})

        def fire():
            pending.pop(handle, None)
            if subscription_id not in self._subscriptions:
                # 等待期间退订了：不重投给已消失的订阅者，退回队列而不是静默丢掉。
                self._fire_and_forget(self.nack(message, True))
                return
            self._fire_and_forget(wrapped_on_receive(message))

        handle = asyncio.get_running_loop().call_later(delay, fire)
        pending[handle] = message

    @staticmethod
    def _fire_and_forget(coro) -> None:
        try:
            asyncio.ensure_future(coro)
        except RuntimeError:  # 没有运行中的事件循环（例如同步的关闭路径）
            coro.close()

    def _on_retry_exhausted(self, message: MailMessage, attempts: int) -> None:
        """可选钩子：重试次数耗尽（例如写死信队列 / 告警）。
        调用后消息仍会退回队列，由 `fetch()` 兜底。"""
        pass

    def _cancel_pending_retries(self, subscription_id: str) -> None:
        """
        取消某个订阅者的待重投。

        取消定时器不等于消息可以消失：每一条被取消的重投都退回队列，由 `fetch()` 兜底。
        """
        pending = self._pending_retries.pop(subscription_id, None)
        if not pending:
            return
        for handle, message in pending.items():
            handle.cancel()
            self._fire_and_forget(self.nack(message, True))

    @staticmethod
    def _is_retriable(error: Exception) -> bool:
        """判定错误是否可重试（实现在 `retry.is_retriable_error`，三端共用同一判定）。"""
        return is_retriable_error(error)

    def _create_subscription_object(self, sub_id: str) -> Subscription:
        provider = self

        class ConcreteSubscription(Subscription):
            async def unsubscribe(self) -> None:
                if sub_id in provider._subscriptions:
                    info = provider._subscriptions[sub_id]
                    provider._cancel_pending_retries(sub_id)
                    await provider._unsubscribe(sub_id, info['handle'])
                    del provider._subscriptions[sub_id]

        return ConcreteSubscription()
