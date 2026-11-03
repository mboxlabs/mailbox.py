from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

@dataclass
class OutgoingMail:
    """
    Represents a message to be sent.
    """
    from_: str
    to: str
    body: Any
    id: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)

@dataclass
class MailMessage:
    """
    Represents a message in the system.
    """
    id: str
    from_: str
    to: str
    body: Any
    headers: Dict[str, str] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_outgoing(cls, outgoing: OutgoingMail, id: str) -> 'MailMessage':
        return cls(
            id=id,
            from_=outgoing.from_,
            to=outgoing.to,
            body=outgoing.body,
            headers=outgoing.headers,
            meta=outgoing.meta
        )

@dataclass
class MailboxStatus:
    """
    Represents the status of a mailbox address.
    """
    #: provider 对**这个地址**的可观测状态：
    #: - `online`：provider 在线，且认得这个地址（有队列 / 订阅 / 活动记录）；
    #: - `unknown`：provider 在线，但对这个地址**没有任何记录**（地址不存在、拼错，
    #:   或者 provider 本身不支持状态查询）；
    #: - `offline` / `degraded`：provider 自己连不上 / 只能给出部分答复。
    #:
    #: 不要把「地址不存在」报成 `online` + 0：诊断页就区分不出「不存在」与「空闲」（issue#4）。
    #: 要区分「地址不存在」和「provider 答不了」，看 `exists`。
    state: str
    #: provider 是否认得这个地址（有过消息 / 订阅 / 活动记录）。
    #: 只有在 provider **明确回答了**状态查询时才有值；不支持 `status` 的 provider 不会把它
    #: 设成 `False`（那会把「问不到」误报成「不存在」）。
    exists: Optional[bool] = None
    #: 队列中未被消费的消息数量（不包含 in-flight）。
    unread_count: Optional[int] = None
    # Messages handed to a consumer but not yet ack'd/nack'd. Kept separate from
    # unread_count so stuck in-flight messages are not invisible in status.
    in_flight_count: Optional[int] = None
    #: 最后一次有消息活动（发送或接收）的时间 (ISO 8601)；没有活动记录时是 `None`（JSON 里就是
    #: `null`）。绝不能是「字段不存在」：那样一次序列化之后整个字段消失，诊断页拿到的是
    #: 「没有这个字段」而不是「没有活动」。
    last_activity_time: Optional[str] = None
    #: provider 特定的扩展信息（`subscriber_count`、错误说明 `message` 等）。
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MailboxAddressSnapshot(MailboxStatus):
    """快照里一个地址的状态：`MailboxStatus` 加地址身份。"""
    #: 寻址用的规范形式（见 `utils.get_canonical_mailbox_address_identifier`）。
    canonical_id: str = ""
    #: 处理这个地址的 provider 协议名。
    protocol: str = ""

    @classmethod
    def from_status(cls, canonical_id: str, protocol: str, status: MailboxStatus) -> "MailboxAddressSnapshot":
        return cls(
            canonical_id=canonical_id,
            protocol=protocol,
            state=status.state,
            exists=status.exists,
            unread_count=status.unread_count,
            in_flight_count=status.in_flight_count,
            last_activity_time=status.last_activity_time,
            extra=dict(status.extra),
        )


@dataclass
class MailboxSnapshot:
    """
    全量快照：一时刻整个 `Mailbox`（所有 provider、所有 topic）的诊断视图。

    给诊断页、测试断言用（issue#4）。只能由能枚举自身 topic 的 provider（实现
    `MailboxProvider.list_addresses`）提供地址；不能枚举的（AMQP / SQS 这类消息不在本地的
    broker）列在 `unsupported_providers` 里，避免快照把「枚举不了」误读成「队列为空」。
    """
    #: 快照的完整度：
    #: - `online`：所有 provider 都能枚举，且所有地址都是 `online`；
    #: - `degraded`：有 provider 无法枚举，或某个地址不是 `online`（只看到了一部分）；
    #: - `unknown`：没有任何 provider 能枚举（包括一个 provider 都没有注册）。
    state: str
    #: 已注册的协议名（排序后）。
    protocols: List[str] = field(default_factory=list)
    #: 快照里的地址总数。
    address_count: int = 0
    #: 所有地址的 `unread_count` 之和。
    unread_count: int = 0
    #: 所有地址的 `in_flight_count` 之和。
    in_flight_count: int = 0
    #: 按 `canonical_id` 排序的地址状态，保证快照可重复、断言可写死。
    addresses: List[MailboxAddressSnapshot] = field(default_factory=list)
    #: 不支持枚举自身地址的 provider（它们的队列不在本次快照里）。
    unsupported_providers: List[str] = field(default_factory=list)
    #: 快照生成的时间（ISO 8601）。
    taken_at: str = ""


@dataclass
class FetchOptions:
    """
    Options for fetching messages.
    """
    manual_ack: bool = False
    # Seconds. None -> DEFAULT_ACK_TIMEOUT; 0 -> disable the stale check entirely
    # (the message stays in-flight until it is ack'd or nack'd).
    ack_timeout: Optional[float] = None
    #: 【只读】`peek=True`：返回队列里消息的**副本**列表，不出队、不建 in-flight、不刷活动时间
    #: （诊断 / 审计用）。与 `manual_ack` 同时给出会直接报错（互相矛盾）；provider 不支持只读
    #: 时抛 `UnsupportedCapabilityError`，不会被静默降级成普通取信。
    peek: bool = False
    #: `peek` 最多看几条；`None`（或 `math.inf`）看整个队列。必须是非负整数。
    limit: Optional[int] = None
