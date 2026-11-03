"""
issue#4 第 3 点 —— 缺只读语义（`peek`）。
（与 TS 的 `src/mailbox.peek.test.ts` + `src/lib/MessageQueue.test.ts` 的 peek 块同一组不变量。）

之前最接近的做法是 `fetch(addr, FetchOptions(manual_ack=True))` 然后不 ack —— 消息会进 in-flight
（默认 30s 超时后重投），语义上不是只读，还会干扰正常消费者的 `unread_count` / `in_flight_count`。

现在 `fetch(addr, FetchOptions(peek=True, limit=n))` 返回副本快照：不出队、不建 in-flight、
不刷活动时间；provider 不支持时明确报错，不会静默退化成普通取信。
"""

import math
import time
import unittest

from mboxlabs.mailbox import (
    AckableMessage,
    FetchOptions,
    InvalidFetchOptionsError,
    InvalidMailboxAddressError,
    Mailbox,
    MailMessage,
    MemoryEventBus,
    MemoryProvider,
    MailboxProvider,
    OutgoingMail,
    UnsupportedCapabilityError,
    copy_for_peek,
    normalize_peek_limit,
)
from mboxlabs.mailbox.providers.queue import MailMessageQueue

SENDER = "mem://a@example.com/u"


def queued(msg_id: str, to: str, body) -> MailMessage:
    """直接喂给队列 / 总线的裸消息（跳过 Mailbox 层）。"""
    return MailMessage(id=msg_id, from_=SENDER, to=to, body=body)


class TestNormalizePeekLimit(unittest.TestCase):
    def test_missing_and_infinity_mean_the_whole_queue(self):
        self.assertIsNone(normalize_peek_limit(None))
        self.assertIsNone(normalize_peek_limit(math.inf))
        self.assertEqual(normalize_peek_limit(0), 0)

    def test_negative_fractional_and_nan_are_caller_bugs(self):
        for bad in (-1, -math.inf, 2.5, math.nan, True, "3"):
            with self.assertRaises(InvalidFetchOptionsError):
                normalize_peek_limit(bad)

    def test_the_error_carries_a_stable_code(self):
        with self.assertRaises(InvalidFetchOptionsError) as caught:
            normalize_peek_limit(-3)
        self.assertEqual(caught.exception.code, "MBX_INVALID_FETCH_OPTIONS")


class TestCopyForPeek(unittest.TestCase):
    def test_envelope_and_clonable_body_are_new_references(self):
        message = MailMessage(
            id="m-1",
            from_="mem://a@example.com/u",
            to="mem://b@example.com/inbox",
            body={"deep": {"n": 1}, "list": [1, 2, 3]},
            headers={"req-id": "r-1"},
            meta={"tag": "x"},
        )

        copy = copy_for_peek(message)

        self.assertIsNot(copy, message)
        self.assertEqual(copy.id, message.id)
        self.assertIsNot(copy.body, message.body)
        self.assertIsNot(copy.body["deep"], message.body["deep"])
        self.assertEqual(copy.body, message.body)
        self.assertIsNot(copy.headers, message.headers)
        self.assertIsNot(copy.meta, message.meta)

    def test_unclonable_payloads_fall_back_to_a_shared_reference(self):
        """文档化：信封隔离，拷不动的嵌套字段共享引用（不抛错、不假装深拷贝）。"""
        message = MailMessage(id="m-2", from_=SENDER, to="mem://b@example.com/inbox",
                              body={"fn": lambda: 1})

        copy = copy_for_peek(message)

        self.assertIsNot(copy, message)
        self.assertIs(copy.body["fn"], message.body["fn"])

    def test_plain_dicts_get_a_new_envelope_too(self):
        original = {"id": "m-3", "payload": "original"}

        copy = copy_for_peek(original)

        self.assertIsNot(copy, original)
        copy["payload"] = "mutated by the observer"
        self.assertEqual(original["payload"], "original")


