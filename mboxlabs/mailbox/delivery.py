"""
投递（delivery）身份的共享记账工具。

`subscribe` 是广播：一条消息 = N 次投递。失败处理若只有「消息」身份（`nack(message)`），
部分失败时就会把已成功者处理过的整条消息退回队列 —— 消息级和投递级两层语义不一致。

这里定义投递级的结算结果，以及「广播结算后要不要回队」的唯一判定。
三端（TS / Python / Rust）保持同一语义（见 TS 的 `src/lib/delivery.ts`）。

时间单位与 Python 其余 API 一致，用**秒**（TS 端用毫秒）。
"""

from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

# 单次投递**尝试**的默认结算超时（秒）；`0` 关闭。
#
# 卡死的订阅回调会让广播的结算 barrier 永不落定：消息既不回队也不死信，`unread_count`
# 里也看不见（等于静默消失）。看门狗给「一次尝试」一个上界：超时后按取消结算，消息回到
# 兜底路径。它只给单次尝试计时，所以不会掐断健康的重试链或退避等待。
DEFAULT_DELIVERY_SETTLE_TIMEOUT = 30.0

# 投递身份的分隔符，避免 id 拼接歧义（与 TS 的 `\u0000` 一致）。
DELIVERY_KEY_SEPARATOR = "\x00"

RETRIABLE = "retriable"
FATAL = "fatal"
CANCELLED = "cancelled"

FAILURE_REASONS = (RETRIABLE, FATAL, CANCELLED)


@dataclass(frozen=True)
class DeliveryResult:
    """
    一次投递的最终结算结果。

    一次投递 = 某个订阅者对某条消息的一次完整处理，**包含整条重试链**：
    只有当回调成功、或彻底失败（预算耗尽 / 不可重试 / 被取消）时才结算。
    广播的发送侧据此判断「有没有订阅者成功」。
    """

    delivery_id: str
    """投递身份：`subscription_id + message_id`，与 `AttemptLedger` 同一粒度。"""

    subscription_id: str
    message_id: str
    ok: bool
    """True = 回调成功结算；False = 这次投递最终失败。"""

    reason: Optional[str] = None
    """
    失败分类（`ok` 为 False 时才有意义）：
    - `retriable`：可重试错误，但重试预算已用尽 —— 值得让 `fetch()` 兜底。
    - `fatal`：不可重试 —— 按死信处理，不回队（避免无限重试同一条坏消息）。
    - `cancelled`：待重投被取消（退订 / 关闭）—— 等同 `retriable` 兜底。
    """


def delivery_key(subscription_id: str, message_id: str) -> str:
    """投递身份。"""
    return "{}{}{}".format(subscription_id, DELIVERY_KEY_SEPARATOR, message_id)


def subscription_id_of_delivery(delivery_id: str) -> str:
    """从投递身份里取出 subscription_id。"""
    return delivery_id.partition(DELIVERY_KEY_SEPARATOR)[0]


def message_id_of_delivery(delivery_id: str) -> str:
    """从投递身份里取出 message_id。"""
    return delivery_id.partition(DELIVERY_KEY_SEPARATOR)[2]


def is_delivery_result(value: Any) -> bool:
    """是否是一次投递的结算结果（鸭子类型，兼容第三方 provider 的自定义结果）。"""
    return isinstance(value, DeliveryResult) or isinstance(getattr(value, "ok", None), bool)


def to_delivery_results(settled: Iterable[Any]) -> List[DeliveryResult]:
    """
    把广播监听器的结算值归一化成 `DeliveryResult`。

    `settled` 是 `asyncio.gather(..., return_exceptions=True)` 的结果（对应 TS 的
    `Promise.allSettled`）。走 `MailboxProvider` 的订阅回调一定返回 `DeliveryResult`；
    第三方 provider 直接注册的监听器可能返回任意值 —— 正常返回视作成功，抛异常视作
    可重试失败（宁可多兜一次，`requeue()` 的去重守卫会挡住重复）。
    """
    results = []
    for index, value in enumerate(settled):
        if isinstance(value, BaseException):
            results.append(_listener_result(index, ok=False, reason=RETRIABLE))
        elif is_delivery_result(value):
            results.append(_as_delivery_result(value))
        else:
            results.append(_listener_result(index, ok=True))
    return results


def _listener_result(index: int, ok: bool, reason: Optional[str] = None) -> DeliveryResult:
    return DeliveryResult(
        delivery_id="listener{}{}".format(DELIVERY_KEY_SEPARATOR, index),
        subscription_id="",
        message_id="",
        ok=ok,
        reason=reason,
    )


def _as_delivery_result(value: Any) -> DeliveryResult:
    ok = bool(value.ok)
    reason = getattr(value, "reason", None)
    if reason not in FAILURE_REASONS:
        reason = None
    return DeliveryResult(
        delivery_id=getattr(value, "delivery_id", "") or "",
        subscription_id=getattr(value, "subscription_id", "") or "",
        message_id=getattr(value, "message_id", "") or "",
        ok=ok,
        reason=None if ok else reason,
    )


def should_requeue_after_broadcast(results: Iterable[DeliveryResult]) -> bool:
    """
    广播结算后，是否把整条消息退回队列。

    判定只看投递，不看「这个 topic 还有没有活跃订阅者」—— 后者会把全部订阅者都失败的情况
    变成静默丢失。正确条件：

    1. **没有任何投递成功** —— 有成功者时整条回队就是跨投递模式重复投递；
    2. **至少一次投递值得重试**（`retriable` / `cancelled`）—— 全是 `fatal` 就按死信丢弃。

    :param results: 同一消息在广播里全部投递的结算结果。
    """
    results = list(results)
    if not results:
        return False
    if any(result.ok for result in results):
        return False  # 已有订阅者处理过，回队 = 副作用再执行一次
    return any(result.reason != FATAL for result in results)


__all__ = [
    "CANCELLED",
    "DEFAULT_DELIVERY_SETTLE_TIMEOUT",
    "DELIVERY_KEY_SEPARATOR",
    "DeliveryResult",
    "FATAL",
    "FAILURE_REASONS",
    "RETRIABLE",
    "delivery_key",
    "is_delivery_result",
    "message_id_of_delivery",
    "should_requeue_after_broadcast",
    "subscription_id_of_delivery",
    "to_delivery_results",
]
