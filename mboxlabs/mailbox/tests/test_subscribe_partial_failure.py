"""
`subscribe` 广播部分失败的投递语义（回归防线，与 TS 的
`src/mailbox.subscribe-partial-failure.test.ts` 同一组不变量）。

落地的语义：
  1. 每次投递（`delivery_id = subscription_id + message_id`）单独结算，结算发生在整条重试链
     落定之后；
  2. 广播的发送侧有结算 barrier：全部投递落定后才判定要不要回队；
  3. **只有「没有任何投递成功」且「至少一次投递可重试」才把整条消息退回队列**
     （`should_requeue_after_broadcast`）—— 保住 `fetch()` 兜底；
  4. 部分失败不回队，失败的投递走 `_on_delivery_deadlettered`，不会把已成功者处理过的
     消息再投一次（跨投递模式重复）；
  5. `post()` 仍然只等派发，不等投递结算。

命名标记：
  test_semantics_*  上面那几条不变量，破坏它们就是回归。
  test_fact_*       附带锁定的行为（`post()` 不阻塞、去重守卫等），不是核心承诺。
"""

import asyncio
import unittest

from mboxlabs.mailbox import Mailbox, MemoryProvider, OutgoingMail, FetchOptions, RetryPolicy
from mboxlabs.mailbox.delivery import DeliveryResult
from mboxlabs.mailbox.tests.helpers import TransientError, addr_of, failing, quiet, reset_bus


class DeadLetterProvider(MemoryProvider):
    """记录 `_on_delivery_deadlettered` 的 provider，用来观察不回队的那些失败投递。"""

    def __init__(self, retry=None):
        super().__init__(retry)
        self.dead_lettered = []

    def _on_delivery_deadlettered(self, message, failed):
        self.dead_lettered.append({'message': message, 'failed': failed})


class PartialFailureTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_bus()
        self.mailbox = Mailbox()
        self.provider = MemoryProvider()
        self.mailbox.register_provider(self.provider)
        await self.mailbox.start()

    async def asyncTearDown(self):
        await self.mailbox.stop()