class TestQueuePeek(unittest.TestCase):
    """`MailMessageQueue.peek`：队列层的只读快照。"""

    def setUp(self):
        self.queue = MailMessageQueue()
        self.topic = "mem://queue-peek/inbox"
        self.msg1 = queued("msg-1", self.topic, {"n": 1})
        self.msg2 = queued("msg-2", self.topic, {"n": 2})

    def test_returns_head_messages_as_copies_without_touching_anything(self):
        self.queue.enqueue(self.topic, self.msg1)
        self.queue.enqueue(self.topic, self.msg2)

        snapshot = self.queue.peek(self.topic)

        self.assertEqual([m.id for m in snapshot], ["msg-1", "msg-2"])
        # 关键：快照完全不留痕迹
        self.assertEqual(self.queue.get_status(self.topic), {"unread_count": 2, "in_flight_count": 0})
        self.assertTrue(self.queue.has("msg-1"))

    def test_empty_queue_and_unknown_topic_are_empty_lists(self):
        self.assertEqual(self.queue.peek(self.topic), [])
        self.assertEqual(self.queue.peek("mem://never-seen/inbox"), [])
        self.assertEqual(self.queue.peek("mem://never-seen/inbox", 3), [])

    def test_mutating_a_copy_cannot_reach_the_queued_message(self):
        self.queue.enqueue(self.topic, queued("msg-1", self.topic, {"payload": "original"}))

        copy = self.queue.peek(self.topic)[0]
        copy.body["payload"] = "mutated by the observer"
        copy.id = "should not leak"

        self.assertEqual(self.queue.peek(self.topic)[0].body["payload"], "original")
        self.assertEqual(self.queue.dequeue(self.topic).id, "msg-1")

    def test_limit_takes_the_head_only(self):
        self.queue.enqueue(self.topic, self.msg1)
        self.queue.enqueue(self.topic, self.msg2)
        self.queue.enqueue(self.topic, queued("msg-3", self.topic, {"n": 3}))

        self.assertEqual(self.queue.peek(self.topic, 0), [])
        self.assertEqual([m.id for m in self.queue.peek(self.topic, 2)], ["msg-1", "msg-2"])
        self.assertEqual([m.id for m in self.queue.peek(self.topic, 99)], ["msg-1", "msg-2", "msg-3"])
        self.assertEqual(len(self.queue.peek(self.topic, math.inf)), 3)
        self.assertEqual(self.queue.get_status(self.topic)["unread_count"], 3)  # limit 不消耗消息

    def test_invalid_limit_raises_instead_of_peeking_everything(self):
        self.queue.enqueue(self.topic, self.msg1)

        for bad in (-1, -math.inf, 1.5, math.nan):
            with self.assertRaisesRegex(InvalidFetchOptionsError, "non-negative integer"):
                self.queue.peek(self.topic, bad)
        self.assertEqual(self.queue.get_status(self.topic)["unread_count"], 1)

    def test_peek_does_not_look_at_in_flight_messages(self):
        self.queue.enqueue(self.topic, self.msg1)
        self.queue.enqueue(self.topic, self.msg2)
        self.queue.dequeue_for_ack(self.topic, None, lambda m: m.id)

        self.assertEqual([m.id for m in self.queue.peek(self.topic)], ["msg-2"])
        self.assertEqual(self.queue.get_status(self.topic), {"unread_count": 1, "in_flight_count": 1})

        self.queue.nack("msg-1", True)
        self.assertEqual([m.id for m in self.queue.peek(self.topic)], ["msg-1", "msg-2"])

    def test_repeated_peek_does_not_run_the_stale_scan(self):
        """只读操作不能触发 stale 重投：in-flight 属于那个消费者。"""
        self.queue.enqueue(self.topic, self.msg1)
        self.queue.dequeue_for_ack(self.topic, 0.01, lambda m: m.id)

        time.sleep(0.05)  # 已超过 ack_timeout
        self.assertEqual(self.queue.peek(self.topic), [])
        self.assertEqual(self.queue.get_status(self.topic), {"unread_count": 0, "in_flight_count": 1})

        self.queue.ack("msg-1")
        self.assertEqual(self.queue.get_status(self.topic), {"unread_count": 0, "in_flight_count": 0})


