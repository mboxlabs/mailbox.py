from abc import ABC, abstractmethod
from typing import Any, Callable, Awaitable, Optional, Union
from datetime import datetime, timezone
import uuid

from .message import MailMessage, MailboxStatus, FetchOptions
from .error import MailboxError

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
    def __init__(self, protocol: str):
        self.protocol = protocol
        self._subscriptions: Dict[str, Any] = {} # Map sub_id to internal handle/info

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
            except Exception as e:
                await self._handle_receive_error(e, message)

        # Call concrete implementation
        unsubscribe_handle = await self._subscribe(address, wrapped_on_receive)

        # Store subscription info
        self._subscriptions[sub_id] = {
            'id': sub_id,
            'address': address,
            'handle': unsubscribe_handle
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

    async def _handle_receive_error(self, error: Exception, message: MailMessage) -> None:
        print(f"[{self.protocol}] Error processing message {message.id}: {error}")
        # Default strategy: NACK with requeue if it looks transient?
        # For now, let's just NACK without requeue to avoid infinite loops unless we have retry logic.
        # Or maybe requeue=True for connection errors.
        # Simple implementation:
        await self.nack(message, requeue=False)

    def _create_subscription_object(self, sub_id: str) -> Subscription:
        provider = self

        class ConcreteSubscription(Subscription):
            async def unsubscribe(self) -> None:
                if sub_id in provider._subscriptions:
                    info = provider._subscriptions[sub_id]
                    await provider._unsubscribe(sub_id, info['handle'])
                    del provider._subscriptions[sub_id]

        return ConcreteSubscription()