class TestPartialFailureDoesNotRequeue(PartialFailureTestCase):
    """部分失败不回队，也不重复投递给已成功者。"""

    async def test_semantics_a_ok_b_retriable_does_not_requeue(self):
        address = addr_of('partial-no-requeue')
        seen = []

        async def ok(msg):
            seen.append(msg)

        with quiet():
            await self.mailbox.subscribe(address, ok)
            await self.mailbox.subscribe(address, failing())

            await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.05)

        self.assertEqual(len(seen), 1)
        self.assertEqual((await self.mailbox.status(address)).unread_count, 0)
        self.assertIsNone(await self.mailbox.fetch(address, FetchOptions(manual_ack=False)))

    async def test_semantics_mixed_subscribe_and_poll_runs_side_effect_once(self):
        address = addr_of('single-side-effect')
        side_effects = {'n': 0}

        def work():
            side_effects['n'] += 1

        with quiet():
            await self.mailbox.subscribe(address, lambda msg: work())  # 成功的订阅者
            await self.mailbox.subscribe(address, failing())

            await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.05)

            polled = await self.mailbox.fetch(address, FetchOptions(manual_ack=False))
            if polled:
                work()

        self.assertEqual(side_effects['n'], 1)

    async def test_semantics_pure_subscribe_leaves_no_orphan(self):
        address = addr_of('no-orphan')
        with quiet():
            await self.mailbox.subscribe(address, lambda msg: None)
            await self.mailbox.subscribe(address, failing())

            for i in range(1, 4):
                await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": i}))
            await asyncio.sleep(0.05)

        self.assertEqual((await self.mailbox.status(address)).unread_count, 0)

    async def test_semantics_resubscribe_does_not_replay_handled_message(self):
        address = addr_of('resubscribe')
        late = []

        with quiet():
            s1 = await self.mailbox.subscribe(address, failing())
            s2 = await self.mailbox.subscribe(address, lambda msg: None)  # 成功

            await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.05)
            await s1.unsubscribe()
            await s2.unsubscribe()

            await self.mailbox.subscribe(address, lambda msg: late.append(msg))
            await asyncio.sleep(0.05)

        self.assertEqual(len(late), 0)
        # 有订阅者成功过，消息已处理完毕，不再留在队列里等兜底
        self.assertEqual((await self.mailbox.status(address)).unread_count, 0)

    async def test_semantics_retry_exhausted_does_not_requeue_handled_message(self):
        mailbox = Mailbox()
        mailbox.register_provider(MemoryProvider(RetryPolicy(max_attempts=3, base_delay=0.001)))
        await mailbox.start()
        address = addr_of('exhaust-partial')
        counts = {'a': 0, 'b': 0}

        async def a(msg):
            counts['a'] += 1

        async def b(msg):
            counts['b'] += 1
            raise TransientError("upstream down")

        try:
            with quiet():
                await mailbox.subscribe(address, a)
                await mailbox.subscribe(address, b)
                await mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.2)

            self.assertEqual(counts['a'], 1)
            self.assertEqual(counts['b'], 3)  # 只有失败者被重投
            self.assertEqual((await mailbox.status(address)).unread_count, 0)
        finally:
            await mailbox.stop()

    async def test_semantics_unsubscribe_failing_sub_during_backoff_keeps_message_handled(self):
        mailbox = Mailbox()
        mailbox.register_provider(MemoryProvider(RetryPolicy(max_attempts=3, base_delay=0.5)))
        await mailbox.start()
        address = addr_of('cancel-partial')
        counts = {'a': 0}

        async def a(msg):
            counts['a'] += 1

        try:
            with quiet():
                await mailbox.subscribe(address, a)
                failing_sub = await mailbox.subscribe(address, failing())

                await mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.01)
                await failing_sub.unsubscribe()  # 取消待重投：投递结算为 cancelled
                await asyncio.sleep(0.1)

            self.assertEqual(counts['a'], 1)
            self.assertEqual((await mailbox.status(address)).unread_count, 0)
        finally:
            await mailbox.stop()

    async def test_semantics_failed_delivery_is_visible_in_dead_letter_hook(self):
        provider = DeadLetterProvider()
        dl_mailbox = Mailbox()
        dl_mailbox.register_provider(provider)
        await dl_mailbox.start()
        address = addr_of('dead-letter-partial')

        try:
            with quiet():
                ok_sub = await dl_mailbox.subscribe(address, lambda msg: None)
                bad_sub = await dl_mailbox.subscribe(address, failing())

                sent = await dl_mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.05)

            self.assertEqual(len(provider.dead_lettered), 1)
            entry = provider.dead_lettered[0]
            self.assertEqual(entry['message'].id, sent.id)
            # 只有失败的那一次投递被记账，而且带投递身份
            self.assertEqual(len(entry['failed']), 1)
            failed = entry['failed'][0]
            self.assertIsInstance(failed, DeliveryResult)
            self.assertEqual(failed.subscription_id, bad_sub.id)
            self.assertEqual(failed.reason, 'retriable')
            self.assertIn(bad_sub.id, failed.delivery_id)
            self.assertEqual(ok_sub.status, 'active')
        finally:
            await dl_mailbox.stop()

    async def test_semantics_cross_instance_broadcast_counts_other_subscribers(self):
        """别人家的订阅者成功也算成功（总线是进程级单例）。"""
        address = addr_of('cross-instance-partial')
        host = Mailbox()  # 成功订阅者住在这里
        host.register_provider(MemoryProvider())
        await host.start()

        poster = Mailbox()  # 失败订阅者 + 发送方在这里
        poster.register_provider(MemoryProvider())
        await poster.start()

        seen = []

        async def a(msg):
            seen.append(msg)

        try:
            with quiet():
                await host.subscribe(address, a)
                await poster.subscribe(address, failing())

                await poster.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.05)

            self.assertEqual(len(seen), 1)
            self.assertEqual((await host.status(address)).unread_count, 0)
            self.assertEqual((await poster.status(address)).unread_count, 0)
        finally:
            await poster.stop()
            await host.stop()


class TestAllDeliveriesFailedStillRequeue(PartialFailureTestCase):
    """全部投递失败时仍然回队（`fetch()` 兜底不能破）。"""

    async def test_semantics_all_subscribers_fail_leave_one_copy(self):
        address = addr_of('all-fail-dedup')
        with quiet():
            await self.mailbox.subscribe(address, failing())
            await self.mailbox.subscribe(address, failing())
            await self.mailbox.subscribe(address, failing())

            sent = await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.05)

        self.assertEqual((await self.mailbox.status(address)).unread_count, 1)  # never queued twice
        recovered = await self.mailbox.fetch(address, FetchOptions(manual_ack=False))
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.id, sent.id)
        self.assertIsNone(await self.mailbox.fetch(address, FetchOptions(manual_ack=False)))

    async def test_semantics_mixed_fatal_and_retriable_still_requeues(self):
        address = addr_of('mixed-fatal-retriable')

        async def fatal(msg):
            raise ValueError("malformed payload")

        with quiet():
            await self.mailbox.subscribe(address, fatal)
            await self.mailbox.subscribe(address, failing())

            sent = await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.05)

        recovered = await self.mailbox.fetch(address, FetchOptions(manual_ack=False))
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.id, sent.id)

    async def test_semantics_all_fatal_is_dead_lettered(self):
        address = addr_of('all-fatal')

        async def fatal(msg):
            raise ValueError("malformed payload")

        async def also_fatal(msg):
            raise ValueError("also malformed")

        with quiet():
            await self.mailbox.subscribe(address, fatal)
            await self.mailbox.subscribe(address, also_fatal)
            await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.05)

        self.assertEqual((await self.mailbox.status(address)).unread_count, 0)
        self.assertIsNone(await self.mailbox.fetch(address, FetchOptions(manual_ack=False)))

    async def test_semantics_retry_exhausted_with_no_success_requeues(self):
        mailbox = Mailbox()
        mailbox.register_provider(MemoryProvider(RetryPolicy(max_attempts=3, base_delay=0.001)))
        await mailbox.start()
        address = addr_of('exhaust-all-fail')
        calls = {'n': 0}

        async def b(msg):
            calls['n'] += 1
            raise TransientError("upstream down")

        try:
            with quiet():
                await mailbox.subscribe(address, b)
                sent = await mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.2)

            self.assertEqual(calls['n'], 3)
            self.assertEqual((await mailbox.status(address)).unread_count, 1)
            recovered = await mailbox.fetch(address, FetchOptions(manual_ack=False))
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.id, sent.id)
        finally:
            await mailbox.stop()

    async def test_semantics_only_failing_subscriber_unsubscribed_during_backoff_requeues(self):
        mailbox = Mailbox()
        mailbox.register_provider(MemoryProvider(RetryPolicy(max_attempts=5, base_delay=0.5)))
        await mailbox.start()
        address = addr_of('cancel-only-subscriber')

        try:
            with quiet():
                sub = await mailbox.subscribe(address, failing())
                sent = await mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.01)
                await sub.unsubscribe()
                await asyncio.sleep(0.1)

            self.assertEqual((await mailbox.status(address)).unread_count, 1)
            recovered = await mailbox.fetch(address, FetchOptions(manual_ack=False))
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.id, sent.id)
        finally:
            await mailbox.stop()


