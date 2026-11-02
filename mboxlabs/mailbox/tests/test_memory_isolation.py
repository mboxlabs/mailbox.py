"""
issue#3 —— `MemoryEventBus` 的实例边界与状态清理。
（与 TS 的 `src/providers/memory.isolation.test.ts` 同一组不变量。）

要锁住两件事：
1. **默认行为不变**：不带参数构造的 `MemoryProvider` 仍然共享进程级总线（"模拟服务端"语义），
   `stop()` 默认仍然不清队列。已有用户依赖这两点，所以这里连"共享带来的串台"也断言下来，
   而不是只断言隔离路径。
2. **可隔离、可清理**：需要隔离时有明确入口（私有总线 / scope / 注入总线），需要清理时有
   `MailMessageQueue.clear()` → `MemoryEventBus.clear()` → `MemoryProvider.clear()` →
   `Mailbox.clear()` / `Mailbox.stop(purge=True)` 这条完整链路。

命名标记：
  test_semantics_*  既有语义（共享总线），破坏它们就是回归。
  test_isolation_*  隔离入口。
  test_clear_*      清理链路。
  test_reuse_*      同一个 provider 实例被多个 Mailbox 复用。
"""

import asyncio
import itertools
import unittest

from mboxlabs.mailbox import (
    DEFAULT_MEMORY_SCOPE,
    FetchOptions,
    Mailbox,
    MailboxProvider,
    MailMessage,
    MemoryEventBus,
    MemoryProvider,
    OutgoingMail,
    RetryPolicy,
)
from mboxlabs.mailbox.tests.helpers import TransientError, addr_of, failing, quiet
from mboxlabs.mailbox.utils import get_canonical_mailbox_address_identifier

SENDER = "mem://sender@example.com/outbox"

#: 用例之间不能撞 scope：注册表不会自动回收，但至少不能互相看到。
_scope_seq = itertools.count(1)


def settle(seconds: float = 0.03) -> float:
    """投递是 fire-and-forget：让回调跑完再断言。用法 `await asyncio.sleep(settle())`。"""
    return seconds


def scope_of(name: str) -> str:
    return "{}-{}".format(name, next(_scope_seq))


def bus_msg(msg_id: str, topic: str) -> MailMessage:
    """直接喂给 `MemoryEventBus` 的裸消息（跳过 provider 层）。"""
    return MailMessage(id=msg_id, from_="mem://probe@example.com/out", to=topic, body={"id": msg_id})


def new_mailbox(provider: MailboxProvider) -> Mailbox:
    mailbox = Mailbox()
    mailbox.register_provider(provider)
    return mailbox


def reuse_warnings(output: str) -> list:
    """只统计 issue#3 相关的那条告警，避免被无关噪音放过 / 误判。"""
    return [line for line in output.splitlines() if "shared by more than one Mailbox" in line]


def bus_counts(bus: MemoryEventBus, topic: str) -> tuple:
    """总线状态里的 (unread, in_flight, subscribers)。"""
    status = bus.get_status(topic)
    return (status["unread_count"], status["in_flight_count"], status["subscriber_count"])


def status_counts(status) -> tuple:
    """`MailboxStatus` 里的 (unread, in_flight, subscribers)。"""
    return (status.unread_count, status.in_flight_count, status.extra["subscriber_count"])


