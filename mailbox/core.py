from typing import Dict, Optional, Union
from urllib.parse import urlparse

from .message import OutgoingMail, MailMessage, MailboxStatus, FetchOptions
from .provider import MailboxProvider, Subscription, AckableMessage
from .error import ProviderNotFound

class Mailbox:
    def __init__(self):
        self.providers: Dict[str, MailboxProvider] = {}

    def register_provider(self, provider: MailboxProvider) -> None:
        self.providers[provider.protocol] = provider

    def _get_provider(self, protocol: str) -> MailboxProvider:
        # Protocol usually comes with ':', so we might need to strip it if the map keys don't have it.
        # In TS, it does `protocol.slice(0, -1)`.
        # Here, we assume the provider.protocol returns "mem" (without colon).
        # And the URL protocol is "mem:".
        key = protocol
        if protocol.endswith(':'):
            key = protocol[:-1]

        if key not in self.providers:
            raise ProviderNotFound(key)
        return self.providers[key]

    async def post(self, mail: OutgoingMail) -> MailMessage:
        parsed_to = urlparse(mail.to)
        provider = self._get_provider(parsed_to.scheme)

        message_id = mail.id if mail.id else provider.generate_id()

        message = MailMessage.from_outgoing(mail, message_id)

        return await provider.send(message)

    async def subscribe(self, address: str, on_receive: callable) -> Subscription:
        parsed_address = urlparse(address)
        provider = self._get_provider(parsed_address.scheme)
        return await provider.subscribe(address, on_receive)

    async def fetch(self, address: str, options: FetchOptions) -> Union[MailMessage, AckableMessage, None]:
        parsed_address = urlparse(address)
        provider = self._get_provider(parsed_address.scheme)
        return await provider.fetch(address, options)

    async def status(self, address: str) -> MailboxStatus:
        parsed_address = urlparse(address)
        provider = self._get_provider(parsed_address.scheme)
        return await provider.status(address)
