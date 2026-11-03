"""库内错误。

`code` 是跨端口（TS `src/error.ts` / Python / Rust）对齐的错误码：调用方判断错误类型请用
`code` 或 `isinstance`，不要匹配 message —— message 是给人看的，会变。
"""

from typing import Optional


class MailboxError(Exception):
    """Mailbox 错误的公共基类（对齐 TS `MailboxError` / Rust `MailboxError`）。"""

    #: 稳定的错误码；子类覆盖。基类不设码。
    code: Optional[str] = None


class ProviderNotFound(MailboxError):
    """没有 provider 认领这个协议（对齐 TS `NoProviderError` / Rust `ProviderNotFound`）。"""

    code = "MBX_NO_PROVIDER"

    def __init__(self, protocol: str):
        super().__init__(f"No provider for protocol: {protocol}")
        self.protocol = protocol


#: 与 TS 端同名的别名：跨端口查文档时，两个名字都能找到同一个错误。
NoProviderError = ProviderNotFound


class InvalidMailboxAddressError(MailboxError):
    """地址不合法：无法解析成 URL，或者带着规范化会**静默丢掉**的部件（query / hash）。

    见 `utils.get_canonical_mailbox_address_identifier`：邮箱语义必须留在 path 里，
    query / hash 不参与寻址，因此带它们的地址直接被拒绝，而不是被「顺手裁掉」。
    """

    code = "MBX_INVALID_ADDRESS"

    def __init__(self, address: str, message: Optional[str] = None):
        super().__init__(message if message is not None else f"Invalid mailbox address: {address}")
        #: 出错时正在处理的地址（字符串形式，便于诊断页直接展示）。
        self.address = str(address)


#: 旧名（0.1.x 暴露过），保留为别名，避免已有调用方的 `except InvalidAddress` 失效。
InvalidAddress = InvalidMailboxAddressError


class InvalidFetchOptionsError(MailboxError):
    """`fetch()` 的选项自相矛盾或取值非法（例如 `peek=True` 与 `manual_ack=True` 同时出现、
    `limit` 为负数）。这类错误是调用方的代码问题，必须在调用点炸出来，不能被忽略。"""

    code = "MBX_INVALID_FETCH_OPTIONS"


class UnsupportedCapabilityError(MailboxError):
    """provider 不具备被请求的能力（例如对不支持只读 `peek` 的传输强要 peek）。

    为什么要有这个错误：把 `peek` 这类新选项交给不认识它的 provider，等于**静默退化成普通
    fetch** —— 调用方以为自己在只读快照，实际已经把消息出队了。宁可抛错。
    """

    code = "MBX_UNSUPPORTED_CAPABILITY"

    def __init__(self, protocol: str, capability: str):
        super().__init__(f"The '{protocol}' provider does not support '{capability}'.")
        self.protocol = protocol
        #: 缺失的能力名，例如 `'read-only peek'` / `'status'`。
        self.capability = capability