class TestBusInstancesAndRegistry(unittest.IsolatedAsyncioTestCase):
    """`MemoryEventBus` 的实例与 scope 注册表。"""

    def test_semantics_get_instance_without_scope_is_the_shared_bus(self):
        shared = MemoryEventBus.get_instance()

        self.assertIs(shared, MemoryEventBus.get_instance())
        self.assertEqual(DEFAULT_MEMORY_SCOPE, "")
        self.assertEqual(shared.scope, DEFAULT_MEMORY_SCOPE)
        self.assertIs(MemoryEventBus.get_instance(""), shared)  # 显式空串 = 默认那份

    def test_get_instance_is_a_registry_per_scope(self):
        a1 = MemoryEventBus.get_instance("suite-a")
        a2 = MemoryEventBus.get_instance("suite-a")
        b = MemoryEventBus.get_instance("suite-b")

        self.assertIs(a1, a2)
        self.assertIsNot(a1, b)
        self.assertIsNot(a1, MemoryEventBus.get_instance())
        self.assertEqual(a1.scope, "suite-a")
        self.assertEqual(b.scope, "suite-b")

    def test_directly_constructed_buses_are_not_registered(self):
        own = MemoryEventBus()
        other = MemoryEventBus()
        labeled = MemoryEventBus("labeled")

        self.assertIsNot(own, other)
        self.assertIsNot(own, MemoryEventBus.get_instance())  # 手工实例 ≠ 默认单例
        self.assertEqual(own.scope, DEFAULT_MEMORY_SCOPE)
        self.assertEqual(labeled.scope, "labeled")
        self.assertIsNot(MemoryEventBus.get_instance("labeled"), labeled)  # 注册表里另起一份

    async def test_different_buses_share_nothing(self):
        x = MemoryEventBus()
        y = MemoryEventBus()
        topic = "mem://same-topic-both-buses/inbox"  # 同名 topic，故意撞

        hits = {"n": 0}

        async def listener(_message):
            hits["n"] += 1

        x.subscribe(topic, listener)

        deliveries = x.publish(topic, bus_msg("x-1", topic))
        self.assertEqual(len(deliveries), 1)  # x 上有订阅者：走广播
        await asyncio.gather(*deliveries)
        self.assertEqual(hits["n"], 1)

        # y 上没人订阅同一个 topic：publish 只能入队，绝不会被 x 的订阅者接走
        self.assertIsNone(y.publish(topic, bus_msg("y-1", topic)))
        self.assertEqual(bus_counts(y, topic), (1, 0, 0))
        self.assertEqual(bus_counts(x, topic), (0, 0, 1))

        # 取消息也不会跨 bus
        self.assertEqual(y.fetch_and_forget(topic).id, "y-1")
        self.assertIsNone(x.fetch_and_forget(topic))

        # in-flight 也是各自一份
        y.publish(topic, bus_msg("y-2", topic))
        self.assertEqual(y.fetch_for_ack(topic, 1000).id, "y-2")
        self.assertEqual(y.get_status(topic)["in_flight_count"], 1)
        self.assertEqual(x.get_status(topic)["in_flight_count"], 0)

        # last_activity 各自一份：y 没碰过的 topic 在 y 上是空的
        elsewhere = "mem://only-on-x/inbox"
        x.publish(elsewhere, bus_msg("x-2", elsewhere))
        self.assertIsInstance(x.get_status(elsewhere)["last_activity_time"], str)
        self.assertIsNone(y.get_status(elsewhere)["last_activity_time"])

    async def test_semantics_two_providers_on_one_bus_still_talk(self):
        bus = MemoryEventBus()
        address = addr_of("shared-bus")
        A = new_mailbox(MemoryProvider(bus=bus))
        B = new_mailbox(MemoryProvider(bus=bus))
        await A.start()
        await B.start()

        hits = {"n": 0}

        async def on_receive(_message):
            hits["n"] += 1

        await B.subscribe(address, on_receive)
        await A.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
        await asyncio.sleep(settle())

        self.assertEqual(hits["n"], 1)
        # 订阅表是这份 bus 共有的
        self.assertEqual((await A.status(address)).extra["subscriber_count"], 1)


