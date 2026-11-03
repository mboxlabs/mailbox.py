"""地址解析 / 规范化，以及只读快照（peek）的选项校验与副本。

三端（TS `src/lib/utils.ts` / Python / Rust）用同一条地址规则，参数名保持一致：
邮箱语义留在 path 里，query / hash 不参与寻址，因此带着它们的地址直接被拒绝。
"""

import copy as copy_module
import math
from dataclasses import fields, is_dataclass
from typing import Any, Optional, Union
from urllib.parse import SplitResult, urlparse

from .error import InvalidFetchOptionsError, InvalidMailboxAddressError

_ATOMIC = (type(None), bool, int, float, complex, str, bytes)


def _parse_or_pass(address: Union[str, SplitResult]) -> SplitResult:
    """已经解析过的（`SplitResult` / `ParseResult`）直接用，只有字符串才解析。"""
    if isinstance(address, (str, bytes)):
        return urlparse(address)
    return address


def assert_lossless_mailbox_address(
    address: Union[str, SplitResult], context: str = "mailbox address"
) -> None:
    """
    校验一个地址在规范化时不会**静默丢掉**信息。

    `mem:inbox@host/thread?thread=1` 和 `mem:inbox@host/thread` 是两个逻辑地址，但它们的
    canonical id 相同（见 `get_canonical_mailbox_address_identifier`）—— query / hash 会被丢掉。
    与其让两条互不相干的业务流共用同一条队列（issue#4：「看起来能用、实际共用一条队列」），
    不如在入口就拒绝：**邮箱语义必须留在 path 里**，query / hash 不参与寻址。

    :param address: 待校验的地址（字符串或已解析的 `SplitResult`）。
    :param context: 出错信息里带上的调用点名字（例如 `'post()'`），便于定位。
    :raises InvalidMailboxAddressError: 地址带 query 或 hash 时抛出。
    """
    parsed = _parse_or_pass(address)
    raw = address if isinstance(address, str) else address.geturl()

    dropped = []

    if parsed.query:
        dropped.append('query "?{}"'.format(parsed.query))
    elif "?" in raw:
        # `mem:host/inbox?` —— query 是空串，但意图仍然是「带 query」
        dropped.append('an empty query ("?")')

    if parsed.fragment:
        dropped.append('hash "#{}"'.format(parsed.fragment))
    elif "#" in raw:
        dropped.append('an empty hash ("#")')

    if dropped:
        raise InvalidMailboxAddressError(
            raw,
            'Invalid mailbox address "{}" in {}: {} would be dropped by address canonicalization, '
            'silently merging this address with "{}". Mailbox semantics must live in the path '
            '(e.g. "mem:host/inbox/thread-1"), not in the query or hash.'.format(
                raw, context, " and ".join(dropped), _canonical_of(parsed)
            ),
        )


def parse_mailbox_address(address: Union[str, SplitResult], context: str = "mailbox address") -> SplitResult:
    """
    把用户传入的地址解析成 `SplitResult`，并校验它可被无损规范化
    （见 `assert_lossless_mailbox_address`）。

    所有入口（`post` / `subscribe` / `fetch` / `status`）都走这里，保证「同一个地址在每个入口
    都指向同一个 topic」，且非法形式在同一处报错。

    `urlparse` 比 TS 的 `new URL()` 宽松得多：缺 scheme 的字符串它照样返回一个结果。那在这里
    同样算「无法解析」——没有 scheme 就选不出 provider，地址根本无从寻址。

    :param address: 地址字符串（或已解析的 `SplitResult`）。
    :param context: 出错信息里带上的调用点名字。
    :return: 解析后的 `SplitResult`。
    :raises InvalidMailboxAddressError: 无法解析，或带 query / hash。
    """
    raw = address if isinstance(address, str) else getattr(address, "geturl", lambda: str(address))()
    try:
        parsed = _parse_or_pass(address)
    except ValueError as error:  # urlparse 对非法端口 / 非法 IPv6 字面量会抛 ValueError
        raise InvalidMailboxAddressError(
            raw,
            'Invalid mailbox address "{}" in {}: not a parseable URL.'.format(raw, context),
        ) from error

    if not parsed.scheme:
        raise InvalidMailboxAddressError(
            raw,
            'Invalid mailbox address "{}" in {}: not a parseable URL '
            '(a mailbox address must start with "<protocol>:").'.format(raw, context),
        )

    # 把**原始字符串**交给无损校验：`urlunsplit` 会把空的 `?` / `#` 抹掉，只有原串能区分
    # “mem:host/inbox?”（空 query，意图仍然是带 query）和“mem:host/inbox”。
    assert_lossless_mailbox_address(raw, context)
    return parsed


