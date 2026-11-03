"""
issue#4 第 1 点 + 第 3 点的后半段 —— status 必须诚实。
（与 TS 的 `src/mailbox.status-diagnostics.test.ts` 同一组不变量。）

- `status()` 对**任何**地址都回 `online` + 0，诊断页分不出「地址不存在」和「地址空闲」；
- `last_activity_time` 曾经缺字段，一次 JSON 序列化之后整个字段消失；
- 没有全量快照，诊断页只能一个地址一个地址地猜。
"""

import json
import unittest
from dataclasses import asdict

from mboxlabs.mailbox import (
    FetchOptions,
    InvalidMailboxAddressError,
    Mailbox,
    MailboxProvider,
    MailMessage,
    MemoryEventBus,
    MemoryProvider,
    OutgoingMail,
    get_canonical_mailbox_address_identifier,
)
from mboxlabs.mailbox.providers.queue import MailMessageQueue

SENDER = "mem://a@example.com/u"


def new_mailbox(provider):
    mailbox = Mailbox()
    mailbox.register_provider(provider)
    return mailbox


class NoStatusProvider(MailboxProvider):
    """既答不了 status，也枚举不了自己的地址（AMQP / SQS 这类消息不在本地的 broker）。"""

    def __init__(self, protocol="nostatus"):
        super().__init__(protocol)
        self.queued = []

    async def _send(self, message: MailMessage) -> None:
        self.queued.append(message)

    async def _subscribe(self, address, on_receive):
        return lambda: None

    async def _unsubscribe(self, subscription_id, handle) -> None:
        pass

    async def _fetch(self, address, options):
        return self.queued.pop(0) if self.queued else None