class TestSettlementBarrierTiming(PartialFailureTestCase):
    """结算 barrier 的时序。"""

    async def test_fact_slow_success_is_not_prejudged(self):
        address = addr_of('barrier-waits-slow-success')
        order = []

        async def a(msg):
            await asyncio.sleep(0.05)  # 成功，但需要一次 I/O
            order.append('A-success')

        async def b(msg):
            order.append('B-fail')
            raise TransientError("down")

        with quiet():
            await self.mailbox.subscribe(address, a)
            await self.mailbox.subscribe(address, b)

            await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
            await asyncio.sleep(0.01)

            # 旧行为：B 先失败就先回队，此刻 unread_count 已经是 1，而 A 的成功与否还没发生。
            self.assertEqual(order, ['B-fail'])
            self.assertEqual((await self.mailbox.status(address)).unread_count, 0)

            await asyncio.sleep(0.1)
            self.assertEqual(order, ['B-fail', 'A-success'])
            self.assertEqual((await self.mailbox.status(address)).unread_count, 0)

    async def test_fact_post_does_not_await_delivery_settlement(self):
        address = addr_of('post-not-awaiting-delivery')
        state = {'settled': False}

        async def handler(msg):
            await asyncio.sleep(0.03)
            state['settled'] = True

        with quiet():
            await self.mailbox.subscribe(address, handler)
            await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))

        self.assertFalse(state['settled'])  # 广播已派发，但未结算

        await asyncio.sleep(0.1)
        self.assertTrue(state['settled'])

    async def test_fact_delivery_settles_only_after_the_whole_retry_chain(self):
        provider = DeadLetterProvider(RetryPolicy(max_attempts=3, base_delay=0.05))
        mailbox = Mailbox()
        mailbox.register_provider(provider)
        await mailbox.start()
        address = addr_of('settle-after-retry-chain')
        calls = []

        async def handler(msg):
            calls.append(msg)
            if len(calls) < 3:
                raise TransientError("flaky")

        try:
            with quiet():
                await mailbox.subscribe(address, handler)
                await mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": 1}))
                await asyncio.sleep(0.01)

                self.assertEqual(len(calls), 1)  # 退避中：还没结算，也就没有死信记录
                self.assertEqual(len(provider.dead_lettered), 0)
        finally:
            await mailbox.stop()


class TestRetryAndManualAckInterplay(PartialFailureTestCase):
    """opt-in 重试与手动 ack 交叉。"""

    async def test_fact_in_flight_hold_is_not_duplicated_by_partial_broadcast(self):
        address = addr_of('inflight-cross')
        await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": "queued"}))
        held = await self.mailbox.fetch(address, FetchOptions(manual_ack=True))
        self.assertIsNotNone(held)
        self.assertEqual((await self.mailbox.status(address)).in_flight_count, 1)

        seen = []

        async def a(msg):
            seen.append(msg)

        with quiet():
            await self.mailbox.subscribe(address, a)
            await self.mailbox.subscribe(address, failing())
            await self.mailbox.post(OutgoingMail(from_="mem:py-sender", to=address, body={"n": "broadcast"}))
            await asyncio.sleep(0.05)

        status = await self.mailbox.status(address)
        self.assertEqual(status.in_flight_count, 1)
        self.assertEqual(status.unread_count, 0)  # A 已处理过：不再回队

        await held.ack()
        self.assertIsNone(await self.mailbox.fetch(address, FetchOptions(manual_ack=False)))
        self.assertEqual(len(seen), 1)


if __name__ == '__main__':
    unittest.main()
