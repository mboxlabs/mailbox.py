"""测试辅助：唯一 topic、屏蔽 provider 的日志输出、共用订阅者桩。"""

import contextlib
import io
import itertools

from mboxlabs.mailbox.providers.memory import MemoryEventBus

_counter = itertools.count(1)


def addr_of(name: str) -> str:
    """默认总线是进程级共享的：用默认 provider 的用例必须用唯一 topic。

    需要真隔离时用 `MemoryProvider.create_isolated()`（issue#3），见 `test_memory_isolation.py`。
    """
    return "mem:py{}-{}@bus/inbox".format(next(_counter), name)


@contextlib.contextmanager
def quiet():
    """屏蔽基类的 error / warn 输出（与 TS 测试里的 `vi.spyOn(console, 'error')` 对应）。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


class TransientError(Exception):
    """可重试错误（与 TS 的 `isRetriable: true` 对齐）。"""
    is_retriable = True


def failing():
    """抛可重试错误的订阅者。"""
    async def handler(_message):
        raise TransientError("upstream down")
    return handler


def reset_bus():
    """清掉进程级单例总线里的积压消息（含 in-flight）与活动时间。

    issue#3 之后走公开的 `MemoryEventBus.clear()`：以前这里直接掏私有字段
    （`bus.queue.queues` / `bus.queue.in_flight`），队列结构一改测试就碎。

    订阅表**不**清：`clear()` 不拆订阅是有意语义（否则旧 `Subscription` 会变成“报 active
    但再也收不到消息”的僵尸）。用例之间不串台靠 `addr_of()` 的唯一 topic。
    """
    MemoryEventBus.get_instance().clear()
