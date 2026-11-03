"""
issue#4 第 2 点 —— 地址里的 query / hash 会被拒绝，不再静默塌成同一个 topic。
（与 TS 的 `src/mailbox.address-canonicalization.test.ts` + `src/lib/utils.test.ts` 同一组不变量。）

`get_canonical_mailbox_address_identifier()` 只用 `protocol://user@host/path`，`query` / `fragment`
会被丢掉 —— 于是 `mem:q@example.com/inbox?thread=1` 和 `mem:q@example.com/inbox` 是**同一个** topic。

采用的修法是 issue 里倾向的那一种：明确禁止。地址语义留在 path 里，query / hash 不参与寻址，
因此带它们的地址在**每个**入口都直接报错（而不是「看起来能用、实际共用一条队列」）。
"""

import unittest

from mboxlabs.mailbox import (
    FetchOptions,
    InvalidMailboxAddressError,
    Mailbox,
    MailMessage,
    MemoryProvider,
    OutgoingMail,
    assert_lossless_mailbox_address,
    get_canonical_mailbox_address_identifier,
    parse_mailbox_address,
)

canon = get_canonical_mailbox_address_identifier


def new_mailbox(provider):
    mailbox = Mailbox()
    mailbox.register_provider(provider)
    return mailbox


class TestCanonicalIdentifier(unittest.TestCase):
    """canonical id 的形状：`protocol://user@host/path`，不带 query / hash，抹掉密码。"""

    def test_canonical_id_is_protocol_user_host_path(self):
        self.assertEqual(canon("mem://bob@example.com:8080/inbox"), "mem://bob@example.com:8080/inbox")
        self.assertEqual(canon("mem://bob:secret@example.com/inbox"), "mem://bob@example.com/inbox")

    def test_opaque_and_hierarchical_forms_land_on_the_same_topic(self):
        self.assertEqual(canon("mem:alice@example.com/inbox"), canon("mem://alice@example.com/inbox"))

    def test_canonical_id_is_idempotent(self):
        once = canon("mem://alice@example.com/inbox/thread-1")
        self.assertEqual(canon(once), once)

    def test_query_is_rejected_instead_of_trimmed_off(self):
        with self.assertRaisesRegex(InvalidMailboxAddressError, "would be dropped by address canonicalization"):
            canon("mem:q@example.com/inbox?thread=1")

        # 塌缩的直接后果：query 版与裸地址版 canonical id 相同 —— 必须无法发生
        self.assertEqual(canon("mem:q@example.com/inbox"), "mem://q@example.com/inbox")

    def test_hash_is_rejected_and_the_message_names_the_dropped_part(self):
        with self.assertRaises(InvalidMailboxAddressError) as caught:
            assert_lossless_mailbox_address("mem:h@example.com/inbox#frag", "status()")
        message = str(caught.exception)
        self.assertIn('hash "#frag" would be dropped', message)
        self.assertIn("in status()", message)

    def test_query_and_hash_together_are_both_reported(self):
        with self.assertRaisesRegex(InvalidMailboxAddressError, r'query "\?x=1" and hash "#f"'):
            assert_lossless_mailbox_address("mem:a@example.com/i?x=1#f")

    def test_empty_query_and_empty_hash_count_too(self):
        # 写法不同、意图相同：都会被规范化丢掉，所以一并拒绝
        with self.assertRaisesRegex(InvalidMailboxAddressError, r'empty query \("\?"\)'):
            assert_lossless_mailbox_address("mem:a@example.com/i?")
        with self.assertRaisesRegex(InvalidMailboxAddressError, r'empty hash \("#"\)'):
            assert_lossless_mailbox_address("mem:a@example.com/i#")

    def test_percent_encoded_markers_are_path_content_and_stay_legal(self):
        assert_lossless_mailbox_address("mem:a@example.com/inbox%3Fthread%3D1%23frag")  # must not raise
        self.assertEqual(canon("mem:a@example.com/inbox%3Fthread%3D1"), "mem://a@example.com/inbox%3Fthread%3D1")

    def test_error_carries_the_address_and_a_stable_code(self):
        with self.assertRaises(InvalidMailboxAddressError) as caught:
            parse_mailbox_address("mem:q@example.com/inbox?thread=1", "post()")
        error = caught.exception
        self.assertEqual(error.code, "MBX_INVALID_ADDRESS")
        self.assertEqual(error.address, "mem:q@example.com/inbox?thread=1")


class TestParseMailboxAddress(unittest.TestCase):
    def test_returns_the_parsed_address_and_keeps_the_spelling(self):
        parsed = parse_mailbox_address("mem:a@example.com/inbox")
        self.assertEqual(parsed.scheme, "mem")
        self.assertEqual(canon("mem:a@example.com/inbox"), canon("mem://a@example.com/inbox"))

    def test_unparseable_input_becomes_an_address_error(self):
        with self.assertRaises(InvalidMailboxAddressError) as caught:
            parse_mailbox_address("not a mailbox address", "subscribe()")
        error = caught.exception
        self.assertIn("in subscribe()", str(error))
        self.assertIn("not a parseable URL", str(error))
        self.assertEqual(error.address, "not a mailbox address")


