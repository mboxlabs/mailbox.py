from typing import Dict, Optional, Union
from urllib.parse import urlparse
import asyncio
import weakref

from .message import OutgoingMail, MailMessage, MailboxStatus, FetchOptions
from .provider import MailboxProvider, Subscription, AckableMessage
from .error import ProviderNotFound


class _ProviderOwnerMark:
    """「这个 provider 实例当前属于哪个 Mailbox」的标记（含已经告过的警）。"""
    __slots__ = ("owner", "warned")

    def __init__(self, owner: "Mailbox"):
        self.owner = owner
        self.warned = False


#: 标记表。TS 端用 symbol 键 + 不可枚举属性；Python 没有 symbol，用弱引用键的字典：
#: 同样不往用户的 `__dict__` 里塞东西，provider 被回收时标记一起消失（不会泄漏）。
_PROVIDER_OWNERS: "weakref.WeakKeyDictionary[MailboxProvider, _ProviderOwnerMark]" = weakref.WeakKeyDictionary()


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

    async def stop(self, purge: bool = False) -> None:
        """
        Stops all registered providers and releases resources.

        默认只拆订阅、不动积压消息：`stop()` 后同一个 provider（或共享同一份状态的另一个
        `Mailbox`）重新 `start()`，队列里的残留消息照旧可见——这是 issue#3 里“测试隔离失效”的根源。
        确实要销毁状态时显式传 `purge=True`。
        """
        providers = list(self._providers.values())
        if providers:
            await asyncio.gather(*(p.close() for p in providers))

        if purge:
            await self.clear()

    async def clear(self) -> None:
        """
        丢弃各 provider 持有的积压消息（含 in-flight），**不**拆订阅。

        测试隔离、用例之间重置状态用它。基类的 `MailboxProvider.clear()` 默认是空实现，
        传输型 provider（AMQP / SQS）因此天然是 no-op：清 broker 属于破坏性运维，不由库代劳。

        注意默认构造的 `MemoryProvider` 共用进程级单例总线——`clear()` 会清掉同一份总线上
        其它 `Mailbox` 的队列。只清自己那份，请用 `MemoryProvider.create_isolated()`
        或 `MemoryProvider.for_scope(scope)`。
        """
        providers = list(self._providers.values())
        if providers:
            await asyncio.gather(*(p.clear() for p in providers))

    def register_provider(self, provider: MailboxProvider) -> None:
        _mark_provider_ownership(self, provider)
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


def _mark_provider_ownership(mailbox: "Mailbox", provider: MailboxProvider) -> None:
    """
    同一个 provider 实例被多个 `Mailbox` 复用时告警一次。

    订阅表挂在 provider 上，不是挂在 Mailbox 上：`a.stop()` 会拆掉 `b` 在这个 provider 上的
    订阅。行为上无法替用户拆开（provider 自己不知道订阅是谁建的），至少让这个坑可见。
    每个 Mailbox 应该持有自己的 provider 实例（`MemoryProvider.create_isolated()`）。
    """
    try:
        mark = _PROVIDER_OWNERS.get(provider)
    except TypeError:
        # provider 不可弱引用（自定义了 __slots__ 之类）：跳过检测，行为不变。
        return

    if mark is None:
        _PROVIDER_OWNERS[provider] = _ProviderOwnerMark(mailbox)
        return

    if mark.owner is mailbox or mark.warned:
        return  # 同一个 Mailbox 内重复注册是常规用法；已经告过就不重复噪。

    mark.warned = True
    print(
        f"[{provider.protocol}] The provider instance is shared by more than one Mailbox. "
        "Subscriptions live on the provider, so `stop()` / `close()` on either Mailbox "
        "unsubscribes the other one. Register a provider per Mailbox "
        "(e.g. `MemoryProvider.create_isolated()`) if they are meant to be independent."
    )
