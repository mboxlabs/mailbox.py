"""投递记账工具的单元测试（与 TS 的 `src/lib/delivery.test.ts` 同语义）。"""

import unittest

from mboxlabs.mailbox.delivery import (
    DeliveryResult,
    delivery_key,
    is_delivery_result,
    message_id_of_delivery,
    should_requeue_after_broadcast,
    subscription_id_of_delivery,
    to_delivery_results,
)


def a_delivery(**overrides) -> DeliveryResult:
    """默认：一次可重试失败的投递。"""
    fields = {
        'delivery_id': delivery_key('sub1', 'msg1'),
        'subscription_id': 'sub1',
        'message_id': 'msg1',
        'ok': False,
        'reason': 'retriable',
    }
    fields.update(overrides)
    return DeliveryResult(**fields)


class TestDeliveryKey(unittest.TestCase):
    """投递身份 = 订阅者 + 消息。"""

    def test_can_be_split_back_into_two_parts(self):
        key = delivery_key('sub1', 'msg1')
        self.assertEqual(subscription_id_of_delivery(key), 'sub1')
        self.assertEqual(message_id_of_delivery(key), 'msg1')

    def test_different_subscribers_are_different_deliveries(self):
        # 订阅是广播：同一个订阅者和另一个订阅者对同一条消息是两次独立投递
        self.assertNotEqual(delivery_key('a', 'm'), delivery_key('b', 'm'))


class TestShouldRequeueAfterBroadcast(unittest.TestCase):
    """只有无人成功才回队。"""

    def test_no_deliveries_do_not_requeue(self):
        self.assertFalse(should_requeue_after_broadcast([]))

    def test_some_delivery_succeeded_do_not_requeue(self):
        # 部分失败不重复投递：回队等于把已成功者的副作用再跑一遍
        self.assertFalse(should_requeue_after_broadcast([
            a_delivery(ok=True, reason=None),
            a_delivery(reason='retriable'),
        ]))

    def test_all_failed_with_at_least_one_retriable_requeues(self):
        # fetch() 兜底
        self.assertTrue(should_requeue_after_broadcast([
            a_delivery(reason='retriable'),
            a_delivery(reason='fatal'),
        ]))

    def test_cancelled_retry_counts_as_retriable(self):
        self.assertTrue(should_requeue_after_broadcast([a_delivery(reason='cancelled')]))

    def test_all_fatal_is_dead_letter_not_requeue(self):
        self.assertFalse(should_requeue_after_broadcast([
            a_delivery(reason='fatal'),
            a_delivery(reason='fatal'),
        ]))


class TestToDeliveryResults(unittest.TestCase):
    """第三方监听器的结算值归一化。"""

    def test_passes_delivery_results_through(self):
        result = a_delivery(reason='fatal')
        self.assertEqual(to_delivery_results([result]), [result])
        self.assertTrue(is_delivery_result(result))
        self.assertFalse(is_delivery_result(None))

    def test_non_delivery_result_value_counts_as_success(self):
        normalized = to_delivery_results([None])[0]
        self.assertTrue(normalized.ok)
        self.assertIsNone(normalized.reason)

    def test_raised_error_counts_as_retriable_failure(self):
        # 宁可多兜一次：requeue 的去重守卫会挡住重复
        normalized = to_delivery_results([RuntimeError('boom')])[0]
        self.assertFalse(normalized.ok)
        self.assertEqual(normalized.reason, 'retriable')

    def test_duck_typed_results_are_normalized(self):
        class CustomResult:
            ok = False
            reason = 'cancelled'
            subscription_id = 'sub9'
            message_id = 'msg9'
            delivery_id = delivery_key('sub9', 'msg9')

        [normalized] = to_delivery_results([CustomResult()])
        self.assertIsInstance(normalized, DeliveryResult)
        self.assertEqual(normalized.reason, 'cancelled')
        self.assertEqual(normalized.subscription_id, 'sub9')


if __name__ == '__main__':
    unittest.main()
