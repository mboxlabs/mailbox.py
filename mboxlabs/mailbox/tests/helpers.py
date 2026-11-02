"""测试辅助：唯一 topic、屏蔽 provider 的日志输出、共用订阅者桩。"""

import contextlib
import io
import itertools

from mboxlabs.mailbox.providers.memory import MemoryEventBus

_counter = itertools.count(1)


def addr_of(name: str) -> str:
    """MemoryEventBus 是进程级单例：每个用例必须用唯一 topic。"""
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
    """清掉进程级单例总线里的 topic 状态（每个用例独立）。"""
    bus = MemoryEventBus.get_instance()
    bus.topics.clear()
    bus.queue.queues.clear()
    bus.queue.in_flight.clear()
    bus.last_activity.clear()
