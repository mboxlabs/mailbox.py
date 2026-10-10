from typing import Dict, List, Any, Optional, Union, Callable, Awaitable
import asyncio
from datetime import datetime, timezone

from ..provider import MailboxProvider, AckableMessage, OnReceiveCallback
from ..message import MailMessage, MailboxStatus, FetchOptions
from ..retry import RetryPolicy
from ..utils import get_canonical_mailbox_address_identifier
from .queue import MailMessageQueue

class MemoryEventBus:
    _instance = None

    def __init__(self):
        self.topics: Dict[str, List[OnReceiveCallback]] = {}
        self.queue: MailMessageQueue[MailMessage] = MailMessageQueue()
        self.last_activity: Dict[str, str] = {}

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = MemoryEventBus()
        return cls._instance

    def subscribe(self, topic: str, listener: OnReceiveCallback) -> Callable[[], None]:
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

    async def publish(self, topic: str, message: MailMessage) -> None:
        self.last_activity[topic] = datetime.now(timezone.utc).isoformat()

        # 交给订阅者
        listeners = self.topics.get(topic)
        if listeners:
            for listener in listeners:
                # Python: asyncio.create_task -> fire and forget (同 TS/Rust 端)
                asyncio.create_task(listener(message))
            # 已交给订阅者的消息不再入队（与 TS 端一致）：两条路都走时，
            # 订阅者从不 fetch → 队列只涨不消；退订后一次 fetch 又会把
            # 已处理的消息再投递一遍。一个 topic 的投递方式只能选一种。
            # 注意这里必须判空列表：退订后 topics[topic] 仍存在但是空列表。
            return

        # 入队，等待 fetch 取回
        self.queue.enqueue(topic, message)

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
    def __init__(self, retry: Optional[RetryPolicy] = None):
        """
        :param retry: 可选的订阅重试策略。不传则保持默认：订阅失败只把消息退回队列，不自动重投。
        """
        super().__init__("mem", retry)
        self.bus = MemoryEventBus.get_instance()

    async def _send(self, message: MailMessage) -> None:
        topic = get_canonical_mailbox_address_identifier(message.to)
        await self.bus.publish(topic, message)

    async def _subscribe(self, address: str, on_receive: OnReceiveCallback) -> Any:
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
        """订阅模式下 on_receive 抛错时，provider.py 会带可重试判定结果调到这里。

        之前 MemoryProvider 没有实现：订阅的消息不再入队后，可重试错误等于消息静默消失。
        requeue=True 将消息退回 topic 队列头部（可被 fetch 取回、在 unread_count 可见）；
        requeue=False（非可重试）按死信处理直接丢弃。
        """
        if not requeue:
            return
        topic = get_canonical_mailbox_address_identifier(message.to)
        self.bus.requeue(topic, message)
