from typing import Any, Dict, Optional
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
    state: str
    unread_count: Optional[int] = None
    last_activity_time: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

@dataclass
class FetchOptions:
    """
    Options for fetching messages.
    """
    manual_ack: bool = False
    ack_timeout: Optional[float] = None  # Seconds
