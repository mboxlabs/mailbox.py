from typing import Dict, List, Any, Optional, Union, Callable
from datetime import datetime, timezone

from ..provider import MailboxProvider, AckableMessage, DeliveryListener
from ..message import MailMessage, MailboxStatus, FetchOptions
from ..retry import RetryPolicy
from ..utils import get_canonical_mailbox_address_identifier
from .queue import MailMessageQueue


async def _rethrow(error: BaseException):
    """把同步抛出的错误延后到被等待时再抛，交给 barrier 的 gather 归一处理。"""
    raise error


class MemoryEventBus:
    _instance = None

    def __init__(self):
        # 监听器 = 基类的投递入口：返回本次投递的最终结算（awaitable）
        self.topics: Dict[str, List[DeliveryListener]] = {}
        self.queue: MailMessageQueue[MailMessage] = MailMessageQueue()
        self.last_activity: Dict[str, str] = {}

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = MemoryEventBus()
        return cls._instance

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

    def get_status(self, topic: str) -> Dict[str, Any]:
        queue_status = self.queue.get_status(topic)
        subscriber_count = len(self.topics.get(topic, []))
        return {
            'unread_count': queue_status['unread_count'],
            'in_flight_count': queue_status['in_flight_count'],
            'last_activity_time': self.last_activity.get(topic),
            'subscriber_count': subscriber_count
        }

class MemoryProvider(MailboxProvider):
    #: 订阅是广播：一条消息对每个订阅者各投递一次。失败处理因此走投递级记账，
    #: 整条消息回不回队由 `_send` 里的结算 barrier 决定。
    broadcast_deliveries = True

    def __init__(self, retry: Optional[RetryPolicy] = None):
        """
        :param retry: 可选的订阅重试策略。不传则保持默认：订阅失败只把消息退回队列，不自动重投。
        """
        super().__init__("mem", retry)
        self.bus = MemoryEventBus.get_instance()

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

    async def _fetch(self, address: str, options: FetchOptions) -> Union[MailMessage, AckableMessage, None]:
        topic = get_canonical_mailbox_address_identifier(address)

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

    async def _status(self, address: str) -> MailboxStatus:
        topic = get_canonical_mailbox_address_identifier(address)
        status_info = self.bus.get_status(topic)

        return MailboxStatus(
            state="online",
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