class TestProviderIsolation(unittest.IsolatedAsyncioTestCase):
    """`MemoryProvider` 的隔离入口。"""

    async def test_semantics_default_construction_shares_the_process_bus(self):
        address = addr_of("default-shared")
        a = MemoryProvider()
        b = MemoryProvider()
        self.assertIs(a.bus, MemoryEventBus.get_instance())
        self.assertIs(a.bus, b.bus)

        A = new_mailbox(a)
        B = new_mailbox(b)
        await A.start()
        await B.start()

        hits = {"n": 0}

        async def on_receive(_message):
            hits["n"] += 1

        await B.subscribe(address, on_receive)
        await A.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
        await asyncio.sleep(settle())

        self.assertEqual(hits["n"], 1)

    async def test_semantics_default_bus_queues_are_shared_too(self):
        address = addr_of("default-steal")
        A = new_mailbox(MemoryProvider())
        B = new_mailbox(MemoryProvider())
        await A.start()
        await B.start()

        await A.post(OutgoingMail(from_=SENDER, to=address, body={"from": "A"}))

        # 共享 bus 的既有语义：B 能 fetch 走 A 的消息
        self.assertEqual((await B.fetch(address, FetchOptions())).body, {"from": "A"})
        self.assertEqual((await A.status(address)).unread_count, 0)

    async def test_isolation_create_isolated_has_no_crosstalk(self):
        address = addr_of("isolated-broadcast")
        A = new_mailbox(MemoryProvider.create_isolated())
        B = new_mailbox(MemoryProvider.create_isolated())
        await A.start()
        await B.start()

        hits = {"n": 0}

        async def on_receive(_message):
            hits["n"] += 1

        await B.subscribe(address, on_receive)
        await A.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
        await asyncio.sleep(settle())

        self.assertEqual(hits["n"], 0)
        self.assertEqual(status_counts(await B.status(address)), (0, 0, 1))  # B 自己的订阅还在
        self.assertEqual((await A.status(address)).extra["subscriber_count"], 0)  # A 看不见 B 的订阅

    async def test_isolation_create_isolated_queues_are_private(self):
        address = addr_of("isolated-queue")
        A = new_mailbox(MemoryProvider.create_isolated())
        B = new_mailbox(MemoryProvider.create_isolated())
        await A.start()
        await B.start()

        await A.post(OutgoingMail(from_=SENDER, to=address, body={"from": "A"}))

        self.assertIsNone(await B.fetch(address, FetchOptions()))
        self.assertEqual((await B.status(address)).unread_count, 0)
        self.assertEqual((await A.fetch(address, FetchOptions())).body, {"from": "A"})

    def test_isolation_create_isolated_allocates_a_new_bus_each_time(self):
        a = MemoryProvider.create_isolated()
        b = MemoryProvider.create_isolated()

        self.assertIsNot(a.bus, b.bus)
        self.assertIsNot(a.bus, MemoryEventBus.get_instance())
        self.assertEqual(a.bus.scope, DEFAULT_MEMORY_SCOPE)  # 私有 bus 不属于任何 scope

    async def test_isolation_create_isolated_stop_is_local(self):
        address = addr_of("isolated-stop")
        A = new_mailbox(MemoryProvider.create_isolated())
        B = new_mailbox(MemoryProvider.create_isolated())
        await A.start()
        await B.start()

        hits = {"n": 0}

        async def on_receive(_message):
            hits["n"] += 1

        await B.subscribe(address, on_receive)
        await A.stop()
        await B.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
        await asyncio.sleep(settle())

        self.assertEqual(hits["n"], 1)

    async def test_isolation_create_isolated_keeps_the_retry_policy(self):
        address = addr_of("isolated-retry")
        provider = MemoryProvider.create_isolated(RetryPolicy(max_attempts=3, base_delay=0.005))
        A = new_mailbox(provider)
        B = new_mailbox(MemoryProvider.create_isolated())
        await A.start()
        await B.start()

        calls = {"n": 0}

        async def on_receive(_message):
            calls["n"] += 1
            raise TransientError("upstream down")

        with quiet():
            await A.subscribe(address, on_receive)
            await A.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
            await asyncio.sleep(0.2)

        self.assertEqual(calls["n"], 3)  # max_attempts=3：三次尝试后放弃
        self.assertEqual((await A.status(address)).unread_count, 1)  # 回自己的队列，由 fetch 兜底
        self.assertEqual((await B.status(address)).unread_count, 0)  # 别的实例看不到

    async def test_isolation_requeue_after_failure_stays_on_own_bus(self):
        address = addr_of("isolated-requeue")
        A = new_mailbox(MemoryProvider.create_isolated())
        B = new_mailbox(MemoryProvider.create_isolated())
        await A.start()
        await B.start()

        with quiet():
            await A.subscribe(address, failing())
            await A.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
            await asyncio.sleep(settle())
            # A 自己能取回
            self.assertEqual((await A.fetch(address, FetchOptions())).body, {"n": 1})

        self.assertEqual((await B.status(address)).unread_count, 0)

    async def test_isolation_for_scope_namespaces_the_bus(self):
        scope = scope_of("suite")
        other_scope = scope_of("suite")
        address = addr_of("scoped")

        provider_a = MemoryProvider.for_scope(scope)
        provider_b = MemoryProvider.for_scope(scope)
        A = new_mailbox(provider_a)
        B = new_mailbox(provider_b)
        Elsewhere = new_mailbox(MemoryProvider.for_scope(other_scope))
        Default = new_mailbox(MemoryProvider())
        await A.start()
        await B.start()
        await Elsewhere.start()
        await Default.start()

        self.assertIs(provider_a.bus, provider_b.bus)
        self.assertIs(provider_a.bus, MemoryEventBus.get_instance(scope))
        self.assertIsNot(provider_a.bus, MemoryEventBus.get_instance())
        self.assertEqual(provider_a.bus.scope, scope)

        hits = {"n": 0}

        async def on_receive(_message):
            hits["n"] += 1

        await B.subscribe(address, on_receive)
        await A.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
        await asyncio.sleep(settle())

        self.assertEqual(hits["n"], 1)
        self.assertEqual(status_counts(await Elsewhere.status(address)), (0, 0, 0))
        self.assertEqual((await Default.status(address)).unread_count, 0)

    def test_isolation_injected_bus_is_used_as_is(self):
        bus = MemoryEventBus()

        self.assertIs(MemoryProvider(bus=bus).bus, bus)
        self.assertIs(MemoryProvider(RetryPolicy(max_attempts=2, base_delay=1), bus).bus, bus)
        self.assertIs(MemoryProvider().bus, MemoryEventBus.get_instance())
        self.assertIs(MemoryProvider(None).bus, MemoryEventBus.get_instance())
        self.assertIs(MemoryProvider(None, None).bus, MemoryEventBus.get_instance())