class TestAddressRulesAtEveryEntry(unittest.IsolatedAsyncioTestCase):
    """同一个地址不能只在一个入口合法：`post` / `subscribe` / `fetch` / `status` 规则一致。"""

    async def asyncSetUp(self):
        self.provider = MemoryProvider.create_isolated()
        self.mailbox = new_mailbox(self.provider)
        await self.mailbox.start()

    async def asyncTearDown(self):
        await self.mailbox.stop(purge=True)

    async def test_post_to_a_query_address_raises_and_delivers_nothing(self):
        with self.assertRaises(InvalidMailboxAddressError):
            await self.mailbox.post(OutgoingMail(
                from_="mem:x@example.com/u", to="mem:q@example.com/inbox?thread=1", body={"n": 2}))

        # 旧行为：不带 query 的 status 也会看到这封信（两条业务流共用一条队列）
        status = await self.mailbox.status("mem:q@example.com/inbox")
        self.assertFalse(status.exists)
        self.assertEqual(status.unread_count, 0)

    async def test_post_to_a_hash_address_raises(self):
        with self.assertRaisesRegex(InvalidMailboxAddressError, 'hash "#frag" would be dropped'):
            await self.mailbox.post(OutgoingMail(
                from_="mem:x@example.com/u", to="mem:h@example.com/inbox#frag", body={"n": 1}))
        self.assertFalse((await self.mailbox.status("mem:h@example.com/inbox")).exists)

    async def test_the_from_address_is_bound_by_the_same_rules(self):
        with self.assertRaises(InvalidMailboxAddressError):
            await self.mailbox.post(OutgoingMail(
                from_="mem:x@example.com/u?route=direct", to="mem:q@example.com/inbox", body={}))

    async def test_subscribe_fetch_and_status_reject_it_too(self):
        address = "mem:s@example.com/inbox?thread=1"

        with self.assertRaises(InvalidMailboxAddressError):
            await self.mailbox.subscribe(address, lambda msg: None)
        with self.assertRaises(InvalidMailboxAddressError):
            await self.mailbox.fetch(address, FetchOptions())
        with self.assertRaises(InvalidMailboxAddressError):
            await self.mailbox.status(address)

        # 订阅侧曾经是最隐蔽的一半：订阅 `?thread=1` 实际订阅的是裸地址的队列
        status = await self.mailbox.status("mem:s@example.com/inbox")
        self.assertEqual(status.extra["subscriber_count"], 0)
        self.assertFalse(status.exists)

    async def test_the_message_shows_which_address_it_would_merge_into(self):
        try:
            await self.mailbox.post(OutgoingMail(
                from_="mem:x@example.com/u", to="mem:q@example.com/inbox?thread=1", body={}))
        except InvalidMailboxAddressError as error:
            self.assertIn("mem:q@example.com/inbox?thread=1", str(error))
            self.assertIn("mem://q@example.com/inbox", str(error))
            self.assertIn("in post()", str(error))
        else:
            self.fail("带 query 的地址应该被拒绝")

    async def test_using_the_provider_directly_is_blocked_the_same_way(self):
        """绕过 Mailbox 直接用 provider 也拦得住（canonicalization 自己就是防线）。"""
        with self.assertRaises(InvalidMailboxAddressError):
            await self.provider.send(MailMessage(
                id="direct-1",
                from_="mem://x@example.com/u",
                to="mem://q@example.com/inbox?thread=1",
                body={},
            ))

        with self.assertRaises(InvalidMailboxAddressError):
            await self.provider.fetch("mem://q@example.com/inbox#frag", FetchOptions())

        with self.assertRaises(InvalidMailboxAddressError):
            await self.provider.subscribe("mem://q@example.com/inbox?thread=1", lambda msg: None)

        # 三个入口都没留下队列 / 订阅痕迹
        self.assertFalse(self.provider.bus.get_status("mem://q@example.com/inbox")["exists"])

    async def test_logical_partitions_live_in_the_path(self):
        thread_one = "mem:q@example.com/inbox/thread-1"
        thread_two = "mem:q@example.com/inbox/thread-2"

        await self.mailbox.post(OutgoingMail(from_="mem:x@example.com/u", to=thread_one, body={"thread": 1}))
        await self.mailbox.post(OutgoingMail(from_="mem:x@example.com/u", to=thread_two, body={"thread": 2}))

        self.assertEqual((await self.mailbox.status(thread_one)).unread_count, 1)
        self.assertEqual((await self.mailbox.status(thread_two)).unread_count, 1)

        # 取一个分区不会动到另一个分区
        self.assertEqual((await self.mailbox.fetch(thread_one, FetchOptions())).body, {"thread": 1})
        self.assertEqual((await self.mailbox.status(thread_one)).unread_count, 0)
        self.assertEqual((await self.mailbox.status(thread_two)).unread_count, 1)
        self.assertEqual((await self.mailbox.fetch(thread_two, FetchOptions())).body, {"thread": 2})

    async def test_opaque_and_hierarchical_spelling_is_still_the_same_address(self):
        await self.mailbox.post(OutgoingMail(from_="mem:x@example.com/u", to="mem:eq@example.com/inbox", body={"n": 1}))
        self.assertEqual((await self.mailbox.status("mem://eq@example.com/inbox")).unread_count, 1)
        self.assertEqual((await self.mailbox.fetch("mem://eq@example.com/inbox", FetchOptions())).body, {"n": 1})


if __name__ == "__main__":
    unittest.main()
