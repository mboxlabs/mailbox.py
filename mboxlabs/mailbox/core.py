from typing import Dict, Optional, Union
from urllib.parse import urlparse

from .message import OutgoingMail, MailMessage, MailboxStatus, FetchOptions
from .provider import MailboxProvider, Subscription, AckableMessage
from .error import ProviderNotFound

import asyncio
...
class Mailbox:
    def __init__(self):
        self._providers: Dict[str, MailboxProvider] = {}

    @property
    def providers(self) -> Dict[str, MailboxProvider]:
        """
        Returns a copy of all registered providers.
        """
        return self._providers.copy()

    def get_provider(self, protocol: str, raise_error_if_failed: bool = False) -> Optional[MailboxProvider]:
        """
        Returns the provider for the specified protocol.

        :param protocol: The protocol name (e.g., "mem" or "mem:").
        :param raise_error_if_failed: If True, raises ProviderNotFound if the provider is not found.
        """
        key = protocol
        if protocol.endswith(':'):
            key = protocol[:-1]

        if key not in self._providers:
            if raise_error_if_failed:
                raise ProviderNotFound(key)
            return None
        return self._providers[key]

    async def start(self) -> None:
        """
        Starts all registered providers.
        """
        providers = list(self._providers.values())
        if providers:
            await asyncio.gather(*(p.init() for p in providers))

    async def stop(self) -> None:
        """
        Stops all registered providers and releases resources.
        """
        providers = list(self._providers.values())
        if providers:
            await asyncio.gather(*(p.close() for p in providers))

    def register_provider(self, provider: MailboxProvider) -> None:
        self._providers[provider.protocol] = provider

    async def post(self, mail: OutgoingMail) -> MailMessage:
        parsed_to = urlparse(mail.to)
        provider = self.get_provider(parsed_to.scheme, raise_error_if_failed=True)

        message_id = mail.id if mail.id else provider.generate_id()

        message = MailMessage.from_outgoing(mail, message_id)

        return await provider.send(message)

    async def subscribe(self, address: str, on_receive: callable) -> Subscription:
        parsed_address = urlparse(address)
        provider = self.get_provider(parsed_address.scheme, raise_error_if_failed=True)
        return await provider.subscribe(address, on_receive)

    async def fetch(self, address: str, options: FetchOptions) -> Union[MailMessage, AckableMessage, None]:
        parsed_address = urlparse(address)
        provider = self.get_provider(parsed_address.scheme, raise_error_if_failed=True)
        return await provider.fetch(address, options)

    async def status(self, address: str) -> MailboxStatus:
        parsed_address = urlparse(address)
        provider = self.get_provider(parsed_address.scheme, raise_error_if_failed=True)
        return await provider.status(address)
