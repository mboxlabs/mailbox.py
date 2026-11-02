"""
投递结算的竞态与兜底（与 TS 的 `src/mailbox.delivery-settlement.test.ts` 同一组不变量）。

固化的不变量（命名标记）：
  test_semantics_*  必须成立的投递语义，破坏它即回归。
  test_fact_*       附带锁定的实现约定（opt-out 开关、幂等结算等）。

覆盖的隐患：
  1. 退订 / `stop()` 时**正在执行**的投递被无条件结算为 `cancelled` → 「无人成功」误判 →
     已成功的副作用被 `fetch()` 消费者再执行一次（重复投递，换了个入口）。
  2. 广播型 provider 没接结算 barrier（第三方 provider 常见错误）→ 可重试失败静默丢失。
  3. 同一 `(subscription_id, message_id)` 并发投递时内部记账互相覆盖 → 一个 barrier 永不结算，
     其中一条消息静默消失（`post()` 允许自定义 id，见 `OutgoingMail.id`）。
  4. 卡死的订阅回调让 barrier 永不结算 → 消息既不回队也不死信，`unread_count` 里看不见。
  5. 生产者型 provider（自己组装消息再丢给本地订阅者）绕过基类投递入口 → 这次投递没人记账，
     失败既不回队也不死信（基类提供 `_deliver_to_local_subscription`）。
"""

import asyncio
import unittest

from mboxlabs.mailbox import (
    DEFAULT_DELIVERY_SETTLE_TIMEOUT,
    DeliveryResult,
    FetchOptions,
    Mailbox,
    MemoryProvider,
    MailboxProvider,
    OutgoingMail,
    RetryPolicy,
)
from mboxlabs.mailbox.message import MailMessage
from mboxlabs.mailbox.providers.queue import MailMessageQueue
from mboxlabs.mailbox.tests.helpers import addr_of, quiet

from mboxlabs.mailbox.tests.helpers import TransientError, addr_of, failing, quiet, reset_bus


class ProbeProvider(MemoryProvider):
    """观察基类的内部记账 + 死信钩子。"""

    def __init__(self, retry=None):
        super().__init__(retry)
        self.dead_lettered = []

    @property
    def outstanding(self) -> int:
        """基类的 `pending_delivery_count`（尚未结算的投递数）。"""
        return self.pending_delivery_count

    def _on_delivery_deadlettered(self, message, failed):
        self.dead_lettered.append({'message': message, 'failed': failed})


class RawBroadcastProvider(MailboxProvider):
    """
    第三方广播 provider 的常见写法：`_send` 里把消息派发出去，但**丢弃**了每次投递的结算
    future（没接 `_track_broadcast_settlement`）。于是这类 provider 的可重试失败会静默消失 ——
    基类必须兜住。
    """
    broadcast_deliveries = True

    def __init__(self):
        super().__init__("raw")
        self._listeners = {}
        self._queue = MailMessageQueue()

    async def _send(self, message: MailMessage) -> None:
        topic = message.to
        listeners = self._listeners.get(topic)
        if not listeners:
            self._queue.enqueue(topic, message)
            return
        # 故意不调用 `_track_broadcast_settlement`：模拟漏接 barrier 的 provider。
        for listener in listeners:
            listener(message)

    async def _subscribe(self, address, on_receive):
        topic = address
        self._listeners.setdefault(topic, set()).add(on_receive)

        def unsubscribe():
            self._listeners.get(topic, set()).discard(on_receive)

        return unsubscribe

    async def _unsubscribe(self, subscription_id, unsubscribe_handle) -> None:
        if callable(unsubscribe_handle):
            unsubscribe_handle()

    async def _fetch(self, address, options):
        return self._queue.dequeue(address)

    async def _nack(self, message: MailMessage, requeue: bool) -> None:
        if requeue:
            self._queue.requeue(message.to, message)