class TestStatusKnowsWhetherTheAddressExists(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = MemoryProvider.create_isolated()
        self.mailbox = new_mailbox(self.provider)
        await self.mailbox.start()

    async def asyncTearDown(self):
        await self.mailbox.stop(purge=True)

    async def test_an_address_that_was_never_touched_is_unknown(self):
        status = await self.mailbox.status("mem:not-even-a-valid-address")

        self.assertEqual(status.state, "unknown")
        self.assertIs(status.exists, False)
        self.assertEqual(status.unread_count, 0)
        self.assertEqual(status.in_flight_count, 0)
        self.assertEqual(status.extra["subscriber_count"], 0)
        self.assertIn("No queue, subscription or activity is known", status.extra["message"])

    async def test_the_two_examples_from_the_issue_are_no_longer_online(self):
        for address in ("mem:not-even-a-valid-address", "mem:anything@example.com/nope"):
            status = await self.mailbox.status(address)
            self.assertNotEqual(status.state, "online")
            self.assertIs(status.exists, False)

    async def test_drained_but_real_is_still_online(self):
        """「存在但空闲」与「不存在」是两回事。"""
        address = "mem://drained@example.com/inbox"
        await self.mailbox.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
        await self.mailbox.fetch(address, FetchOptions())

        status = await self.mailbox.status(address)
        self.assertIs(status.exists, True)
        self.assertEqual(status.state, "online")
        self.assertEqual(status.unread_count, 0)  # 空闲
        self.assertIsInstance(status.last_activity_time, str)

    async def test_an_address_with_only_a_subscriber_exists_too(self):
        """订阅就是「这个地址有人用」。"""
        address = "mem://subscribed-only@example.com/inbox"
        await self.mailbox.subscribe(address, lambda msg: None)

        status = await self.mailbox.status(address)
        self.assertIs(status.exists, True)
        self.assertEqual(status.state, "online")
        self.assertEqual(status.extra["subscriber_count"], 1)
        self.assertEqual(status.unread_count, 0)

    async def test_last_activity_time_is_none_not_a_missing_field(self):
        """`None`（JSON 里是 `null`）才能穿过序列化；字段消失 = 诊断页拿到「没有这个概念」。"""
        status = await self.mailbox.status("mem://json-roundtrip@example.com/inbox")

        self.assertIsNone(status.last_activity_time)

        wire = json.loads(json.dumps(asdict(status)))
        self.assertIn("last_activity_time", wire)
        self.assertIsNone(wire["last_activity_time"])
        self.assertIs(wire["exists"], False)
        self.assertEqual(wire["state"], "unknown")

    async def test_status_is_read_only_too(self):
        address = "mem://read-only-status@example.com/inbox"
        await self.mailbox.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))

        first = await self.mailbox.status(address)
        second = await self.mailbox.status(address)

        self.assertIsInstance(first.last_activity_time, str)
        self.assertEqual(second.last_activity_time, first.last_activity_time)

    async def test_in_flight_messages_keep_an_address_existing(self):
        address = "mem://inflight-only@example.com/inbox"
        await self.mailbox.post(OutgoingMail(from_=SENDER, to=address, body={"n": 1}))
        await self.mailbox.fetch(address, FetchOptions(manual_ack=True))

        status = await self.mailbox.status(address)
        self.assertIs(status.exists, True)
        self.assertEqual(status.unread_count, 0)
        self.assertEqual(status.in_flight_count, 1)

    async def test_clear_brings_the_address_back_to_unknown(self):
        """但仍有订阅者的地址继续算存在（issue#3：`clear()` 不拆订阅）。"""
        cleared = "mem://cleared@example.com/inbox"
        subscribed = "mem://cleared-but-subscribed@example.com/inbox"
        await self.mailbox.subscribe(subscribed, lambda msg: None)
        await self.mailbox.post(OutgoingMail(from_=SENDER, to=cleared, body={"n": 1}))
        await self.mailbox.post(OutgoingMail(from_=SENDER, to=subscribed, body={"n": 1}))

        await self.mailbox.clear()

        cleared_status = await self.mailbox.status(cleared)
        self.assertIs(cleared_status.exists, False)
        self.assertEqual(cleared_status.state, "unknown")

        subscribed_status = await self.mailbox.status(subscribed)
        self.assertIs(subscribed_status.exists, True)
        self.assertEqual(subscribed_status.extra["subscriber_count"], 1)

    async def test_a_provider_that_cannot_answer_status_does_not_claim_absence(self):
        """问不到 ≠ 不存在：`exists` 必须留空（None），不是 False。"""
        mailbox = new_mailbox(NoStatusProvider())

        status = await mailbox.status("nostatus:x@example.com/inbox")
        self.assertEqual(status.state, "unknown")
        self.assertIsNone(status.exists)
        self.assertEqual(status.extra["message"],
                         "The 'nostatus' provider does not support status queries.")

    async def test_address_rules_apply_to_status_too(self):
        with self.assertRaises(InvalidMailboxAddressError):
            await self.mailbox.status("mem://q@example.com/inbox?thread=1")
        with self.assertRaisesRegex(InvalidMailboxAddressError, "not a parseable URL"):
            await self.mailbox.status("definitely not a url")


class TestBusLevelExistsAndEnumeration(unittest.TestCase):
    """总线层的同一套语义（`MemoryEventBus.get_status` / `knows` / `list_topics`）。"""

    def test_unknown_topic_then_subscribe_then_unsubscribe(self):
        bus = MemoryEventBus()
        topic = "mem://bus-level/inbox"

        self.assertEqual(
            bus.get_status(topic),
            {
                "exists": False,
                "unread_count": 0,
                "in_flight_count": 0,
                "last_activity_time": None,
                "subscriber_count": 0,
            },
        )

        unsubscribe = bus.subscribe(topic, lambda msg: None)
        status = bus.get_status(topic)
        self.assertIs(status["exists"], True)
        self.assertEqual(status["subscriber_count"], 1)
        self.assertIsInstance(status["last_activity_time"], str)

        unsubscribe()
        self.assertTrue(bus.knows(topic))  # 订阅表清空后仍留着活动痕迹，地址仍算「存在」
        status = bus.get_status(topic)
        self.assertIs(status["exists"], True)
        self.assertEqual(status["subscriber_count"], 0)

    def test_knows_and_list_topics_reflect_what_the_bus_knows(self):
        bus = MemoryEventBus()
        b = "mem://list-b/inbox"
        a = "mem://list-a/inbox"

        bus.publish(b, MailMessage(id="b-1", from_=SENDER, to=b, body={}))
        bus.subscribe(a, lambda msg: None)

        self.assertTrue(bus.knows(a))
        self.assertFalse(bus.knows("mem://list-never/inbox"))
        self.assertEqual(bus.list_topics(), [a, b])

        bus.clear()
        self.assertEqual(bus.list_topics(), [a])  # 只剩订阅留下的痕迹


