from typing import Dict, List, Optional, Union
import asyncio
from datetime import datetime, timezone
import weakref

from .message import (
    OutgoingMail,
    MailMessage,
    MailboxAddressSnapshot,
    MailboxSnapshot,
    MailboxStatus,
    FetchOptions,
)
from .provider import MailboxProvider, Subscription, AckableMessage
from .error import ProviderNotFound
from .utils import parse_mailbox_address


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
        parsed_to = parse_mailbox_address(mail.to, "post()")
        parse_mailbox_address(mail.from_, "post() (from)")  # 两端规则要一致
        provider = self.get_provider(parsed_to.scheme, raise_error_if_failed=True)

        message_id = mail.id if mail.id else provider.generate_id()

        message = MailMessage.from_outgoing(mail, message_id)

        return await provider.send(message)

    async def subscribe(self, address: str, on_receive: callable) -> Subscription:
        parsed_address = parse_mailbox_address(address, "subscribe()")
        provider = self.get_provider(parsed_address.scheme, raise_error_if_failed=True)
        return await provider.subscribe(address, on_receive)

    async def fetch(self, address: str, options: Optional[FetchOptions] = None) -> Union[
        MailMessage, AckableMessage, List[MailMessage], None
    ]:
        """
        取信 / 只读快照。

        - 默认（不传 `options`）：取一封，隐式 ACK（出队即完成）。
        - `FetchOptions(manual_ack=True)`：取一封，进 in-flight，直到 ack / nack 或超时重投。
        - `FetchOptions(peek=True, limit=n)`：**只读**——返回副本列表（空队列是 `[]`），不出队、
          不建 in-flight、不刷活动时间。诊断与测试断言用这个，不要用「manual_ack 但不 ack」凑。

        `peek` 与 `manual_ack` 同时给出会直接报错（互相矛盾），provider 不支持只读时抛
        `UnsupportedCapabilityError`——都不会被静默降级成普通取信。
        """
        parsed_address = parse_mailbox_address(address, "fetch()")
        provider = self.get_provider(parsed_address.scheme, raise_error_if_failed=True)
        return await provider.fetch(address, options)

    async def status(self, address: Optional[str] = None) -> Union[MailboxStatus, MailboxSnapshot]:
        """
        查询指定地址的状态信息；**不传地址 = 全量快照**（见 `snapshot()`）。

        地址在 provider 里没有任何记录（没投过、没订阅过、没有活动）时，返回
        `state='unknown'` + `exists=False`，而不是 `state='online'` + 0——诊断页要能区分
        “这个地址不存在”和“这个地址空闲”。

        :param address: 要查询的地址；`None` 时返回全量快照。
        """
        if address is None:
            return await self.snapshot()

        parsed_address = parse_mailbox_address(address, "status()")
        provider = self.get_provider(parsed_address.scheme, raise_error_if_failed=True)

        return await provider.status(address)

    async def snapshot(self) -> MailboxSnapshot:
        """
        全量快照：把所有能枚举自身地址的 provider 的 topic 状态汇成一个可断言的结构。

        等价于不传地址的 `status()`，只是名字更直白（诊断页里 `mailbox.snapshot()` 比
        `mailbox.status()` 更好读）。不能枚举的 provider（消息不在本地，如 AMQP / SQS）会列在
        `unsupported_providers` 里，不会被误报成“队列为空”。
        """
        protocols = sorted(self._providers.keys())
        addresses: List[MailboxAddressSnapshot] = []
        unsupported_providers: List[str] = []

        for protocol, provider in self._providers.items():
            try:
                canonical_ids = await provider.list_addresses()
            except NotImplementedError:
                unsupported_providers.append(protocol)
                continue

            for canonical_id in canonical_ids:
                addresses.append(
                    MailboxAddressSnapshot.from_status(
                        canonical_id, protocol, await _status_of_canonical_address(provider, canonical_id)
                    )
                )

        addresses.sort(key=lambda entry: entry.canonical_id)

        unread_count = sum(entry.unread_count or 0 for entry in addresses)
        in_flight_count = sum(entry.in_flight_count or 0 for entry in addresses)
        enumerated = len(protocols) - len(unsupported_providers)

        if enumerated == 0:
            state = "unknown"  # 没有任何 provider 能枚举：这份快照什么都没说
        elif unsupported_providers or any(entry.state != "online" for entry in addresses):
            state = "degraded"  # 只看到一部分，或有地址本身就不在线
        else:
            state = "online"

        return MailboxSnapshot(
            state=state,
            protocols=protocols,
            address_count=len(addresses),
            unread_count=unread_count,
            in_flight_count=in_flight_count,
            addresses=addresses,
            unsupported_providers=sorted(unsupported_providers),
            taken_at=datetime.now(timezone.utc).isoformat(),
        )


async def _status_of_canonical_address(provider: MailboxProvider, canonical_id: str) -> MailboxStatus:
    """
    快照里对单个地址的查询：canonical id 本身可以重新解析成地址（它已经是
    `protocol://user@host/path` 形式）。解析不了是 provider 的 bug，宁可报 unknown
    也不能让整个快照崩掉。
    """
    try:
        return await provider.status(canonical_id)
    except Exception as error:  # pragma: no cover - provider 自己报的 id 又查不回来
        return MailboxStatus(
            state="unknown",
            exists=True,
            extra={
                "message": f"The '{provider.protocol}' provider listed '{canonical_id}', "
                           "but it could not be queried back.",
                "cause": str(error),
            },
        )


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
