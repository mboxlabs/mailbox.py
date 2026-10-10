# 📮 Mailbox — Python Implementation

> A lightweight, pluggable "mailbox/queue" kernel that treats all communication as "delivering a letter to an address". An address represents a unique mailbox, accessible through different transport protocols (like `mem:`, `mailto:`, `slack:`) via pluggable Providers.
> Build fault-tolerant, distributed, human-machine collaborative systems using mailbox-based async communication.

## 🌟 Why Mailbox?

| Traditional Approach | Mailbox Approach |
|---------------------|------------------|
| ❌ Shared state + locks | ✅ Independent mailboxes + messages |
| ❌ Callback hell | ✅ `async/await` seamless integration |
| ❌ Complex human-machine collaboration | ✅ Human = a mailbox address |
| ❌ Offline scenarios difficult | ✅ Messages auto-buffered and retried |

### Erlang Inspiration

> _🙏 Tribute: Erlang's Actor Model_
> _"In the 1980s, when computers were as big as rooms,
> Erlang's creators proposed a revolutionary idea:
> **Each process has its own mailbox, communicates via messages, and crashes are part of the design, not failures**"_
> —— Joe Armstrong, Robert Virding, Mike Williams

Mailbox is **deeply inspired by Erlang's Actor model**, but with key evolution:

| Erlang (1986) | Mailbox (Today) | Why It Matters |
|---------------|-----------------|----------------|
| `Pid ! Message` | `send({ to: 'protocol://address' })` | **Address is identity, protocol is routing**: `address` part is the mailbox's unique ID. `protocol` (e.g. `mem`, `mailto`) determines routing. Same address accessible via different protocols. |
| In-process FIFO mailbox | Pluggable Providers | **Transport-agnostic**: Seamlessly switch between memory/email/Wechat/Mastodon |
| Same-node communication | Cross-network, cross-organization | **Truly distributed**: Humans and machines participate equally |

## 🚀 Quick Start

### Installation

**From source:**

```bash
# Clone the repository
cd packages/mailbox/python

# Install in development mode
pip install -e .

# Or install with development dependencies
pip install -e ".[dev]"
```

**From PyPI (when published):**

```bash
pip install mboxlabs-mailbox
```

**For development:**

```bash
# Install development dependencies
pip install -r requirements-dev.txt

# Run tests
export PYTHONPATH=$(pwd)/python
pytest python/mboxlabs/mailbox/tests/
```

### Basic Example

```python
import asyncio
from mboxlabs.mailbox import Mailbox, MemoryProvider, OutgoingMail

async def main():
    # 1. Create mailbox instance and register a memory provider
    mailbox = Mailbox()
    mailbox.register_provider(MemoryProvider())

    # 2. Start the mailbox (initializes providers)
    await mailbox.start()

    # 3. Subscribe to an address and define message handler
    async def on_receive(message):
        print(f"Received message! From: {message.from_}")
        print(f"Content: {message.body}")

    subscription = await mailbox.subscribe(
        "mem:service@example.com/inbox",
        on_receive
    )

    print("Mailbox established, listening on 'mem:service@example.com/inbox'...")

    # 4. Send a message to that address
    await mailbox.post(OutgoingMail(
        from_="mem:client@example.com/user-1",
        to="mem:service@example.com/inbox",
        body={"text": "Hello, Mailbox!"}
    ))

    # 5. Clean up: Unsubscribe and stop the mailbox (releases resources)
    await subscription.unsubscribe()
    await mailbox.stop()

    # Give async tasks time to complete (if needed)
    await asyncio.sleep(0.1)

if __name__ == "__main__":
    asyncio.run(main())
```

Output:
```
Mailbox established, listening on 'mem:service@example.com/inbox'...
Received message! From: mem:client@example.com/user-1
Content: {'text': 'Hello, Mailbox!'}
```

## 📪 Mailbox Address