class TestBusPeek(unittest.TestCase):
    """`MemoryEventBus.peek`：总线层的只读快照。"""

    def test_no_dequeue_no_in_flight_no_activity_bump(self):
        bus = MemoryEventBus()
        topic = "mem://bus-peek/inbox"
        bus.publish(topic, queued("p-1", topic, {"n": 1}))

        activity_before = bus.get_status(topic)["last_activity_time"]
        snapshot = bus.peek(topic)

        self.assertEqual([m.id for m in snapshot], ["p-1"])
        self.assertEqual(
            bus.get_status(topic),
            {
                "exists": True,
                "unread_count": 1,
                "in_flight_count": 0,
                "last_activity_time": activity_before,
                "subscriber_count": 0,
            },
        )

        # 消息仍能被正常取走
        self.assertEqual(bus.fetch_and_forget(topic).id, "p-1")
        self.assertEqual(bus.peek(topic), [])

    def test_the_bus_hands_out_copies(self):
        bus = MemoryEventBus()
        topic = "mem://bus-peek-copy/inbox"
        original = queued("p-2", topic, {"n": 1})
        bus.publish(topic, original)

        bus.peek(topic)[0].body["n"] = 99

        self.assertEqual(original.body["n"], 1)
        self.assertEqual(bus.fetch_and_forget(topic).body["n"], 1)


