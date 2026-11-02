import asyncio
import contextlib
import io
import unittest
from mboxlabs.mailbox import (
    Mailbox,
    MemoryProvider,
    OutgoingMail,
    FetchOptions,
    MailboxStatus,
    DEFAULT_ACK_TIMEOUT,
    AttemptLedger,
    RetryPolicy,
    is_retriable_error,
    next_retry_delay,
)
from mboxlabs.mailbox.message import MailMessage
from mboxlabs.mailbox.providers.queue import MailMessageQueue
from mboxlabs.mailbox.tests.helpers import TransientError


class TestMailbox(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mailbox = Mailbox()
        self.provider = MemoryProvider()
        self.mailbox.register_provider(self.provider)
        await self.mailbox.start()

    async def asyncTearDown(self):
        await self.mailbox.stop()

    async def test_stop_cleanup(self):
        address = "mem:test/stop"
        received_msgs = []

        async def on_receive(msg):
            received_msgs.append(msg)

        sub = await self.mailbox.subscribe(address, on_receive)
        
        # Stop mailbox
        await self.mailbox.stop()

        # Send message after stop
        mail = OutgoingMail(from_="mem:sender", to=address, body="after stop")
        await self.mailbox.post(mail)
        await asyncio.sleep(0.1)

        self.assertEqual(len(received_msgs), 0)

    async def test_send_and_subscribe(self):
        address = "mem:test/inbox"
        received_msgs = []

        async def on_receive(msg):
            received_msgs.append(msg)

        await self.mailbox.subscribe(address, on_receive)

        mail = OutgoingMail(
            from_="mem:test/sender",
            to=address,
            body={"hello": "world"}
        )

        await self.mailbox.post(mail)

        # Give it a moment for async delivery
        await asyncio.sleep(0.1)

        self.assertEqual(len(received_msgs), 1)
        self.assertEqual(received_msgs[0].body, {"hello": "world"})
        self.assertIn('mbx-sent-at', received_msgs[0].headers)

    async def test_fetch_auto_ack(self):
        address = "mem:test/fetch"
        mail = OutgoingMail(
            from_="mem:test/sender",
            to=address,
            body="content",
            id="msg1"
        )

        await self.mailbox.post(mail)

        # Fetch
        msg = await self.mailbox.fetch(address, FetchOptions(manual_ack=False))
        self.assertIsNotNone(msg)
        self.assertEqual(msg.id, "msg1")

        # Should be gone
        msg2 = await self.mailbox.fetch(address, FetchOptions(manual_ack=False))
        self.assertIsNone(msg2)

    async def test_fetch_manual_ack(self):
        address = "mem:test/ack"
        mail = OutgoingMail(
            from_="mem:test/sender",
            to=address,
            body="content",
            id="msg2"
        )

        await self.mailbox.post(mail)

        options = FetchOptions(manual_ack=True)

        # Fetch
        ackable = await self.mailbox.fetch(address, options)
        self.assertIsNotNone(ackable)
        self.assertEqual(ackable.message.id, "msg2")

        # Should not be available (in flight)
        msg2 = await self.mailbox.fetch(address, options)
        self.assertIsNone(msg2)

        # Ack
        await ackable.ack()

        # Should be gone permanently
        msg3 = await self.mailbox.fetch(address, options)
        self.assertIsNone(msg3)

    async def test_fetch_nack_requeue(self):
        address = "mem:test/nack"
        mail = OutgoingMail(
            from_="mem:test/sender",
            to=address,
            body="content",
            id="msg3"
        )

        await self.mailbox.post(mail)

        options = FetchOptions(manual_ack=True)

        # Fetch
        ackable = await self.mailbox.fetch(address, options)
        self.assertIsNotNone(ackable)

        # Nack with requeue
        await ackable.nack(requeue=True)

        # Should be available again
        msg2 = await self.mailbox.fetch(address, options)
        self.assertIsNotNone(msg2)
        self.assertEqual(msg2.message.id, "msg3")

    async def test_status(self):
        address = "mem:test/status"
        mail = OutgoingMail(
            from_="mem:test/sender",
            to=address,
            body="content"
        )

        await self.mailbox.post(mail)

        status = await self.mailbox.status(address)
        self.assertEqual(status.unread_count, 1)
        self.assertEqual(status.state, "online")

    def test_provider_access(self):
        # Test providers property
        providers = self.mailbox.providers
        self.assertIn("mem", providers)
        self.assertEqual(providers["mem"], self.provider)

        # Test get_provider with and without colon
        self.assertEqual(self.mailbox.get_provider("mem"), self.provider)
        self.assertEqual(self.mailbox.get_provider("mem:"), self.provider)

        # Test get_provider without raise_error
        self.assertIsNone(self.mailbox.get_provider("unknown"))

        # Test get_provider with raise_error
        from mboxlabs.mailbox.error import ProviderNotFound
        with self.assertRaises(ProviderNotFound):
            self.mailbox.get_provider("unknown", raise_error_if_failed=True)

class TestMemoryDeliverySemantics(unittest.IsolatedAsyncioTestCase):
    """订阅与取回二选一、订阅失败不静默丢失、in-flight 可见（与 TS 端对齐）。

    这里用的是默认构造的 provider（共享进程级 bus），topic 跨 Mailbox 实例可见，
    所以地址仍必须用独立前缀。需要真隔离的用例见 `test_memory_isolation.py`
    （`MemoryProvider.create_isolated()`）。
    """

    async def asyncSetUp(self):
        self.mailbox = Mailbox()
        self.provider = MemoryProvider()
        self.mailbox.register_provider(self.provider)
        await self.mailbox.start()

    async def asyncTearDown(self):
        await self.mailbox.stop()

    async def test_subscriber_leaves_no_queued_copy(self):
        address = "mem:py-bus/sub-only"
        received = []

        async def on_receive(msg):
            received.append(msg)

        await self.mailbox.subscribe(address, on_receive)
        await self.mailbox.post(OutgoingMail(from_="mem:py-bus/sender", to=address, body={"n": 1}))
        await asyncio.sleep(0.1)

        self.assertEqual(len(received), 1)
        # 旧行为：投递给监听器的同时又入队 → unread_count 永久增长
        status = await self.mailbox.status(address)
        self.assertEqual(status.unread_count, 0)
        self.assertIsNone(await self.mailbox.fetch(address, FetchOptions(manual_ack=False)))

    async def test_switches_back_to_queue_after_last_unsubscribe(self):
        # 守护“只判 topic 是否存在”的写法：退订后 topics[topic] 仍存在但是空列表，
        # 不判空就会静默丢弃后续消息。
        address = "mem:py-bus/unsub-then-fetch"

        async def on_receive(msg):
            pass

        sub = await self.mailbox.subscribe(address, on_receive)
        await sub.unsubscribe()

        await self.mailbox.post(OutgoingMail(from_="mem:py-bus/sender", to=address, body={"n": 1}))

        msg = await self.mailbox.fetch(address, FetchOptions(manual_ack=False))
        self.assertIsNotNone(msg)
        self.assertEqual(msg.body, {"n": 1})

    async def test_retriable_subscriber_failure_returns_to_queue(self):
        address = "mem:py-bus/retriable-failure"

        async def on_receive(msg):
            raise TransientError("temporary outage")

        await self.mailbox.subscribe(address, on_receive)

        with contextlib.redirect_stdout(io.StringIO()):
            await self.mailbox.post(
                OutgoingMail(from_="mem:py-bus/sender", to=address, body={"n": 1}, id="retry-me")
            )
            await asyncio.sleep(0.1)

        # 旧行为：unread_count 为 0，消息在无人处理的情况下凭空消失
        status = await self.mailbox.status(address)
        self.assertEqual(status.unread_count, 1)

        recovered = await self.mailbox.fetch(address, FetchOptions(manual_ack=False))
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.id, "retry-me")

    async def test_non_retriable_subscriber_failure_is_dropped(self):
        address = "mem:py-bus/fatal-failure"

        async def on_receive(msg):
            raise ValueError("malformed payload")

        await self.mailbox.subscribe(address, on_receive)

        with contextlib.redirect_stdout(io.StringIO()):
            await self.mailbox.post(OutgoingMail(from_="mem:py-bus/sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.1)

        # 不可重试的错误按死信处理：不回到队列，避免无限重试同一条坏消息
        status = await self.mailbox.status(address)
        self.assertEqual(status.unread_count, 0)
        self.assertIsNone(await self.mailbox.fetch(address, FetchOptions(manual_ack=False)))

    async def test_in_flight_messages_are_visible_in_status(self):
        address = "mem:py-bus/in-flight-visible"
        await self.mailbox.post(OutgoingMail(from_="mem:py-bus/sender", to=address, body="content"))

        initial = await self.mailbox.status(address)
        self.assertEqual(initial.unread_count, 1)
        self.assertEqual(initial.in_flight_count, 0)

        ackable = await self.mailbox.fetch(address, FetchOptions(manual_ack=True))
        self.assertIsNotNone(ackable)

        during = await self.mailbox.status(address)
        self.assertEqual(during.unread_count, 0)
        # 旧行为：in-flight 消息在状态里完全隐形
        self.assertEqual(during.in_flight_count, 1)

        await ackable.ack()
        after = await self.mailbox.status(address)
        self.assertEqual(after.in_flight_count, 0)

    async def test_never_acked_message_is_recovered_after_default_timeout(self):
        address = "mem:py-bus/in-flight-recovered"
        await self.mailbox.post(
            OutgoingMail(from_="mem:py-bus/sender", to=address, body="content", id="lost-then-found")
        )

        # 消费者“崩溃”：取走后不 ack，也不指定 ack_timeout
        ackable = await self.mailbox.fetch(address, FetchOptions(manual_ack=True))
        self.assertIsNotNone(ackable)
        self.assertIsNone(await self.mailbox.fetch(address, FetchOptions(manual_ack=True)))

        # 不等 30 秒：直接把 in-flight 时间戳往前推，等价于时间流逝
        flight = self.provider.bus.queue.in_flight[ackable.message.id]
        flight.timestamp -= DEFAULT_ACK_TIMEOUT + 1

        recovered = await self.mailbox.fetch(address, FetchOptions(manual_ack=True))
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.message.id, "lost-then-found")


class TestRetryHelpers(unittest.TestCase):
    """retry 模块只放策略数学，单独测（与 TS 的 src/lib/retry.ts 同语义）。"""

    def test_next_retry_delay_is_exponential_and_capped(self):
        policy = RetryPolicy(max_attempts=5, base_delay=1.0, factor=2.0, max_delay=5.0)
        self.assertEqual(next_retry_delay(policy, 1), 1.0)
        self.assertEqual(next_retry_delay(policy, 2), 2.0)
        self.assertEqual(next_retry_delay(policy, 3), 4.0)
        # 夹在 max_delay 之内，不能无限增长
        self.assertEqual(next_retry_delay(policy, 4), 5.0)

    def test_is_retriable_error_matches_other_clients(self):
        class Marked(Exception):
            is_retriable = True

        class Coded(Exception):
            code = 503

        self.assertTrue(is_retriable_error(Marked()))
        self.assertTrue(is_retriable_error(Coded()))
        self.assertFalse(is_retriable_error(ValueError("malformed payload")))

    def test_attempt_ledger_counts_per_subscriber(self):
        # 订阅是广播：重试次数必须按「订阅者 + 消息」算，不能混在一起
        ledger = AttemptLedger()
        self.assertEqual(ledger.record("sub-1", "msg-1"), 1)
        self.assertEqual(ledger.record("sub-1", "msg-1"), 2)
        self.assertEqual(ledger.record("sub-2", "msg-1"), 1)
        ledger.reset("sub-1", "msg-1")
        self.assertEqual(ledger.count("sub-1", "msg-1"), 0)
        self.assertEqual(ledger.count("sub-2", "msg-1"), 1)


class TestQueueRequeueDedup(unittest.TestCase):
    """回队去重：同一 id 不重复入队（与 TS 的 MessageQueue.requeue 对齐）。"""

    def test_duplicate_requeue_is_skipped(self):
        queue = MailMessageQueue()
        message = MailMessage(id="dup-1", from_="a", to="b", body=1)
        queue.enqueue("topic", message)

        self.assertFalse(queue.requeue("topic", message))
        self.assertFalse(queue.requeue("topic", message))
        self.assertEqual(queue.get_status("topic")["unread_count"], 1)

    def test_requeue_skips_a_message_that_is_in_flight(self):
        queue = MailMessageQueue()
        message = MailMessage(id="inflight-1", from_="a", to="b", body=1)
        queue.enqueue("topic", message)
        queue.dequeue_for_ack("topic", None, lambda m: m.id)

        self.assertFalse(queue.requeue("topic", message))
        status = queue.get_status("topic")
        self.assertEqual(status["unread_count"], 0)
        self.assertEqual(status["in_flight_count"], 1)

    def test_same_id_can_be_requeued_again_after_being_taken_out(self):
        # 去重不能把「已处理完」也当成重复，否则重试永远进不了队列
        queue = MailMessageQueue()
        message = MailMessage(id="again-1", from_="a", to="b", body=1)
        queue.enqueue("topic", message)

        queue.dequeue_for_ack("topic", None, lambda m: m.id)
        queue.nack("again-1", True)
        self.assertEqual(queue.get_status("topic")["unread_count"], 1)

        queue.dequeue_for_ack("topic", None, lambda m: m.id)
        queue.nack("again-1", True)
        self.assertEqual(queue.get_status("topic")["unread_count"], 1)
        self.assertTrue(queue.has("again-1"))


class TestQueueClear(unittest.TestCase):
    """issue#3：队列状态必须能被清掉，否则同进程的多个 Mailbox / 多个用例互相串台。"""

    def test_clear_one_topic_reports_how_many_messages_were_dropped(self):
        queue = MailMessageQueue()
        one = MailMessage(id="q-1", from_="a", to="b", body=1)
        two = MailMessage(id="q-2", from_="a", to="b", body=2)
        queue.enqueue("topic1", one)
        queue.enqueue("topic1", two)
        queue.enqueue("topic2", two)

        self.assertEqual(queue.clear("topic1"), 2)

        self.assertEqual(queue.get_status("topic1"), {"unread_count": 0, "in_flight_count": 0})
        self.assertEqual(queue.get_status("topic2")["unread_count"], 1)
        self.assertEqual(queue.dequeue("topic2").id, two.id)

    def test_clear_drops_in_flight_too_and_it_cannot_come_back(self):
        # in-flight 也是队列状态的一部分：只清「等待中」的那半，用例跑完还是会看到残留消息。
        queue = MailMessageQueue()
        message = MailMessage(id="clear-inflight-1", from_="a", to="b", body=1)
        queue.enqueue("topic1", message)
        queue.dequeue_for_ack("topic1", None, lambda m: m.id)
        self.assertEqual(queue.get_status("topic1"), {"unread_count": 0, "in_flight_count": 1})

        self.assertEqual(queue.clear("topic1"), 1)
        self.assertEqual(queue.get_status("topic1"), {"unread_count": 0, "in_flight_count": 0})
        self.assertFalse(queue.has(message.id))

        # 迟到的 ack / nack 不能再把已清理的消息放回队列（否则会“复活”成幽灵消息）。
        queue.ack(message.id)
        queue.nack(message.id, True)
        self.assertEqual(queue.get_status("topic1"), {"unread_count": 0, "in_flight_count": 0})

    def test_clear_without_topic_drops_every_topic(self):
        queue = MailMessageQueue()
        queue.enqueue("topic1", MailMessage(id="q-1", from_="a", to="b", body=1))
        queue.enqueue("topic2", MailMessage(id="q-2", from_="a", to="b", body=2))
        queue.dequeue_for_ack("topic2", None, lambda m: m.id)

        self.assertEqual(queue.clear(), 2)  # topic1 的 1 条 + topic2 的 1 条 in-flight
        self.assertEqual(queue.get_status("topic1"), {"unread_count": 0, "in_flight_count": 0})
        self.assertEqual(queue.get_status("topic2"), {"unread_count": 0, "in_flight_count": 0})

    def test_clear_is_idempotent_and_safe_on_an_unknown_topic(self):
        queue = MailMessageQueue()
        message = MailMessage(id="q-1", from_="a", to="b", body=1)
        queue.enqueue("topic1", message)

        self.assertEqual(queue.clear("topic1"), 1)
        self.assertEqual(queue.clear("topic1"), 0)
        self.assertEqual(queue.clear("never-seen"), 0)
        self.assertEqual(queue.clear(), 0)

        # 清空后队列还能照常使用
        queue.enqueue("topic1", message)
        self.assertEqual(queue.dequeue("topic1").id, message.id)


class TestSubscriptionRetry(unittest.IsolatedAsyncioTestCase):
    """订阅重试（opt-in）：默认不重投，开启后只重投给失败的那个订阅者。"""

    async def asyncSetUp(self):
        self.mailbox = Mailbox()
        self.mailbox.register_provider(MemoryProvider())
        await self.mailbox.start()

    async def asyncTearDown(self):
        await self.mailbox.stop()

    async def test_multiple_failing_subscribers_leave_one_copy(self):
        address = "mem:py-bus/dedup-multi-sub"

        async def failing(msg):
            raise TransientError("outage")

        with contextlib.redirect_stdout(io.StringIO()):
            await self.mailbox.subscribe(address, failing)
            await self.mailbox.subscribe(address, failing)
            sent = await self.mailbox.post(
                OutgoingMail(from_="mem:py-bus/sender", to=address, body={"n": 1})
            )
            await asyncio.sleep(0.1)

            # 旧行为：每个失败的订阅者各回队一份 → unread_count 为 2，同一条消息被 fetch 两次
            status = await self.mailbox.status(address)
            self.assertEqual(status.unread_count, 1)

            first = await self.mailbox.fetch(address, FetchOptions(manual_ack=False))
            self.assertIsNotNone(first)
            self.assertEqual(first.id, sent.id)
            self.assertIsNone(await self.mailbox.fetch(address, FetchOptions(manual_ack=False)))

    async def test_retry_targets_only_the_failing_subscriber(self):
        address = "mem:py-bus/retry-targeted"
        mailbox = Mailbox()
        mailbox.register_provider(MemoryProvider(RetryPolicy(max_attempts=3, base_delay=0.005)))
        await mailbox.start()

        state = {"healthy": 0, "flaky": 0}

        async def healthy(msg):
            state["healthy"] += 1

        async def flaky(msg):
            state["flaky"] += 1
            if state["flaky"] < 3:
                raise TransientError("outage")

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                await mailbox.subscribe(address, healthy)
                await mailbox.subscribe(address, flaky)
                await mailbox.post(OutgoingMail(from_="mem:py-bus/sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.3)

            self.assertEqual(state["flaky"], 3)
            # 已经成功的订阅者不会被重试广播再跑一次（否则副作用执行两次）
            self.assertEqual(state["healthy"], 1)
            self.assertEqual((await mailbox.status(address)).unread_count, 0)
        finally:
            await mailbox.stop()

    async def test_retry_exhausted_falls_back_to_queue(self):
        address = "mem:py-bus/retry-exhausted"
        mailbox = Mailbox()
        mailbox.register_provider(MemoryProvider(RetryPolicy(max_attempts=2, base_delay=0.005)))
        await mailbox.start()

        calls = {"n": 0}

        async def failing(msg):
            calls["n"] += 1
            raise TransientError("outage")

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                await mailbox.subscribe(address, failing)
                sent = await mailbox.post(
                    OutgoingMail(from_="mem:py-bus/sender", to=address, body={"n": 1})
                )
                await asyncio.sleep(0.3)

            self.assertEqual(calls["n"], 2)
            self.assertEqual((await mailbox.status(address)).unread_count, 1)
            recovered = await mailbox.fetch(address, FetchOptions(manual_ack=False))
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.id, sent.id)
        finally:
            await mailbox.stop()

    async def test_non_retriable_error_is_not_retried(self):
        address = "mem:py-bus/retry-fatal"
        mailbox = Mailbox()
        mailbox.register_provider(MemoryProvider(RetryPolicy(max_attempts=3, base_delay=0.005)))
        await mailbox.start()

        calls = {"n": 0}

        async def failing(msg):
            calls["n"] += 1
            raise ValueError("malformed payload")

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                await mailbox.subscribe(address, failing)
                await mailbox.post(OutgoingMail(from_="mem:py-bus/sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.3)

            self.assertEqual(calls["n"], 1)
            self.assertEqual((await mailbox.status(address)).unread_count, 0)
        finally:
            await mailbox.stop()

    async def test_unsubscribe_during_pending_retry_returns_message_to_queue(self):
        address = "mem:py-bus/retry-unsubscribed"
        mailbox = Mailbox()
        mailbox.register_provider(MemoryProvider(RetryPolicy(max_attempts=5, base_delay=0.05)))
        await mailbox.start()

        calls = {"n": 0}

        async def failing(msg):
            calls["n"] += 1
            raise TransientError("outage")

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                sub = await mailbox.subscribe(address, failing)
                sent = await mailbox.post(
                    OutgoingMail(from_="mem:py-bus/sender", to=address, body={"n": 1})
                )
                await asyncio.sleep(0.01)
                await sub.unsubscribe()
                await asyncio.sleep(0.2)

            self.assertEqual(calls["n"], 1)
            # 重投被取消，但消息不能静默消失
            self.assertEqual((await mailbox.status(address)).unread_count, 1)
            recovered = await mailbox.fetch(address, FetchOptions(manual_ack=False))
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.id, sent.id)
        finally:
            await mailbox.stop()


if __name__ == '__main__':
    unittest.main()