class TestClearChain(unittest.IsolatedAsyncioTestCase):
    """清理：MailMessageQueue → MemoryEventBus → provider → Mailbox。"""

    async def test_clear_bus_topic_drops_queued_and_in_flight_only(self):
        bus = MemoryEventBus()
        mailbox = new_mailbox(MemoryProvider(bus=bus))
        await mailbox.start()

        kept = addr_of("bus-clear-kept")
        dropped = addr_of("bus-clear-dropped")
        await mailbox.post(OutgoingMail(from_=SENDER, to=kept, body={"n": 1}))
        await mailbox.post(OutgoingMail(from_=SENDER, to=dropped, body={"n": 1}))
        await mailbox.post(OutgoingMail(from_=SENDER, to=dropped, body={"n": 2}))

        await mailbox.fetch(dropped, FetchOptions(manual_ack=True, ack_timeout=1000))
        self.assertEqual((await mailbox.status(dropped)).in_flight_count, 1)

        # 总线按 canonical topic 记账，所以 clear 也要传 canonical 形式
        dropped_topic = get_canonical_mailbox_address_identifier(dropped)
        self.assertEqual(bus.clear(dropped_topic), 2)  # 1 条等待 + 1 条 in-flight
        self.assertEqual(bus_counts(bus, dropped_topic), (0, 0, 0))
        self.assertIsNone(bus.get_status(dropped_topic)["last_activity_time"])
        self.assertEqual((await mailbox.status(kept)).unread_count, 1)

    def test_clear_bus_makes_late_ack_and_nack_no_ops(self):
        bus = MemoryEventBus()
        topic = "mem://late-settlement/inbox"
        bus.publish(topic, bus_msg("late-1", topic))

        inflight = bus.fetch_for_ack(topic, 1000)
        self.assertEqual(inflight.id, "late-1")
        self.assertEqual(bus.clear(topic), 1)

        bus.ack("late-1")         # 消费侧的迟到 ack
        bus.nack("late-1", True)  # 迟到的 nack(requeue)

        status = bus.get_status(topic)
        self.assertEqual(status["unread_count"], 0)
        self.assertEqual(status["in_flight_count"], 0)
        self.assertIsNone(bus.fetch_and_forget(topic))

    def test_clear_bus_all_topics_is_idempotent(self):
        bus = MemoryEventBus()
        a = "mem://clear-all-a/inbox"
        b = "mem://clear-all-b/inbox"

        bus.publish(a, bus_msg("a-1", a))
        bus.publish(b, bus_msg("b-1", b))
        bus.publish(b, bus_msg("b-2", b))
        bus.fetch_for_ack(b, 1000)

        self.assertEqual(bus.clear(), 3)  # 2 条等待 + 1 条 in-flight
        self.assertEqual(bus_counts(bus, a), (0, 0, 0))
        self.assertIsNone(bus.get_status(a)["last_activity_time"])
        self.assertEqual(bus_counts(bus, b), (0, 0, 0))
        self.assertIsNone(bus.get_status(b)["last_activity_time"])
        self.assertEqual(bus.clear(), 0)
        self.assertEqual(bus.clear("never-seen"), 0)

    async def test_clear_keeps_subscriptions(self):
        bus = MemoryEventBus()
        topic = "mem://clear-keeps-listener/inbox"

        hits = {"n": 0}

        async def listener(_message):
            hits["n"] += 1

        bus.subscribe(topic, listener)
        await asyncio.gather(*bus.publish(topic, bus_msg("before", topic)))
        self.assertEqual(hits["n"], 1)

        self.assertEqual(bus.clear(topic), 0)
        self.assertEqual(bus.get_status(topic)["subscriber_count"], 1)  # 订阅表未被拆

        await asyncio.gather(*bus.publish(topic, bus_msg("after", topic)))
        self.assertEqual(hits["n"], 2)

    async def test_clear_provider_only_clears_its_own_bus(self):
        address = addr_of("provider-clear")
        A = new_mailbox(MemoryProvider.create_isolated())
        B = new_mailbox(MemoryProvider.create_isolated())
        await A.start()
        await B.start()

        await A.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
        await B.post(OutgoingMail(from_=SENDER, to=address, body={"n": 2}))

        await A.clear()

        self.assertEqual((await A.status(address)).unread_count, 0)
        self.assertEqual((await B.status(address)).unread_count, 1)

    async def test_clear_mailbox_calls_clear_on_every_provider(self):
        class CountingProvider(MemoryProvider):
            def __init__(self, protocol="mem"):
                super().__init__(bus=MemoryEventBus())  # 别去动进程级共享总线
                self.protocol = protocol
                self.clear_calls = 0

            async def clear(self):
                self.clear_calls += 1
                await super().clear()

        mem = CountingProvider()
        other = CountingProvider(protocol="other")
        mailbox = Mailbox()
        mailbox.register_provider(mem)
        mailbox.register_provider(other)

        await mailbox.clear()

        self.assertEqual(mem.clear_calls, 1)
        self.assertEqual(other.clear_calls, 1)

    async def test_clear_without_providers_and_twice_is_fine(self):
        mailbox = new_mailbox(MemoryProvider.create_isolated())
        await mailbox.start()
        await mailbox.post(OutgoingMail(from_=SENDER, to=addr_of("clear-idempotent"), body={"n": 1}))

        await mailbox.clear()
        await mailbox.clear()
        await Mailbox().clear()  # 空 Mailbox：没有 provider 可清

    async def test_clear_mailbox_keeps_subscriptions(self):
        mailbox = new_mailbox(MemoryProvider.create_isolated())
        await mailbox.start()

        queued = addr_of("keep-sub-queue")
        live = addr_of("keep-sub-live")

        hits = {"n": 0}

        async def on_receive(_message):
            hits["n"] += 1

        sub = await mailbox.subscribe(live, on_receive)

        await mailbox.post(OutgoingMail(from_=SENDER, to=queued, body={"n": 1}))
        self.assertEqual((await mailbox.status(queued)).unread_count, 1)

        await mailbox.clear()

        self.assertEqual((await mailbox.status(queued)).unread_count, 0)
        self.assertEqual(sub.status, "active")

        await mailbox.post(OutgoingMail(from_=SENDER, to=live, body={"n": 2}))
        await asyncio.sleep(settle())
        self.assertEqual(hits["n"], 1)

    async def test_semantics_stop_keeps_the_backlog(self):
        for purge in (None, False):  # `stop()` 与 `stop(purge=False)` 都是老行为
            scope = scope_of("legacy")
            address = addr_of("stop-keeps-queue")

            first = new_mailbox(MemoryProvider.for_scope(scope))
            await first.start()
            await first.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
            if purge is None:
                await first.stop()
            else:
                await first.stop(purge=purge)

            second = new_mailbox(MemoryProvider.for_scope(scope))
            await second.start()
            self.assertEqual((await second.status(address)).unread_count, 1)
            await second.clear()  # 收尾，别把残留留给下一个用例

    async def test_clear_stop_with_purge_closes_subscriptions_then_purges(self):
        mailbox = new_mailbox(MemoryProvider.create_isolated())
        await mailbox.start()

        live = addr_of("stop-purge-live")
        queued = addr_of("stop-purge-queue")

        sub = await mailbox.subscribe(live, lambda _message: None)
        await mailbox.post(OutgoingMail(from_=SENDER, to=live, body={"n": 0}))  # 有订阅者：走广播，不入队
        await asyncio.sleep(settle())
        self.assertEqual((await mailbox.status(live)).unread_count, 0)

        await mailbox.post(OutgoingMail(from_=SENDER, to=queued, body={"n": 1}))
        await mailbox.post(OutgoingMail(from_=SENDER, to=queued, body={"n": 2}))
        await mailbox.fetch(queued, FetchOptions(manual_ack=True, ack_timeout=1000))
        self.assertEqual((await mailbox.status(queued)).unread_count, 1)
        self.assertEqual((await mailbox.status(queued)).in_flight_count, 1)

        await mailbox.stop(purge=True)

        self.assertEqual(sub.status, "closed")
        self.assertEqual((await mailbox.status(queued)).unread_count, 0)
        self.assertEqual((await mailbox.status(queued)).in_flight_count, 0)
        self.assertEqual((await mailbox.status(live)).unread_count, 0)

    async def test_semantics_clear_on_a_shared_bus_is_scope_wide(self):
        scope = scope_of("shared-purge")
        address = addr_of("shared-purge")
        A = new_mailbox(MemoryProvider.for_scope(scope))
        B = new_mailbox(MemoryProvider.for_scope(scope))
        await A.start()
        await B.start()

        await B.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
        await A.clear()

        self.assertEqual((await B.status(address)).unread_count, 0)

    async def test_base_provider_clear_is_a_no_op(self):
        class NoPurgeProvider(MailboxProvider):
            def __init__(self):
                super().__init__("nopurge")

            async def _send(self, message):
                pass

            async def _subscribe(self, address, on_receive):
                return lambda: None

            async def _unsubscribe(self, subscription_id, handle):
                pass

            async def _fetch(self, address, options):
                return None

        provider = NoPurgeProvider()
        self.assertIsNone(await provider.clear())  # 基类默认实现

        mailbox = new_mailbox(provider)
        await mailbox.start()
        self.assertIsNone(await mailbox.clear())
        self.assertIsNone(await mailbox.stop(purge=True))


