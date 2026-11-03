from typing import Dict, List, Any, Optional, Union, Callable
from datetime import datetime, timezone

from ..provider import MailboxProvider, AckableMessage, DeliveryListener
from ..message import MailMessage, MailboxStatus, FetchOptions
from ..retry import RetryPolicy
from ..utils import get_canonical_mailbox_address_identifier
from .queue import MailMessageQueue

#: 默认 scope 的名字（即“进程级共享”那一份 bus）。
DEFAULT_MEMORY_SCOPE = ""


async def _rethrow(error: BaseException):
    """把同步抛出的错误延后到被等待时再抛，交给 barrier 的 gather 归一处理。"""
    raise error


class MemoryEventBus:
    """
    模拟消息传递的内存总线（一个“服务端”）：每个实例持有一份 topic 队列 + 订阅表。

    实例边界（issue#3）：
    - `MemoryEventBus()` —— 私有总线，只属于持有它的 `MemoryProvider`。需要“每个 Mailbox
      一套互不相干的 `mem:` 状态”（宿主 + 插件、测试隔离）就用这个。
    - `MemoryEventBus.get_instance(scope=None)` —— 按 scope 共享的进程级单例。不带 `scope`
      就是历史上那个全局单例（保持现有“模拟服务端”用法不变）。

    注意：隔离靠“换 bus 实例”，不靠“换地址”——`mem:` 地址空间在同一份 bus 内是全局的，
    没有租户前缀，跨实例串台就是同一份 bus 上 topic 同名。
    """

    #: 按 scope 登记的总线；`DEFAULT_MEMORY_SCOPE` 就是历史全局单例。
    _registry: Dict[str, "MemoryEventBus"] = {}

    def __init__(self, scope: str = DEFAULT_MEMORY_SCOPE):
        """
        :param scope: 所属 scope。仅用于诊断（`status()` / 日志里能看出这份状态是谁的）。
        """
        self.scope = scope
        # 监听器 = 基类的投递入口：返回本次投递的最终结算（awaitable）
        self.topics: Dict[str, List[DeliveryListener]] = {}
        self.queue: MailMessageQueue[MailMessage] = MailMessageQueue()
        self.last_activity: Dict[str, str] = {}

    @classmethod
    def get_instance(cls, scope: str = DEFAULT_MEMORY_SCOPE) -> "MemoryEventBus":
        """
        取（或创建）某个 scope 的共享总线。不带参数 = 历史行为：进程级唯一单例。

        注册表不会自动回收：动态生成的 scope 名（带时间戳、每次新建）会一直占着那份状态。
        需要“用完就丢”的场景用私有总线（`MemoryProvider.create_isolated`）。

        :param scope: 命名空间标识；同 scope 拿到同一份队列与订阅表，不同 scope 互不可见。
        """
        # 注册表挂在 MemoryEventBus 上，不用 cls._registry：子类不该另开一份（也不该把自己
        # 塞进历史单例的位置）。
        bus = MemoryEventBus._registry.get(scope)
        if bus is None:
            bus = MemoryEventBus(scope)
            MemoryEventBus._registry[scope] = bus
        return bus

    def subscribe(self, topic: str, listener: DeliveryListener) -> Callable[[], None]:
        if topic not in self.topics:
            self.topics[topic] = []
        self.topics[topic].append(listener)
        self.last_activity[topic] = datetime.now(timezone.utc).isoformat()

        def unsubscribe():
            if topic in self.topics:
                try:
                    self.topics[topic].remove(listener)
                except ValueError:
                    pass
        return unsubscribe

    def publish(self, topic: str, message: MailMessage) -> Optional[List[Any]]:
        """
        发布一条消息：有订阅者时交给订阅回调，否则入队等待 fetch 取回。

        :return: 广播时返回每次投递的结算 awaitable（供发送侧做结算判定，见 `_send`）；
            入队时返回 `None`。注意广播仍是 fire-and-forget：这里不等待，`post()` 也不会
            等订阅回调跑完。
        """
        self.last_activity[topic] = datetime.now(timezone.utc).isoformat()

        # 交给订阅者
        listeners = self.topics.get(topic)
        if listeners:
            # 一条消息只走一条路：有订阅者就只走订阅。
            # 两条路都走时，订阅者从不 fetch → 队列只涨不消；退订后一次 fetch 又会把
            # 已处理的消息再投递一遍。一个 topic 的投递方式只能选一种。
            # 注意这里必须判空列表：退订后 topics[topic] 仍存在但是空列表。
            #
            # 每次投递的结算要留一份给发送侧：部分失败时「整条消息回不回队」
            # 必须等全部投递结算后才能判断，不能由先失败的那个订阅者说了算。
            deliveries = []
            for listener in listeners:
                try:
                    deliveries.append(listener(message))
                except Exception as error:  # 监听器同步抛错：归一成失败结算，不能打断 publish
                    deliveries.append(_rethrow(error))
            return deliveries

        # 入队，等待 fetch 取回
        self.queue.enqueue(topic, message)
        return None

    def fetch_and_forget(self, topic: str) -> Optional[MailMessage]:
        self.last_activity[topic] = datetime.now(timezone.utc).isoformat()
        return self.queue.dequeue(topic)

    def fetch_for_ack(self, topic: str, timeout: Optional[float]) -> Optional[MailMessage]:
        self.last_activity[topic] = datetime.now(timezone.utc).isoformat()
        return self.queue.dequeue_for_ack(topic, timeout, lambda m: m.id)

    def ack(self, message_id: str) -> None:
        self.queue.ack(message_id)

    def nack(self, message_id: str, requeue: bool) -> None:
        # Note: In MemoryProvider, nack usually refers to fetched messages.
        # Subscribed messages that fail are handled in provider.py's wrapped callback.
        # But here we just support the queue operation.
        self.queue.nack(message_id, requeue)

    def requeue(self, topic: str, message: MailMessage) -> None:
        """不经 in-flight 直接把消息放回队列头部。用于订阅者可重试失败。"""
        self.last_activity[topic] = datetime.now(timezone.utc).isoformat()
        self.queue.requeue(topic, message)

    def peek(self, topic: str, limit: Optional[float] = None) -> List[MailMessage]:
        """
        【只读】取一份队列快照（副本），不出队、不建 in-flight、不刷活动时间。

        :param topic: 要看的 topic。
        :param limit: 最多看几条；省略 = 整个队列。
        """
        # 注意：这里**不**写 last_activity。只读诊断不该把一个空闲地址刷成“刚刚有活动”，
        # 否则一个每隔几秒轮询一次的诊断页会让 last_activity_time 永远假绿。
        return self.queue.peek(topic, limit)

    def clear(self, topic: Optional[str] = None) -> int:
        """
        丢弃队列里的消息（含 in-flight）并抹掉对应的活动时间；不传 `topic` 则清掉全部。

        **不动订阅表**：订阅是消费者主动建立的意图，拆它的是 `unsubscribe()` / `close()`。
        在 `clear()` 里顺手拆订阅，会让 provider 侧的 `Subscription` 对象变成“报 active 但
        再也收不到消息”的僵尸。需要拆订阅就关掉持有它的 `Mailbox` / provider。

        :param topic: 只清这个 topic；省略则清空整份总线状态。
        :return: 被丢弃的消息数（含 in-flight）。
        """
        dropped = self.queue.clear(topic)
        if topic is None:
            self.last_activity.clear()
        else:
            self.last_activity.pop(topic, None)
        return dropped

    def knows(self, topic: str) -> bool:
        """
        这份总线认不认这个 topic：有过订阅、有过活动记录，或现在/曾有未结算的消息。

        这是 `status().exists` 的依据。“地址不存在”与“地址空闲但确实存在”在诊断上是两件事
        （issue#4）：前者 `exists=False` / `state='unknown'`，后者 `exists=True` / `state='online'`。
        """
        return (topic in self.topics) or (topic in self.last_activity) or self.queue.has_topic(topic)

    def list_topics(self) -> List[str]:
        """
        这份总线上所有“认得”的 topic（订阅过 / 有过活动 / 有消息），按字典序排序。
        供 `Mailbox.status()`（不传地址）做全量快照。
        """
        known = set(self.topics.keys()) | set(self.last_activity.keys()) | set(self.queue.topics())
        return sorted(known)

    def get_status(self, topic: str) -> Dict[str, Any]:
        """
        :return: `exists` 区分“不存在”与“空闲”；`last_activity_time` 没活动记录时是 `None`
            （不是“字段缺失”，否则 JSON 序列化后整个字段消失）。
        """
        queue_status = self.queue.get_status(topic)
        subscriber_count = len(self.topics.get(topic, []))
        return {
            'exists': self.knows(topic),
            'unread_count': queue_status['unread_count'],
            'in_flight_count': queue_status['in_flight_count'],
            'last_activity_time': self.last_activity.get(topic),
            'subscriber_count': subscriber_count
        }