class TestRunningDeliveryIsNotPrejudged(unittest.IsolatedAsyncioTestCase):
    """隐患1：退订 / 关闭不得替正在执行的投递下结论。"""

    async def asyncSetUp(self):
        reset_bus()
        self.mailbox = Mailbox()
        self.mailbox.register_provider(MemoryProvider())
        await self.mailbox.start()

    async def asyncTearDown(self):
        await self.mailbox.stop()

    async def test_semantics_running_callback_is_not_declared_failed(self):
        address = addr_of('unsubscribe-inflight')
        side_effects = {'n': 0}

        async def handler(msg):
            await asyncio.sleep(0.04)
            side_effects['n'] += 1

        with quiet():
            sub = await self.mailbox.subscribe(address, handler)
            await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.01)
            await sub.unsubscribe()  # 回调还在跑：不能替它结算成 cancelled
            await asyncio.sleep(0.2)

        self.assertEqual(side_effects['n'], 1)
        self.assertEqual((await self.mailbox.status(address)).unread_count, 0)
        self.assertIsNone(await self.mailbox.fetch(address, FetchOptions(manual_ack=False)))

    async def test_semantics_stop_does_not_requeue_a_running_callback_that_succeeds(self):
        address = addr_of('stop-inflight')
        side_effects = {'n': 0}

        async def handler(msg):
            await asyncio.sleep(0.04)
            side_effects['n'] += 1

        with quiet():
            await self.mailbox.subscribe(address, handler)
            await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.01)
            await self.mailbox.stop()
            await asyncio.sleep(0.2)

        self.assertEqual(side_effects['n'], 1)
        self.assertEqual((await self.mailbox.status(address)).unread_count, 0)
        self.assertIsNone(await self.mailbox.fetch(address, FetchOptions(manual_ack=False)))

    async def test_semantics_running_callback_that_later_fails_still_falls_back_to_fetch(self):
        address = addr_of('unsubscribe-inflight-fail')

        async def handler(msg):
            await asyncio.sleep(0.03)
            raise TransientError("upstream down")

        with quiet():
            sub = await self.mailbox.subscribe(address, handler)
            sent = await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.01)
            await sub.unsubscribe()  # 回调还在跑，随后才抛错：没有任何订阅者成功 → 交给 fetch()
            await asyncio.sleep(0.2)

        self.assertEqual((await self.mailbox.status(address)).unread_count, 1)
        recovered = await self.mailbox.fetch(address, FetchOptions(manual_ack=False))
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.id, sent.id)

    async def test_semantics_unsubscribe_cancels_a_delivery_waiting_in_backoff(self):
        provider = ProbeProvider(RetryPolicy(max_attempts=5, base_delay=0.5))
        retry_mailbox = Mailbox()
        retry_mailbox.register_provider(provider)
        await retry_mailbox.start()

        try:
            with quiet():
                address = addr_of('unsubscribe-during-backoff')
                sub = await retry_mailbox.subscribe(address, failing())

                sent = await retry_mailbox.post(
                    OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1})
                )
                await asyncio.sleep(0.01)
                await sub.unsubscribe()
                await asyncio.sleep(0.1)

                self.assertEqual(provider.outstanding, 0)  # 退避定时器已取消并结算
                self.assertEqual((await retry_mailbox.status(address)).unread_count, 1)
                recovered = await retry_mailbox.fetch(address, FetchOptions(manual_ack=False))
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered.id, sent.id)
        finally:
            await retry_mailbox.stop()


