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
python -m pytest tests/
```

### Basic Example

```python
import asyncio
from mailbox import Mailbox, MemoryProvider, OutgoingMail

async def main():
    # 1. Create mailbox instance and register a memory provider
    mailbox = Mailbox()
    mailbox.register_provider(MemoryProvider())

    # 2. Subscribe to an address and define message handler
    async def on_receive(message):
        print(f"Received message! From: {message.from_}")
        print(f"Content: {message.body}")

    subscription = await mailbox.subscribe(
        "mem:service@example.com/inbox",
        on_receive
    )

    print("Mailbox established, listening on 'mem:service@example.com/inbox'...")

    # 3. Send a message to that address
    await mailbox.post(OutgoingMail(
        from_="mem:client@example.com/user-1",
        to="mem:service@example.com/inbox",
        body={"text": "Hello, Mailbox!"}
    ))

    # Give async tasks time to complete
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

### 1. Subscribe Pattern (Push)

```python
subscription = await mailbox.subscribe(
    "mem:service/inbox",
    lambda msg: print(f"Got: {msg.body}")
)

# Unsubscribe when done
await subscription.unsubscribe()
```

### 2. Fetch Pattern (Pull)

**Auto-acknowledgment:**
```python
from mailbox import FetchOptions

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

### 3. Status Query

```python
status = await mailbox.status("mem:service/inbox")
print(f"State: {status.state}")
print(f"Unread: {status.unread_count}")
```

## 🏗️ Architecture

### Provider Interface

Implement the `MailboxProvider` abstract class to create custom providers:

```python
from abc import ABC, abstractmethod
from mailbox import MailboxProvider, MailMessage, MailboxStatus, FetchOptions

class MyCustomProvider(MailboxProvider):
    def __init__(self):
        super().__init__("myprotocol")

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
```

### Built-in Providers

- **MemoryProvider**: In-memory message bus for local communication
  - Topic-based routing
  - FIFO queue with manual/auto acknowledgment
  - Stale message requeueing
  - Thread-safe singleton event bus

## 🧪 Testing

Run the test suite:

```bash
cd packages/mailbox
export PYTHONPATH=$PYTHONPATH:$(pwd)/python
python3 -m unittest discover -s python/mailbox/tests -p "test_*.py"
```

Tests include:
- Send and subscribe
- Fetch with auto-ack
- Fetch with manual-ack
- Nack with requeue
- Status queries

## 📚 Project Structure

```
python/mailbox/
├── __init__.py           # Package exports
├── core.py               # Main Mailbox class
├── message.py            # Message data structures
├── provider.py           # Abstract provider base class
├── error.py              # Exception definitions
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
