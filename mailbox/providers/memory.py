from typing import Dict, List, Any, Optional, Union, Callable, Awaitable
import asyncio
from datetime import datetime, timezone

from ..provider import MailboxProvider, AckableMessage, OnReceiveCallback
from ..message import MailMessage, MailboxStatus, FetchOptions
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

        # Push to subscribers
        if topic in self.topics:
            for listener in self.topics[topic]:
                # Execute listener (fire and forget or await?)
                # In Rust/TS it's async. Here we await it to ensure order or spawn task?
                # TS: listeners.forEach(listener => listener(message)); -> fire and forget if async?
                # Rust: tokio::spawn -> fire and forget
                # Python: asyncio.create_task -> fire and forget
                asyncio.create_task(listener(message))

        # Enqueue for pull consumers
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

    def get_status(self, topic: str) -> Dict[str, Any]:
        unread_count = self.queue.get_status(topic)
        subscriber_count = len(self.topics.get(topic, []))
        return {
            'unread_count': unread_count,
            'last_activity_time': self.last_activity.get(topic),
            'subscriber_count': subscriber_count
        }

class MemoryProvider(MailboxProvider):
    def __init__(self):
        super().__init__("mem")
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
            last_activity_time=status_info['last_activity_time'],
            extra={'subscriber_count': status_info['subscriber_count']}
        )