class TestSharedProviderReuse(unittest.IsolatedAsyncioTestCase):
    """同一个 provider 实例被多个 Mailbox 复用。"""

    async def test_reuse_warns_once_and_tears_down_the_other_subscription(self):
        shared = MemoryProvider()

        with quiet() as output:
            A = new_mailbox(shared)
            B = new_mailbox(shared)
            C = new_mailbox(shared)  # 第三个复用者也不能再吵一遍
            new_mailbox(shared)     # 注册表已记下 owner，后面的复用同样闭嘴
        self.assertEqual(len(reuse_warnings(output.getvalue())), 1)

        await A.start()
        await B.start()
        await C.start()

        address = addr_of("shared-provider-reuse")
        hits = {"n": 0}

        async def on_receive(_message):
            hits["n"] += 1

        with quiet():
            await B.subscribe(address, on_receive)
            await A.stop()  # 只停 A
            await B.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
            await asyncio.sleep(settle())

        # 订阅表挂在 provider 上，`A.stop()` 拆掉的就是它的全部订阅。
        self.assertEqual(hits["n"], 0)

    async def test_reuse_does_not_warn_for_normal_uses(self):
        provider = MemoryProvider()
        mailbox = Mailbox()

        with quiet() as output:
            mailbox.register_provider(provider)  # 同一个 Mailbox 内重复注册是常规用法
            mailbox.register_provider(provider)
            mailbox.register_provider(provider)

            A = new_mailbox(MemoryProvider())
            B = new_mailbox(MemoryProvider())
            await A.start()
            await B.start()
            await A.stop()
            await B.stop()

        self.assertEqual(reuse_warnings(output.getvalue()), [])

    def test_reuse_mark_does_not_pollute_the_provider(self):
        provider = MemoryProvider()
        attributes_before = set(vars(provider))

        with quiet() as output:
            Mailbox().register_provider(provider)
            Mailbox().register_provider(provider)

        self.assertEqual(set(vars(provider)), attributes_before)  # 标记不落进 provider.__dict__
        self.assertEqual(len(reuse_warnings(output.getvalue())), 1)