class TestQueueTopicPresence(unittest.TestCase):
    """`MailMessageQueue.has_topic()` / `topics()`：`exists` 与快照枚举的底层。"""

    def setUp(self):
        self.queue = MailMessageQueue()
        self.topic1 = "mem://queue-presence-a/inbox"
        self.topic2 = "mem://queue-presence-b/inbox"
        self.msg1 = MailMessage(id="q-1", from_=SENDER, to=self.topic1, body={})
        self.msg2 = MailMessage(id="q-2", from_=SENDER, to=self.topic2, body={})

    def test_has_topic_survives_the_trip_through_in_flight(self):
        self.assertFalse(self.queue.has_topic(self.topic1))

        self.queue.enqueue(self.topic1, self.msg1)
        self.assertTrue(self.queue.has_topic(self.topic1))

        self.queue.dequeue_for_ack(self.topic1, None, lambda m: m.id)
        self.assertTrue(self.queue.has_topic(self.topic1))  # 只是离开队列，并未处理完

        self.queue.ack("q-1")
        self.assertFalse(self.queue.has_topic(self.topic1))

    def test_topics_lists_only_topics_holding_messages_sorted(self):
        self.queue.enqueue(self.topic2, self.msg2)
        self.queue.enqueue(self.topic1, self.msg1)
        self.queue.enqueue(self.topic1, MailMessage(id="q-3", from_=SENDER, to=self.topic1, body={}))
        self.assertEqual(self.queue.topics(), [self.topic1, self.topic2])

        self.queue.clear(self.topic1)
        self.assertEqual(self.queue.topics(), [self.topic2])

        self.queue.dequeue_for_ack(self.topic2, None, lambda m: m.id)
        self.assertEqual(self.queue.topics(), [self.topic2])  # in-flight 也算这个 topic 还有痕迹