class MemoryProvider(MailboxProvider):
    #: 订阅是广播：一条消息对每个订阅者各投递一次。失败处理因此走投递级记账，
    #: 整条消息回不回队由 `_send` 里的结算 barrier 决定。
    broadcast_deliveries = True

    def __init__(self, retry: Optional[RetryPolicy] = None, bus: Optional[MemoryEventBus] = None):
        """
        :param retry: 可选的订阅重试策略。不传则保持默认：订阅失败只把消息退回队列，不自动重投。
        :param bus: 可选的总线。不传则用进程级共享单例（向后兼容）；传 `MemoryEventBus()`
            得到实例级隔离，传 `MemoryEventBus.get_instance('scope')` 得到命名空间级隔离。
        """
        super().__init__("mem", retry)
        self.bus = bus if bus is not None else MemoryEventBus.get_instance()

    @classmethod
    def create_isolated(cls, retry: Optional[RetryPolicy] = None) -> "MemoryProvider":
        """
        造一个“自带私有总线”的 provider：同进程的其它 `mem:` 地址完全看不到它的队列与订阅。
        宿主 + 插件、kernel + provider 各持一个 `Mailbox`，以及测试隔离，都应该用这个。
        """
        return cls(retry, MemoryEventBus())

    @classmethod
    def for_scope(cls, scope: str, retry: Optional[RetryPolicy] = None) -> "MemoryProvider":
        """
        造一个挂在命名空间总线上的 provider：同 `scope` 共享一份状态，不同 `scope` 互不可见。
        “一个进程里跑多个互不相干的 `mem:` 世界”用它，不需要自己管总线对象。
        """
        return cls(retry, MemoryEventBus.get_instance(scope))

    async def clear(self) -> None:
        """
        丢弃这份总线上的所有消息（含 in-flight），订阅保持不变。

        代价要写清楚：默认构造的 provider 共用进程级单例总线，`clear()` 就是清空全进程的
        `mem:` 队列。只想清自己的状态，就用 `create_isolated()` / `for_scope()`。
        """
        self.bus.clear()

    async def _send(self, message: MailMessage) -> None:
        """
        发送并给广播挂上结算 barrier：等这条消息的全部投递（含重试链）落定后，才决定要不要
        把它退回队列（判定在 `should_requeue_after_broadcast`）。
        """
        topic = get_canonical_mailbox_address_identifier(message.to)
        deliveries = self.bus.publish(topic, message)
        if deliveries is None:
            return  # 没有订阅者：消息入队，由 fetch / ack 那套语义负责
        self._track_broadcast_settlement(message, deliveries)

    async def _subscribe(self, address: str, on_receive: DeliveryListener) -> Any:
        topic = get_canonical_mailbox_address_identifier(address)
        return self.bus.subscribe(topic, on_receive)

    async def _unsubscribe(self, subscription_id: str, unsubscribe_handle: Any) -> None:
        if callable(unsubscribe_handle):
            unsubscribe_handle()

    async def _fetch(self, address: str, options: Optional[FetchOptions] = None) -> Union[MailMessage, AckableMessage, None]:
        topic = get_canonical_mailbox_address_identifier(address)
        options = options if options is not None else FetchOptions()

        if not options.manual_ack:
            return self.bus.fetch_and_forget(topic)

        message = self.bus.fetch_for_ack(topic, options.ack_timeout)
        if message:
            msg_id = message.id

            async def ack_fn():
                self.bus.ack(msg_id)

            async def nack_fn(requeue: bool):
                self.bus.nack(msg_id, requeue)

            return AckableMessage(message, ack_fn, nack_fn)

        return None

    async def _peek(self, address: str, limit: Optional[float] = None) -> List[MailMessage]:
        """
        只读快照：副本，不动队列、不建 in-flight、不刷活动时间。

        之前没有这个能力：想“看一眼”只能 `fetch(..., manual_ack=True)` 不 ack，消息就进了
        in-flight（默认 30s 后重投），既干扰正常消费者的计数，也不是只读（issue#4）。
        """
        topic = get_canonical_mailbox_address_identifier(address)
        return self.bus.peek(topic, limit)

    async def list_addresses(self) -> List[str]:
        """本总线认得的地址（canonical id）—— `mem:` 的消息就在本地，所以能枚举。"""
        return self.bus.list_topics()

    async def _status(self, address: str) -> MailboxStatus:
        topic = get_canonical_mailbox_address_identifier(address)
        status_info = self.bus.get_status(topic)

        if not status_info['exists']:
            # 对“这个地址从不存在”回 `online` + 0，诊断页就分不出「不存在」和「空闲」（issue#4）。
            return MailboxStatus(
                state="unknown",
                exists=False,
                unread_count=status_info['unread_count'],
                in_flight_count=status_info['in_flight_count'],
                last_activity_time=status_info['last_activity_time'],
                extra={
                    'message': f"No queue, subscription or activity is known for '{topic}'.",
                    'subscriber_count': status_info['subscriber_count'],
                },
            )

        return MailboxStatus(
            state="online",
            exists=True,
            unread_count=status_info['unread_count'],
            in_flight_count=status_info['in_flight_count'],
            last_activity_time=status_info['last_activity_time'],
            extra={'subscriber_count': status_info['subscriber_count']}
        )

    async def _nack(self, message: MailMessage, requeue: bool) -> None:
        """订阅模式下广播结算后的处置入口（由 `_send` 的 barrier 调用一次，不在每个订阅者失败时调用）。

        不实现的话，订阅的消息不再入队后可重试错误等于消息静默消失。
        requeue=True（全部投递都失败）将消息退回 topic 队列头部（可被 fetch 取回、在
        unread_count 里可见）；requeue=False（有订阅者成功，或失败不可重试）按死信处理直接丢弃。
        """
        if not requeue:
            return
        topic = get_canonical_mailbox_address_identifier(message.to)
        self.bus.requeue(topic, message)