def _canonical_of(parsed: SplitResult) -> str:
    """由已解析的地址拼出 canonical id：`protocol://user@host/path`（抹掉密码）。"""
    if parsed.netloc:
        userinfo, sep, host = parsed.netloc.rpartition("@")
        user = userinfo.partition(":")[0]  # 密码不进 topic：它不是地址的一部分
        netloc = "{}{}{}".format(user, sep, host) if sep else host
        return "{}://{}{}".format(parsed.scheme, netloc, parsed.path)
    # opaque 形式（mem:user@host/path）：整段都是 path。补上 `//` 后与 hierarchical
    # 形式（mem://user@host/path）落到同一个 topic。
    return "{}://{}".format(parsed.scheme, parsed.path)


def get_canonical_mailbox_address_identifier(address_url: Union[str, SplitResult]) -> str:
    """
    生成地址的 canonical id：`protocol://user@host/path`（不带 query / hash，抹掉密码）。

    query 曾经也被「顺手丢掉」—— 那会让 `?thread=1`、`#fragment` 这类写法看起来能用、实际和
    不带 query 的地址塌成同一个 topic（issue#4）。现在这类地址在规范化之前就被拒绝：调用方拿到
    的是明确错误，而不是一条被合并的队列。

    canonical id 带上了 protocol，因此可以再解析回地址（`Mailbox.snapshot()` 就是这么按
    canonical id 复查每个地址的），并且规范化是幂等的。

    :param address_url: 地址字符串（或已解析的 `SplitResult`）。
    :return: 该地址的 canonical id。
    :raises InvalidMailboxAddressError: 地址带 query / hash（它们无法在 canonical id 里保留）。
    """
    parsed = parse_mailbox_address(address_url, "address canonicalization")
    return _canonical_of(parsed)


def normalize_peek_limit(limit: Optional[float]) -> Optional[int]:
    """
    校验并归一 `peek` 的 `limit`。

    `None` / `math.inf` 表示「整个队列」；其余必须是**非负整数**。
    负数、`NaN`、小数都会让「取几条」变成含糊语义，直接拒绝。

    :return: 归一后的 limit；`None` 表示不限条数。
    :raises InvalidFetchOptionsError: 取值非法。
    """
    if limit is None or (isinstance(limit, float) and math.isinf(limit) and limit > 0):
        return None

    bad = isinstance(limit, bool) or not isinstance(limit, (int, float))
    if not bad and isinstance(limit, float) and not limit.is_integer():
        bad = True  # NaN、小数都是调用方的 bug
    if not bad and isinstance(limit, float) and math.isinf(limit):  # -inf
        bad = True
    if bad or limit < 0:
        raise InvalidFetchOptionsError(
            "Invalid `limit` for read-only peek: expected a non-negative integer "
            "(or math.inf for the whole queue), got {!r}.".format(limit)
        )
    return int(limit)


def _clone_or_share(value: Any) -> Any:
    """能深拷贝就深拷贝，拷不动就原样返回引用（函数 / 锁 / 连接拷贝不了）。"""
    if isinstance(value, _ATOMIC):
        return value
    try:
        return copy_module.deepcopy(value)
    except Exception:
        return value


def copy_for_peek(message: Any) -> Any:
    """
    给只读快照（peek）做一份消息副本。

    副本是必需的：直接把队列里的对象交给调用方，调用方改一下字段就等于改了队列内容，
    「只读」就成了空话。拷贝强度写清楚，不做「假装深拷贝」：

    - 信封（`id` / `from_` / `to` / `headers` / `meta` 等顶层字段）**总是**新对象；
    - `body` / `headers` / `meta` 尽量深拷贝；里面带函数、锁、连接之类拷不动的东西时，
      只能共享引用（不抛错）—— peek 保证的是「不出队、不建 in-flight、信封不被回写」，
      不是「body 绝对隔离」。

    :param message: 队列中保存的消息。
    :return: 与 `message` 不是同一个引用的副本（拷不动时退化为原对象）。
    """
    if message is None or isinstance(message, _ATOMIC):
        return message

    if isinstance(message, dict):
        return {key: _clone_or_share(value) for key, value in message.items()}

    if is_dataclass(message) and not isinstance(message, type):
        values = {f.name: _clone_or_share(getattr(message, f.name)) for f in fields(message)}
        try:
            return type(message)(**values)
        except TypeError:
            pass  # 字段不是构造参数（init=False 之类）：退回下面的通用浅拷贝路径

    try:
        clone = copy_module.copy(message)  # 新信封
    except Exception:
        return message  # 整个对象拷不动：共享引用，不谎称隔离

    for name in list(getattr(clone, "__dict__", {})):
        try:
            setattr(clone, name, _clone_or_share(getattr(clone, name)))
        except Exception:
            continue
    return clone