class TestMissingSettlementBarrier(unittest.IsolatedAsyncioTestCase):
    """隐患2：广播 provider 没接结算 barrier 时不得静默丢失。"""

    async def asyncSetUp(self):
        self.mailbox = Mailbox()
        self.mailbox.register_provider(RawBroadcastProvider())
        await self.mailbox.start()

    async def asyncTearDown(self):
        await self.mailbox.stop()

    async def test_semantics_retriable_failure_is_still_recoverable_by_fetch(self):
        address = "raw://bus/no-barrier"
        with quiet() as output:
            await self.mailbox.subscribe(address, failing())
            sent = await self.mailbox.post(OutgoingMail(from_="raw://sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.05)

            recovered = await self.mailbox.fetch(address, FetchOptions(manual_ack=False))

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.id, sent.id)
        # 并且明确告诉 provider 作者：barrier 漏接了
        self.assertIn("settlement barrier", output.getvalue())

    async def test_semantics_missing_barrier_fallback_still_keeps_one_copy(self):
        address = "raw://bus/no-barrier-dedup"
        with quiet():
            await self.mailbox.subscribe(address, failing())
            await self.mailbox.subscribe(address, failing())
            await self.mailbox.post(OutgoingMail(from_="raw://sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.05)

            recovered = await self.mailbox.fetch(address, FetchOptions(manual_ack=False))
            self.assertIsNotNone(recovered)
            self.assertIsNone(await self.mailbox.fetch(address, FetchOptions(manual_ack=False)))

    async def test_semantics_provider_with_barrier_does_not_warn(self):
        address = addr_of('barrier-wired-no-warning')
        mem_mailbox = Mailbox()
        mem_mailbox.register_provider(MemoryProvider())
        await mem_mailbox.start()

        try:
            with quiet() as output:
                await mem_mailbox.subscribe(address, failing())
                await mem_mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.05)

            self.assertNotIn("settlement barrier", output.getvalue())
        finally:
            await mem_mailbox.stop()


class TestConcurrentDeliveriesOfSameId(unittest.IsolatedAsyncioTestCase):
    """隐患3：同 id 并发投递各自独立结算。"""

    async def asyncSetUp(self):
        reset_bus()

    async def test_semantics_bookkeeping_does_not_overwrite_itself(self):
        provider = ProbeProvider()
        mailbox = Mailbox()
        mailbox.register_provider(provider)
        await mailbox.start()

        try:
            address = addr_of('dup-id-fatal')
            seen = []

            async def handler(msg):
                seen.append(msg.body['n'])
                await asyncio.sleep(0.03)
                raise ValueError("malformed payload")  # 全部不可重试 → 各自死信

            with quiet():
                await mailbox.subscribe(address, handler)
                await mailbox.post(OutgoingMail(id='dup-1', from_="mem:py-sender", to=address, body={"n": 1}))
                await mailbox.post(OutgoingMail(id='dup-1', from_="mem:py-sender", to=address, body={"n": 2}))
                await asyncio.sleep(0.2)

            self.assertEqual(seen, [1, 2])
            self.assertEqual(provider.outstanding, 0)
            # 关键：两条投递各自结算，各自的 barrier 都跑完（修复前只有一条 barrier 会跑到死信钩子）
            self.assertEqual(len(provider.dead_lettered), 2)
            self.assertEqual(sorted(e['message'].body['n'] for e in provider.dead_lettered), [1, 2])
        finally:
            await mailbox.stop()

    async def test_semantics_same_id_retriable_failures_still_leave_one_copy(self):
        provider = ProbeProvider()
        mailbox = Mailbox()
        mailbox.register_provider(provider)
        await mailbox.start()

        try:
            address = addr_of('dup-id-retriable')
            calls = {'n': 0}

            async def handler(msg):
                calls['n'] += 1
                await asyncio.sleep(0.02)
                raise TransientError("upstream down")

            with quiet():
                await mailbox.subscribe(address, handler)
                await mailbox.post(OutgoingMail(id='dup-2', from_="mem:py-sender", to=address, body={"n": 1}))
                await mailbox.post(OutgoingMail(id='dup-2', from_="mem:py-sender", to=address, body={"n": 2}))
                await asyncio.sleep(0.2)

            self.assertEqual(calls['n'], 2)
            self.assertEqual(provider.outstanding, 0)

            status = await mailbox.status(address)
            self.assertEqual(status.unread_count, 1)  # never queued twice
            self.assertIsNotNone(await mailbox.fetch(address, FetchOptions(manual_ack=False)))
            self.assertIsNone(await mailbox.fetch(address, FetchOptions(manual_ack=False)))
        finally:
            await mailbox.stop()


class TestStuckSubscriberIsBounded(unittest.IsolatedAsyncioTestCase):
    """隐患4：卡死的订阅回调必须有界。"""

    async def test_semantics_hanging_callback_is_requeued_by_the_watchdog(self):
        provider = ProbeProvider()
        provider.delivery_settle_timeout = 0.02
        mailbox = Mailbox()
        mailbox.register_provider(provider)
        await mailbox.start()

        try:
            address = addr_of('hung-callback')

            async def handler(msg):
                await asyncio.get_running_loop().create_future()  # 永不 settle

            with quiet() as output:
                await mailbox.subscribe(address, handler)
                sent = await mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.15)

                self.assertEqual(provider.outstanding, 0)
                self.assertEqual((await mailbox.status(address)).unread_count, 1)
                recovered = await mailbox.fetch(address, FetchOptions(manual_ack=False))
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered.id, sent.id)

            self.assertIn("did not settle", output.getvalue())
        finally:
            await mailbox.stop()

    async def test_semantics_watchdog_settles_only_once(self):
        provider = ProbeProvider()
        provider.delivery_settle_timeout = 0.02
        mailbox = Mailbox()
        mailbox.register_provider(provider)
        await mailbox.start()

        try:
            address = addr_of('late-success-after-stall')
            side_effects = {'n': 0}

            async def handler(msg):
                await asyncio.sleep(0.08)  # 比看门狗晚
                side_effects['n'] += 1

            with quiet():
                await mailbox.subscribe(address, handler)
                await mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.2)

            self.assertEqual(side_effects['n'], 1)
            self.assertEqual(provider.outstanding, 0)
            self.assertEqual(len(provider.dead_lettered), 0)
            self.assertEqual((await mailbox.status(address)).unread_count, 1)  # 只有看门狗那一次回队
        finally:
            await mailbox.stop()

    async def test_semantics_watchdog_does_not_cut_a_healthy_retry_chain(self):
        provider = ProbeProvider(RetryPolicy(max_attempts=5, base_delay=0.005))
        provider.delivery_settle_timeout = 0.04
        mailbox = Mailbox()
        mailbox.register_provider(provider)
        await mailbox.start()

        try:
            address = addr_of('watchdog-not-cutting-retry')
            calls = {'n': 0}

            async def handler(msg):
                calls['n'] += 1
                await asyncio.sleep(0.02)  # 单次尝试 0.02s < 0.04s，但整条重试链 > 0.04s
                if calls['n'] < 3:
                    raise TransientError("upstream down")

            with quiet():
                await mailbox.subscribe(address, handler)
                await mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.3)

            self.assertEqual(calls['n'], 3)
            self.assertEqual(provider.outstanding, 0)
            self.assertEqual((await mailbox.status(address)).unread_count, 0)
        finally:
            await mailbox.stop()

    async def test_semantics_watchdog_does_not_cut_a_backoff_wait(self):
        provider = ProbeProvider(RetryPolicy(max_attempts=3, base_delay=0.08))
        provider.delivery_settle_timeout = 0.02
        mailbox = Mailbox()
        mailbox.register_provider(provider)
        await mailbox.start()

        try:
            address = addr_of('watchdog-not-cutting-backoff')
            calls = {'n': 0}

            async def handler(msg):
                calls['n'] += 1
                if calls['n'] < 2:
                    raise TransientError("upstream down")

            with quiet():
                await mailbox.subscribe(address, handler)
                await mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.3)

            self.assertEqual(calls['n'], 2)  # 退避 0.08s > 看门狗 0.02s，但不能被掐断
            self.assertEqual((await mailbox.status(address)).unread_count, 0)
        finally:
            await mailbox.stop()

    async def test_fact_settle_timeout_zero_disables_the_watchdog(self):
        provider = ProbeProvider()
        provider.delivery_settle_timeout = 0
        mailbox = Mailbox()
        mailbox.register_provider(provider)
        await mailbox.start()

        try:
            address = addr_of('watchdog-off')

            async def handler(msg):
                await asyncio.get_running_loop().create_future()  # 永不 settle

            with quiet() as output:
                await mailbox.subscribe(address, handler)
                await mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.1)

                self.assertEqual(provider.outstanding, 1)
                self.assertEqual((await mailbox.status(address)).unread_count, 0)

            self.assertNotIn("did not settle", output.getvalue())

            await mailbox.stop()
            # 关闭也不会替正在执行的回调下结论（否则事后成功的副作用会被再执行一次）。
            # 关掉看门狗 = 调用方自己保证回调会结算；否则这条消息既不在队列也不在死信里。
            self.assertEqual((await mailbox.status(address)).unread_count, 0)
        finally:
            await mailbox.stop()

    async def test_fact_watchdog_covers_the_gap_left_by_stop(self):
        provider = ProbeProvider()
        provider.delivery_settle_timeout = 0.02
        mailbox = Mailbox()
        mailbox.register_provider(provider)
        await mailbox.start()

        try:
            address = addr_of('watchdog-closes-gap')

            async def handler(msg):
                await asyncio.get_running_loop().create_future()  # 永不 settle

            with quiet():
                await mailbox.subscribe(address, handler)
                sent = await mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await mailbox.stop()  # 正在执行的投递不会被取消
                await asyncio.sleep(0.15)

            self.assertEqual(provider.outstanding, 0)
            recovered = await mailbox.fetch(address, FetchOptions(manual_ack=False))
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.id, sent.id)
        finally:
            await mailbox.stop()

    def test_fact_default_watchdog_exists_and_is_bounded(self):
        self.assertGreater(DEFAULT_DELIVERY_SETTLE_TIMEOUT, 0)
        self.assertEqual(MemoryProvider().delivery_settle_timeout, DEFAULT_DELIVERY_SETTLE_TIMEOUT)