class TestSnapshot(unittest.IsolatedAsyncioTestCase):
    """issue#4 要求的配套能力：不传地址的 `status()` = 全量 topic 快照。"""

    async def test_lists_every_topic_sorted_by_canonical_id_with_aggregates(self):
        mailbox = new_mailbox(MemoryProvider.create_isolated())
        await mailbox.start()

        await mailbox.post(OutgoingMail(from_=SENDER, to="mem://snapshot-b@example.com/inbox", body={"n": 1}))
        await mailbox.post(OutgoingMail(from_=SENDER, to="mem://snapshot-b@example.com/inbox", body={"n": 2}))
        await mailbox.post(OutgoingMail(from_=SENDER, to="mem://snapshot-a@example.com/inbox", body={"n": 3}))
        await mailbox.fetch("mem://snapshot-b@example.com/inbox", FetchOptions(manual_ack=True))

        snap = await mailbox.status()

        self.assertEqual(snap.state, "online")
        self.assertEqual(snap.protocols, ["mem"])
        self.assertEqual(snap.address_count, 2)
        self.assertEqual(snap.unread_count, 2)  # snapshot-a: 1 + snapshot-b: 2 - 1 (in-flight)
        self.assertEqual(snap.in_flight_count, 1)
        self.assertEqual(
            [entry.canonical_id for entry in snap.addresses],
            ["mem://snapshot-a@example.com/inbox", "mem://snapshot-b@example.com/inbox"],
        )
        self.assertEqual(snap.addresses[0].protocol, "mem")
        self.assertEqual(snap.addresses[0].state, "online")
        self.assertIs(snap.addresses[0].exists, True)
        self.assertEqual(snap.addresses[0].unread_count, 1)
        self.assertEqual(snap.addresses[0].in_flight_count, 0)
        self.assertEqual(snap.addresses[1].state, "online")
        self.assertEqual(snap.addresses[1].unread_count, 1)
        self.assertEqual(snap.addresses[1].in_flight_count, 1)
        self.assertIsInstance(snap.taken_at, str)

    async def test_an_empty_mailbox_still_produces_a_readable_snapshot(self):
        snap = await Mailbox().status()

        self.assertEqual(snap.state, "unknown")
        self.assertEqual(snap.protocols, [])
        self.assertEqual(snap.address_count, 0)
        self.assertEqual(snap.addresses, [])
        self.assertEqual(snap.unsupported_providers, [])

    async def test_provider_registered_but_no_topics_is_online_not_unknown(self):
        mailbox = new_mailbox(MemoryProvider.create_isolated())

        snap = await mailbox.status()
        self.assertEqual(snap.state, "online")
        self.assertEqual(snap.address_count, 0)
        self.assertEqual(snap.addresses, [])

    async def test_providers_that_cannot_enumerate_degrade_the_snapshot(self):
        """不能枚举的 provider 记在 `unsupported_providers`，而不是被当成「队列为空」。"""
        mailbox = Mailbox()
        mailbox.register_provider(MemoryProvider.create_isolated())
        mailbox.register_provider(NoStatusProvider("remote"))
        await mailbox.post(OutgoingMail(from_=SENDER, to="mem://snap-visible@example.com/inbox", body={}))
        await mailbox.post(OutgoingMail(from_="remote:a@example.com/u", to="remote:snap-hidden@example.com/inbox", body={}))

        snap = await mailbox.status()

        self.assertEqual(snap.protocols, ["mem", "remote"])
        self.assertEqual(snap.unsupported_providers, ["remote"])
        self.assertEqual(snap.state, "degraded")
        self.assertEqual(snap.address_count, 1)  # 只有 mem 那份能看见
        self.assertEqual(snap.addresses[0].protocol, "mem")

    async def test_snapshot_and_argumentless_status_are_the_same_view(self):
        mailbox = new_mailbox(MemoryProvider.create_isolated())
        await mailbox.post(OutgoingMail(from_=SENDER, to="mem://snapshot-eq@example.com/inbox", body={}))

        via_status = await mailbox.status()
        via_method = await mailbox.snapshot()

        # taken_at 是快照时刻，两次调用必然不同；其余字段必须一致
        self.assertIsInstance(via_status.taken_at, str)
        self.assertIsInstance(via_method.taken_at, str)
        self.assertEqual(
            {**asdict(via_status), "taken_at": None},
            {**asdict(via_method), "taken_at": None},
        )

    async def test_a_snapshot_only_covers_this_mailboxs_providers(self):
        a = new_mailbox(MemoryProvider.create_isolated())
        b = new_mailbox(MemoryProvider.create_isolated())

        await a.post(OutgoingMail(from_=SENDER, to="mem://snapshot-isolation@example.com/inbox", body={}))

        self.assertEqual((await a.status()).address_count, 1)
        self.assertEqual((await b.status()).address_count, 0)
        self.assertEqual((await b.status()).state, "online")

    async def test_every_snapshot_entry_can_be_queried_back_by_canonical_id(self):
        mailbox = new_mailbox(MemoryProvider.create_isolated())
        address = "mem://snapshot-roundtrip@example.com/inbox/thread-1"
        await mailbox.post(OutgoingMail(from_=SENDER, to=address, body={}))

        entry = (await mailbox.status()).addresses[0]
        direct = await mailbox.status(entry.canonical_id)

        self.assertEqual(entry.canonical_id, get_canonical_mailbox_address_identifier(address))
        self.assertEqual(direct.unread_count, entry.unread_count)
        self.assertEqual(direct.state, entry.state)


if __name__ == "__main__":
    unittest.main()
