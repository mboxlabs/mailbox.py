import asyncio
import unittest
from mailbox import Mailbox, MemoryProvider, OutgoingMail, FetchOptions, MailboxStatus

class TestMailbox(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mailbox = Mailbox()
        self.provider = MemoryProvider()
        self.mailbox.register_provider(self.provider)

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

if __name__ == '__main__':
    unittest.main()