class TestProducerStyleProvidersUseTheDeliveryEntry(unittest.IsolatedAsyncioTestCase):
    """隐患5：自己派发消息的 provider 也必须走基类投递入口。"""

    async def asyncSetUp(self):
        reset_bus()

    async def test_semantics_local_delivery_is_bookkept_too(self):
        class LocalProducerProvider(MemoryProvider):
            """模拟生产者型 provider（如 InputProvider）：组装一条消息，直接投给本地订阅者。"""

            def produce(self, subscription_id: str, message: MailMessage):
                return self._deliver_to_local_subscription(subscription_id, message)

            @property
            def outstanding(self) -> int:
                return self.pending_delivery_count

        provider = LocalProducerProvider()
        mailbox = Mailbox()
        mailbox.register_provider(provider)
        await mailbox.start()

        try:
            address = addr_of('local-producer')

            with quiet():
                sub = await mailbox.subscribe(address, failing())

                reply = MailMessage(
                    id='reply-1',
                    from_='mem:py-sender',
                    to=address,
                    body={'produced': True},
                    headers={},
                    meta={},
                )
                result = await provider.produce(sub.id, reply)
                await asyncio.sleep(0.05)

            self.assertIsInstance(result, DeliveryResult)
            self.assertFalse(result.ok)
            self.assertEqual(provider.outstanding, 0)  # 这次投递被记账并结算了
            self.assertEqual((await mailbox.status(address)).unread_count, 1)
            recovered = await mailbox.fetch(address, FetchOptions(manual_ack=False))
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.id, 'reply-1')
        finally:
            await mailbox.stop()


if __name__ == '__main__':
    unittest.main()