class TestPeekThroughMailbox(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = MemoryProvider.create_isolated()
        self.mailbox = Mailbox()
        self.mailbox.register_provider(self.provider)
        await self.mailbox.start()

    async def asyncTearDown(self):
        await self.mailbox.stop(purge=True)

    async def _post(self, address, body):
        return await self.mailbox.post(OutgoingMail(from_=SENDER, to=address, body=body))

    async def test_returns_every_queued_message_without_consuming_anything(self):
        address = "mem://peek-basic@example.com/inbox"
        await self._post(address, {"n": 1})
        await self._post(address, {"n": 2})

        snapshot = await self.mailbox.fetch(address, FetchOptions(peek=True))

        self.assertIsInstance(snapshot, list)
        self.assertEqual([m.body for m in snapshot], [{"n": 1}, {"n": 2}])
        for message in snapshot:
            self.assertNotIsInstance(message, AckableMessage)  # 只读快照不可 ack
            self.assertFalse(hasattr(message, "ack"))

        status = await self.mailbox.status(address)
        self.assertEqual(status.unread_count, 2)
        self.assertEqual(status.in_flight_count, 0)

    async def test_repeated_snapshots_are_stable_and_fetch_still_works_fifo(self):
        address = "mem://peek-stable@example.com/inbox"
        await self._post(address, {"n": 1})
        await self._post(address, {"n": 2})

        first = await self.mailbox.fetch(address, FetchOptions(peek=True))
        second = await self.mailbox.fetch(address, FetchOptions(peek=True))

        self.assertEqual([m.id for m in first], [m.id for m in second])
        self.assertEqual((await self.mailbox.fetch(address)).body, {"n": 1})  # 不传 options = 隐式 ACK
        self.assertEqual((await self.mailbox.fetch(address, FetchOptions())).body, {"n": 2})
        self.assertIsNone(await self.mailbox.fetch(address, FetchOptions()))

    async def test_peek_does_not_refresh_last_activity_time(self):
        """诊断页轮询不会把空闲地址刷成「刚刚有活动」。"""
        address = "mem://peek-activity@example.com/inbox"
        await self._post(address, {"n": 1})

        before = await self.mailbox.status(address)
        await self.mailbox.fetch(address, FetchOptions(peek=True))
        await self.mailbox.fetch(address, FetchOptions(peek=True))
        after = await self.mailbox.status(address)

        self.assertEqual(after.last_activity_time, before.last_activity_time)

    async def test_mutating_a_snapshot_cannot_reach_the_queue(self):
        address = "mem://peek-copy@example.com/inbox"
        await self._post(address, {"deep": {"n": 1}})

        copy = (await self.mailbox.fetch(address, FetchOptions(peek=True)))[0]
        copy.body["deep"]["n"] = 99
        copy.headers = {"tampered": True}

        again = (await self.mailbox.fetch(address, FetchOptions(peek=True)))[0]
        self.assertEqual(again.body, {"deep": {"n": 1}})

        consumed = await self.mailbox.fetch(address, FetchOptions())
        self.assertEqual(consumed.body, {"deep": {"n": 1}})

    async def test_limit_zero_empty_and_larger_than_the_queue(self):
        address = "mem://peek-limit@example.com/inbox"
        for n in (1, 2, 3):
            await self._post(address, {"n": n})

        self.assertEqual(await self.mailbox.fetch(address, FetchOptions(peek=True, limit=0)), [])
        snapshot = await self.mailbox.fetch(address, FetchOptions(peek=True, limit=2))
        self.assertEqual([m.body["n"] for m in snapshot], [1, 2])
        self.assertEqual(len(await self.mailbox.fetch(address, FetchOptions(peek=True, limit=99))), 3)
        self.assertEqual(len(await self.mailbox.fetch(address, FetchOptions(peek=True))), 3)
        self.assertEqual((await self.mailbox.status(address)).unread_count, 3)

    async def test_invalid_limit_raises_without_consuming_anything(self):
        address = "mem://peek-bad-limit@example.com/inbox"
        await self._post(address, {})

        with self.assertRaises(InvalidFetchOptionsError):
            await self.mailbox.fetch(address, FetchOptions(peek=True, limit=-1))
        with self.assertRaises(InvalidFetchOptionsError):
            await self.mailbox.fetch(address, FetchOptions(peek=True, limit=1.5))
        self.assertEqual((await self.mailbox.status(address)).unread_count, 1)

    async def test_empty_queue_and_unknown_address_are_empty_lists(self):
        self.assertEqual(await self.mailbox.fetch("mem://peek-empty@example.com/inbox", FetchOptions(peek=True)), [])
        unknown = await self.mailbox.fetch("mem://not-even-a-valid-address", FetchOptions(peek=True, limit=5))
        self.assertEqual(unknown, [])

    async def test_peek_with_manual_ack_is_contradictory(self):
        """直接报错，绝不「猜」调用方想要哪个。"""
        address = "mem://peek-conflict@example.com/inbox"
        await self._post(address, {})

        with self.assertRaisesRegex(InvalidFetchOptionsError, "contradictory"):
            await self.mailbox.fetch(address, FetchOptions(peek=True, manual_ack=True))

        status = await self.mailbox.status(address)
        self.assertEqual(status.unread_count, 1)   # 报错没消耗消息
        self.assertEqual(status.in_flight_count, 0)  # 也没偷偷建 in-flight

    async def test_peek_false_is_an_ordinary_consuming_fetch(self):
        address = "mem://peek-false@example.com/inbox"
        await self._post(address, {"n": 1})

        message = await self.mailbox.fetch(address, FetchOptions(peek=False))
        self.assertEqual(message.body, {"n": 1})
        self.assertEqual((await self.mailbox.status(address)).unread_count, 0)

    async def test_peek_covers_only_waiting_messages(self):
        address = "mem://peek-vs-inflight@example.com/inbox"
        await self._post(address, {"n": 1})
        await self._post(address, {"n": 2})

        inflight = await self.mailbox.fetch(address, FetchOptions(manual_ack=True))
        self.assertEqual(inflight.message.body, {"n": 1})

        snapshot = await self.mailbox.fetch(address, FetchOptions(peek=True))
        self.assertEqual([m.body["n"] for m in snapshot], [2])  # 不会把别人手上的消息也列出来

        await inflight.nack(True)
        self.assertEqual([m.body["n"] for m in await self.mailbox.fetch(address, FetchOptions(peek=True))], [1, 2])

    async def test_peek_does_not_hide_messages_from_another_consumer(self):
        address = "mem://peek-other-consumer@example.com/inbox"
        await self._post(address, {"n": 1})

        auditor = await self.mailbox.fetch(address, FetchOptions(peek=True))
        consumer = await self.mailbox.fetch(address, FetchOptions(manual_ack=True))

        self.assertEqual([m.body["n"] for m in auditor], [1])
        self.assertEqual(consumer.message.body, {"n": 1})
        await consumer.ack()
        self.assertEqual(await self.mailbox.fetch(address, FetchOptions(peek=True)), [])

    async def test_peek_is_not_a_way_around_address_validation(self):
        with self.assertRaisesRegex(InvalidMailboxAddressError, "would be dropped by address canonicalization"):
            await self.mailbox.fetch("mem://peek-invalid@example.com/inbox?thread=1", FetchOptions(peek=True))

    async def test_snapshots_are_independent_of_each_other(self):
        address = "mem://peek-independent@example.com/inbox"
        await self._post(address, {"shared": {"n": 1}})

        first = await self.mailbox.fetch(address, FetchOptions(peek=True))
        second = await self.mailbox.fetch(address, FetchOptions(peek=True))

        first[0].body["shared"]["n"] = 42
        self.assertEqual(second[0].body["shared"]["n"], 1)


class TransportProvider(MailboxProvider):
    """不支持只读快照的传输型 provider（消息不在本地，无法枚举 / 快照）。"""

    def __init__(self):
        super().__init__("transport")
        self.queued = []

    async def _send(self, message: MailMessage) -> None:
        self.queued.append(message)

    async def _subscribe(self, address, on_receive):
        return lambda: None

    async def _unsubscribe(self, subscription_id, handle) -> None:
        pass

    async def _fetch(self, address, options):
        return self.queued.pop(0) if self.queued else None


class CountingPeekProvider(TransportProvider):
    """实现了 `_peek` 的 provider：基类把归一后的 limit 交下去。"""

    def __init__(self):
        super().__init__()
        self.protocol = "counter"
        self.peek_calls = []

    async def _peek(self, address, limit=None):
        self.peek_calls.append({"address": address, "limit": limit})
        return []


class TestProvidersWithoutPeekMustNotSilentlyConsume(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = TransportProvider()
        self.mailbox = Mailbox()
        self.mailbox.register_provider(self.provider)
        await self.mailbox.start()

    async def test_missing_peek_raises_and_keeps_every_message(self):
        await self.mailbox.post(OutgoingMail(from_="transport:a@e/u", to="transport:b@e/inbox", body={"n": 1}))

        with self.assertRaises(UnsupportedCapabilityError) as caught:
            await self.mailbox.fetch("transport:b@e/inbox", FetchOptions(peek=True))
        self.assertIn("does not support 'read-only peek", str(caught.exception))
        self.assertEqual(caught.exception.code, "MBX_UNSUPPORTED_CAPABILITY")
        self.assertEqual(caught.exception.protocol, "transport")

        self.assertEqual(len(self.provider.queued), 1)  # 关键：没有「以为在只读、实际被取走」

    async def test_a_provider_that_does_peek_gets_the_normalized_limit(self):
        provider = CountingPeekProvider()
        mailbox = Mailbox()
        mailbox.register_provider(provider)

        await mailbox.post(OutgoingMail(from_="counter:a@e/u", to="counter:b@e/inbox", body={}))
        await mailbox.fetch("counter:b@e/inbox", FetchOptions(peek=True, limit=math.inf))

        self.assertEqual(provider.peek_calls, [{"address": "counter:b@e/inbox", "limit": None}])


if __name__ == "__main__":
    unittest.main()