A **MailAddress** is the foundation of the entire system, serving as the unique universal identifier for any destination. It follows the [RFC 3986](https://tools.ietf.org/html/rfc3986) URI specification.

- **Format**: `protocol:user@physical_address[/logical_address]`
- **Example**: `mem:api@myservice.com/utils/greeter`

A mailbox address consists of three parts:

- **`protocol`**: Specifies the **transport method** for messages (e.g., `mem` for memory bus, `mailto` for email). It tells `Mailbox` which provider should handle the message.
- **`user@physical_address`**: The **globally unique, protocol-agnostic ID** of the logical mailbox or service. The same physical address can be accessed via different protocols (e.g., `mem:api@myservice.com` and `mailto:api@myservice.com` point to the same logical entity).
- **`/logical_address`** (optional): An optional path for internal routing. For example, when combined with `tool-rpc`, it can route messages to specific tools within a larger service, allowing one physical address to serve as a unified gateway for multiple logical functions.

## 🎯 Core Features

There are two ways to receive mail, and one message goes to **one of them only**, decided
when `post()` runs:

| State of the address when `post()` runs | Where the message goes |
|------|------|
| at least one active subscriber | handed to every subscriber's callback; **not** queued, so `fetch()` gets nothing |
| no subscriber | queued; `fetch()` gets it, subscribers get nothing |

Mutually exclusive per message: a message already handed to a subscriber can never be
fetched, and a queued message is never replayed to a subscriber that appears later.

### 1. Subscribe

```python
subscription = await mailbox.subscribe(
    "mem:service/inbox",
    lambda msg: print(f"Got: {msg.body}")
)

# Unsubscribe when done
await subscription.unsubscribe()
```

If the callback raises, the error decides the message's fate: retriable errors
(`is_retriable = True`, or `code` in `503 / 504 / ECONNRESET / ETIMEDOUT`) put the message
back in the queue, where `fetch()` can pick it up; anything else is dropped, so one bad
message is not retried forever.

```python
class UpstreamDown(Exception):
    is_retriable = True

async def handler(msg):
    raise UpstreamDown("upstream down")   # → 回到队列，计入 unread_count
```

### 2. Fetch

**Auto-acknowledgment:**
```python
from mboxlabs.mailbox import FetchOptions

msg = await mailbox.fetch(
    "mem:service/inbox",
    FetchOptions(manual_ack=False)
)

if msg:
    print(f"Fetched: {msg.body}")
    # Auto-acknowledged
```

**Manual acknowledgment:**
```python
ackable = await mailbox.fetch(
    "mem:service/inbox",
    FetchOptions(manual_ack=True, ack_timeout=5.0)  # 5 seconds
)

if ackable:
    # Process message...
    await ackable.ack()  # Acknowledge
    # Or: await ackable.nack(requeue=True)  # Negative ack with requeue
```

`ack_timeout` 的三态语义（与 TS / Rust 端一致）：

| 传值 | 含义 |
|------|------|
| `None`（缺省） | 用 `DEFAULT_ACK_TIMEOUT`（30 秒），未 ack 的消息会重新入队 |
| 正数 | 该超时（秒） |
| `0` | 关闭 stale 扫描，消息常驻 in-flight 直到 ack/nack |

不传 ≠ 永不超时：否则崩溃的消费者会把消息永久卡在 in-flight。

### 3. Status Query

```python
status = await mailbox.status("mem:service/inbox")
print(f"State: {status.state}")
print(f"Unread: {status.unread_count}")        # 仍在队列里等待
print(f"In flight: {status.in_flight_count}")  # 已取出、尚未 ack/nack
print(f"Subscribers: {status.extra['subscriber_count']}")
```

`unread_count` 只统计队列里等待的消息；用 `manual_ack=True` 取出的消息单独计入
`in_flight_count`，一直不 ack 会回到队列。

### 4. Accessing Providers

You can gracefully access registered providers via the `providers` property or `get_provider` method:

```python
# Get all providers (returns a copy of the mapping)
all_providers = mailbox.providers
if "mem" in all_providers:
    print("Memory provider is registered")

# Get a specific provider
# protocol can be "mem" or "mem:"
provider = mailbox.get_provider("mem")

# Get provider or raise error if not found
provider = mailbox.get_provider("slack", raise_error_if_failed=True)
```

### 5. Subscriber Failures and Retry (opt-in)

What a subscriber callback raises decides where the message goes: a retriable error
(`is_retriable = True`, or `code` in `503 / 504 / ECONNRESET / ETIMEDOUT`) requeues the
message so `fetch()` can pick it up; anything else is dropped, so one bad message is not
retried forever.

By default nothing calls the callback again. Opt into automatic redelivery per provider:

```python
from mboxlabs.mailbox import MemoryProvider, RetryPolicy

provider = MemoryProvider(RetryPolicy(max_attempts=3, base_delay=1.0))  # seconds
mailbox.register_provider(provider)
```

Only the failing subscriber is re-invoked — subscribing is a broadcast, and re-broadcasting
would run the side effects of the subscribers that already succeeded a second time. Attempts
are counted per (subscriber, message); when the budget runs out the message goes back to the
queue, and unsubscribing while a retry is pending returns it to the queue as well. A message
is never queued twice: `requeue()` skips one that is already queued or in flight.

Retry is opt-in because AMQP / SQS style transports already redeliver and ship dead-letter
queues; retrying on top of them would double-deliver. `provider.set_retry_policy(None)`
returns to the default behaviour, and a provider subclass can override
`_on_retry_exhausted(message, attempts)` to dead-letter or alert.

The policy pieces are importable on their own (`mboxlabs.mailbox.retry`: `RetryPolicy`,
`resolve_retry_policy`, `is_retriable_error`, `next_retry_delay`, `AttemptLedger`) so other
packages can reuse the same retriable check and backoff instead of writing their own.

## 🏗️ Architecture

### Provider Interface

Implement the `MailboxProvider` abstract class to create custom providers:

```python
from abc import ABC, abstractmethod
from typing import Optional
from mboxlabs.mailbox import (
    MailboxProvider, MailMessage, MailboxStatus, FetchOptions, RetryPolicy
)

class MyCustomProvider(MailboxProvider):
    def __init__(self, retry: Optional[RetryPolicy] = None):  # retry is opt-in
        super().__init__("myprotocol", retry)

    async def init(self) -> None:
        # Optional: Initialize connections, pre-warm caches
        pass

    async def _close(self) -> None:
        # Optional: Specific closing logic, release resources
        pass

    async def _send(self, message: MailMessage) -> None:
        # Implement sending logic
        pass

    async def _subscribe(self, address: str, on_receive) -> Any:
        # Implement subscription logic
        # Return an unsubscribe handle
        pass

    async def _unsubscribe(self, subscription_id: str, handle: Any) -> None:
        # Implement unsubscription logic
        pass

    async def _fetch(self, address: str, options: FetchOptions):
        # Implement fetch logic
        pass

    async def _status(self, address: str) -> MailboxStatus:
        # Implement status query logic
        pass

    async def _nack(self, message: MailMessage, requeue: bool) -> None:
        # Optional: called when a subscriber callback fails. Implement it if your transport
        # can put the message back; otherwise a retriable failure is simply lost.
        pass

    def _on_retry_exhausted(self, message: MailMessage, attempts: int) -> None:
        # Optional: dead-letter or alert once the retry budget is spent. The message still
        # goes back to the queue afterwards.
        pass
```

### Built-in Providers

- **MemoryProvider**: In-memory message bus for local communication
  - Topic-based routing
  - FIFO queue with manual/auto acknowledgment
  - Stale message requeueing, with requeue deduplication (a message is never queued twice)
  - Opt-in subscription retry (`RetryPolicy`); by default a failed subscriber only requeues
    the message and nothing re-invokes the callback
  - Thread-safe singleton event bus

## 🧪 Testing

Run the test suite from the package root directory. **Note: You must set `PYTHONPATH` to include the `python` directory so the `mboxlabs` module can be found.**

```bash
# Set PYTHONPATH and run tests
export PYTHONPATH=$(pwd)/python
pytest python/mboxlabs/mailbox/tests/test_mailbox.py
```

Tests include:
- Send and subscribe
- Fetch with auto-ack
- Fetch with manual-ack
- Nack with requeue
- Status queries, including in-flight visibility
- Subscribe and fetch are mutually exclusive per message
- Requeue deduplication and the retry policy helpers
- Opt-in subscription retry: targeted redelivery, exhausted budget falls back to the queue

Run them with either of:

```bash
python3 -m pytest -q          # from the python/ directory
pytest python/mboxlabs/mailbox/tests/test_mailbox.py   # from the package root, with PYTHONPATH set
```

## 📚 Project Structure

```
python/mboxlabs/mailbox/
├── __init__.py           # Package exports
├── core.py               # Main Mailbox class
├── message.py            # Message data structures
├── provider.py           # Abstract provider base class
├── error.py              # Exception definitions
├── retry.py              # Retry policy, backoff and attempt accounting
├── utils.py              # Utility functions
├── providers/
│   ├── memory.py         # In-memory provider
│   └── queue.py          # Generic message queue
└── tests/
    └── test_mailbox.py   # Test suite
```

## 🔧 Dependencies

- **Python 3.7+**: For async/await and dataclasses
- **Standard Library Only**: No external dependencies required

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE-MIT](../../LICENSE-MIT) file for details.

## 🙏 Acknowledgments

Inspired by:
- Erlang's Actor Model and OTP
- The original TypeScript Mailbox implementation
- The Rust Mailbox implementation
- The principle that "the postal system has worked for 500 years because it doesn't assume the recipient is waiting at the door!"

---

> **Remember**: In the Mailbox world, **every mailbox is an independent universe, and messages are messengers traveling through space and time** 🌌
