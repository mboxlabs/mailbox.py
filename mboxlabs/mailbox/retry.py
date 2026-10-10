"""
订阅重试的共享策略与记账工具。

这里只放「策略数学」——纯函数、可单测；拦截订阅失败的位置仍在 `MailboxProvider`
基类，因为那是唯一能看到回调抛错的地方。

三端（TS / Python / Rust）保持同一套语义：`is_retriable_error` 的判定条件、退避公式、
尝试计数方式都一致。时间单位与 Python 其余 API 一致，用**秒**（TS 端用毫秒）。
"""

from dataclasses import dataclass
from typing import Optional
import time


@dataclass
class RetryPolicy:
    """
    订阅失败后的重试策略。

    注意：重试是 **opt-in** 的。默认（`None`）保持「失败只回队、不自动重投」的行为，
    因为 AMQP / SQS 这类传输层自带重投与死信，库再重投就是双重投递。
    """

    max_attempts: int
    """总尝试次数，含首次。必须 >= 1；1 等价于不重试。"""

    base_delay: float
    """首次重试前的等待秒数。"""

    factor: float = 2.0
    """退避倍数。"""

    max_delay: float = 30.0
    """单次等待上限（秒）。"""


DEFAULT_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay=1.0, factor=2.0, max_delay=30.0)

RETRIABLE_ERROR_CODES = (503, 504, "ECONNRESET", "ETIMEDOUT")


def resolve_retry_policy(policy: Optional[RetryPolicy]) -> Optional[RetryPolicy]:
    """归一化策略；`None` 表示不重试（保持默认行为）。"""
    if policy is None:
        return None
    return RetryPolicy(
        max_attempts=max(1, int(policy.max_attempts)),
        base_delay=max(0.0, float(policy.base_delay)),
        factor=float(policy.factor),
        max_delay=float(policy.max_delay),
    )


def is_retriable_error(error: BaseException) -> bool:
    """
    判定错误是否可重试。与 TS 的 `isRetriableError` / Rust 的 `MailboxError::is_retriable` 一致：
    显式标记，或错误码命中传输层重试白名单。

    标记属性名同时接受 `is_retriable`（Python 习惯）与 `isRetriable`（与 TS 对齐）。
    """
    for attr in ("is_retriable", "isRetriable"):
        if getattr(error, attr, False) is True:
            return True
    return getattr(error, "code", None) in RETRIABLE_ERROR_CODES


def next_retry_delay(policy: RetryPolicy, attempt: int) -> float:
    """
    第 `attempt` 次尝试失败之后，下一次重试要等多久（秒）。

    `attempt` 是已经发生的尝试次数（首次失败即 1），因此第一次退避就是 `base_delay`。
    指数退避并夹在 `max_delay` 之内。
    """
    resolved = resolve_retry_policy(policy) or DEFAULT_RETRY_POLICY
    safe_attempt = max(1, int(attempt))
    delay = resolved.base_delay * (resolved.factor ** (safe_attempt - 1))
    return min(delay, resolved.max_delay)


class AttemptLedger:
    """
    按「订阅者 + 消息」维度记录已经尝试过几次。

    键必须包含订阅者：订阅是广播，某个订阅者失败不代表其他订阅者也失败，重试次数不能混在一起算。
    条目带 TTL，避免长期运行的进程里台账无限增长。
    """

    def __init__(self, ttl_seconds: float = 300.0):
        self._entries: dict = {}
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _key(subscription_id: str, message_id: str) -> str:
        return "{}\x00{}".format(subscription_id, message_id)

    def record(self, subscription_id: str, message_id: str) -> int:
        """记录一次尝试，返回这是第几次。"""
        self._gc()
        key = self._key(subscription_id, message_id)
        entry = self._entries.get(key)
        attempts = (entry[0] if entry else 0) + 1
        self._entries[key] = (attempts, time.monotonic())
        return attempts

    def count(self, subscription_id: str, message_id: str) -> int:
        """当前已尝试次数（未记录过则为 0）。"""
        entry = self._entries.get(self._key(subscription_id, message_id))
        return entry[0] if entry else 0

    def reset(self, subscription_id: str, message_id: str) -> None:
        """处理成功后清零，让同 id 的消息日后还能重试。"""
        self._entries.pop(self._key(subscription_id, message_id), None)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def size(self) -> int:
        return len(self._entries)

    def _gc(self) -> None:
        now = time.monotonic()
        expired = [key for key, (_, updated_at) in self._entries.items()
                   if now - updated_at > self._ttl_seconds]
        for key in expired:
            del self._entries[key]


__all__ = [
    "AttemptLedger",
    "DEFAULT_RETRY_POLICY",
    "RETRIABLE_ERROR_CODES",
    "RetryPolicy",
    "is_retriable_error",
    "next_retry_delay",
    "resolve_retry_policy",
]
