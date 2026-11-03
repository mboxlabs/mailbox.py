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

**Query strings and fragments are not part of an address.** An address is normalized to
`protocol://user@host/path` before it becomes a topic (the canonical id keeps the protocol so it can
be parsed back — `get_canonical_mailbox_address_identifier()` is idempotent), so anything after `?`
or `#` would be dropped silently: `mem:sales@example.com/inbox?thread=1` and
`mem:sales@example.com/inbox` would end up sharing one queue and one delivery path, invisibly.
Instead of merging two logical addresses, Mailbox rejects such addresses at every entry point
(`post()`, `subscribe()`, `fetch()`, `status()`) with an `InvalidMailboxAddressError` — including the
spellings with an empty `?` or `#`. Put what distinguishes the logical partitions in the path:
`mem:sales@example.com/inbox/thread-1`. Percent-encoded `%3F` / `%23` are path content and stay legal.

## 🎯 Core Features

There are two ways to receive mail, and one message goes to **one of them only**, decided
when `post()` runs:

| State of the address when `post()` runs | Where the message goes |
|------|------|
| at least one active subscriber | handed to every subscriber's callback; **not** queued, so `fetch()` gets nothing |
| no subscriber | queued; `fetch()` gets it, subscribers get nothing |

Mutually exclusive per message: a message already handed to a subscriber can never be
fetched, and a queued message is never replayed to a subscriber that appears later.

There are three verbs, three APIs — use these words consistently: `post()` writes, `subscribe()`
registers a callback, `fetch()` asks for the next message itself. `fetch(addr,
FetchOptions(peek=True))` is not a fourth way to receive: it receives nothing, it only looks.

### 1. Subscribe

```python
subscription = await mailbox.subscribe(
    "mem:service/inbox",
    lambda msg: print(f"Got: {msg.body}")
)

# Unsubscribe when done
await subscription.unsubscribe()
```

If the callback raises, the error decides the message's fate: a retriable error
(`is_retriable = True`, or `code` in `503 / 504 / ECONNRESET / ETIMEDOUT`) puts the message
back in the queue, where `fetch()` can pick it up; anything else is dropped, so one bad
message is not retried forever.

```python
class UpstreamDown(Exception):
    is_retriable = True

async def handler(msg):
    raise UpstreamDown("upstream down")   # → 回到队列，计入 unread_count
```

One address can have several subscribers, and subscribing is a **broadcast**: one message
becomes one _delivery_ per subscriber, and each delivery settles on its own
(`delivery_id = subscription_id + message_id`). That decides what happens when subscribers
disagree — A succeeds, B throws a retriable error:

| 广播结果              | 邮箱的处理                                                          |
| --------------------- | ------------------------------------------------------------------- |
| 有投递成功            | 消息已处理——**不**回队；失败的投递交给 `_on_delivery_deadlettered` |
| 全部失败且可重试      | 消息退回队列头部，由 `fetch()` 取回（只有一份）                     |
| 全部失败且不可重试    | 直接丢弃，避免一条坏消息被无限重试                                  |

把“已经有人处理过”的消息退回队列，等于让 `fetch()` 消费者把同一段副作用再跑一遍——所以部分
失败按投递结算，不整条回队。结算发生在全部投递（含重试链）落定之后：`post()` 只等派发、不等
结算，因此 `post()` 之后立刻读 `status()` 看不出结果。

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

**Read-only peek:**
```python
pending = await mailbox.fetch("mem:service/inbox", FetchOptions(peek=True, limit=5))
for msg in pending:
    print(msg.body)   # 快照里的副本，改它改不到队列
```

`peek` returns **copies** of the queued messages as a list (`[]` when the queue is idle), and it
changes nothing: `unread_count` and `in_flight_count` stay put, `last_activity_time` is not refreshed,
and mutating what you got back cannot touch the queue. Before `peek` existed, the closest trick was
`fetch(addr, FetchOptions(manual_ack=True))` without acking — that parks the message in-flight
(requeued after the 30s ack timeout by default), which is not read-only and distorts what real
consumers see. `peek` never lists messages another consumer is holding; those only show up as
`in_flight_count`. Combining `peek` with `manual_ack` throws (`InvalidFetchOptionsError`), a bad
`limit` (negative, fractional, `NaN`) throws too, and a provider that cannot do read-only snapshots
raises `UnsupportedCapabilityError` rather than silently falling back to a consuming fetch.
`limit=None` (or `math.inf`) means the whole queue.

### 3. Status Query

```python
status = await mailbox.status("mem:service/inbox")
print(f"State: {status.state}")            # 'online' | 'unknown' | 'degraded' | 'offline'
print(f"Exists: {status.exists}")          # does the provider know this address at all?
print(f"Unread: {status.unread_count}")    # 仍在队列里等待
print(f"In flight: {status.in_flight_count}")  # 已取出、尚未 ack/nack
print(f"Subscribers: {status.extra['subscriber_count']}")
print(f"Last activity: {status.last_activity_time}")  # ISO 8601, or None
```

`unread_count` 只统计队列里等待的消息；用 `manual_ack=True` 取出的消息单独计入
`in_flight_count`，一直不 ack 会回到队列。

`state` describes the provider's view **of that address**. An address the provider has never seen
(never posted to, never subscribed, no recorded activity) reports `state="unknown"` and
`exists=False` — reporting `online` + 0 for it would make a dashboard unable to tell "this address
does not exist" from "this address is idle". A drained but real address stays `online`; so does an
address that only has subscribers. `exists` is left `None` when the provider cannot answer status
queries at all: "I cannot ask" is not "it does not exist". `last_activity_time` is `None` when there
is no record — never a missing field, which would vanish entirely from `json.dumps`. Reading status
never refreshes it.

Diagnostics pages and tests usually want the whole picture at once. Call `status()` with no address
(or `snapshot()`, same view) to get every topic of every registered provider:

```python
snap = await mailbox.status()
print(snap.state)          # 'online' | 'degraded' | 'unknown'
print(snap.address_count)  # topics known across the registered providers
print(snap.unread_count, snap.in_flight_count)
for entry in snap.addresses:
    print(entry.canonical_id, entry.protocol, entry.state, entry.unread_count)
```

`addresses` is sorted by `canonical_id` so snapshots are reproducible, and each entry can be queried
back with `mailbox.status(entry.canonical_id)`. Providers whose messages do not live locally
(AMQP / SQS style brokers) cannot enumerate their topics; they are listed in
`unsupported_providers` and the snapshot degrades to `state="degraded"` instead of pretending the
queues are empty.

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

|                  | 默认（不传 `retry`）          | 传入策略                        |
| ---------------- | ----------------------------- | ------------------------------- |
| 可重试失败       | 回队一次，由 `fetch()` 取回   | 按指数退避重新调用回调          |
| 尝试次数         | 1                             | `max_attempts`（含首次）        |
| 次数耗尽         | —                             | 没有任何投递成功时回队          |

Three rules keep this from duplicating work:

- **Only the failing subscriber is re-invoked** — subscribing is a broadcast, and re-broadcasting
  would run the side effects of the subscribers that already succeeded a second time.
- **A message is never queued twice** — `requeue()` skips one that is already queued or in flight,
  so N failing subscribers leave exactly one copy in the queue.
- **A handled message is never requeued** — if any delivery of the broadcast succeeded, the failing
  ones are dead-lettered (`_on_delivery_deadlettered`) instead of putting the whole message back;
  requeue is reserved for "every subscriber failed".

Attempts are counted per (subscriber, message); when the budget runs out the delivery settles as
`retriable`, and unsubscribing while a retry is pending settles it as `cancelled`.

Retry is opt-in because AMQP / SQS style transports already redeliver and ship dead-letter
queues; retrying on top of them would double-deliver. `provider.set_retry_policy(None)`
returns to the default behaviour, a provider subclass can override
`_on_retry_exhausted(message, attempts)` to alert once the budget is spent, and
`_on_delivery_deadlettered(message, failed_deliveries)` to persist the failed deliveries that
will **not** be requeued.

The policy pieces are importable on their own (`mboxlabs.mailbox.retry`: `RetryPolicy`,
`resolve_retry_policy`, `is_retriable_error`, `next_retry_delay`, `AttemptLedger`) so other
packages can reuse the same retriable check and backoff instead of writing their own. So is the
delivery bookkeeping (`mboxlabs.mailbox.delivery`: `DeliveryResult`, `delivery_key`,
`should_requeue_after_broadcast`, `to_delivery_results`).

### 6. Settlement Safety Nets (timeouts, races, provider contracts)

A broadcast is only settled once **every** delivery of it settles — that is what makes the
per-delivery requeue rule above work, and it is also where the hazards hide. Three safety nets,
same as the TypeScript side:

- **A stuck callback cannot make a message invisible.** Every delivery _attempt_ is watched by
  `provider.delivery_settle_timeout` (default `DEFAULT_DELIVERY_SETTLE_TIMEOUT` = 30 seconds).
  When it fires the attempt settles as `cancelled`: the message falls back to `fetch()` (or the
  dead-letter hook) and a warning is printed. The watchdog covers one attempt only — a backoff
  wait is never counted — so it cannot cut a healthy retry chain short. Setting
  `provider.delivery_settle_timeout = 0` opts out: you then own the lifecycle.
- **`unsubscribe()` and `stop()` never prejudge a running callback.** Only deliveries sitting in a
  retry backoff are cancelled. A callback that is still running settles on its own; declaring it
  failed would make the broadcast look like "nobody succeeded" and hand the same side effect to a
  `fetch()` consumer a second time.
- **Concurrent deliveries of the same `id` settle independently.** `post(OutgoingMail(id=...))`
  accepts caller-supplied ids, so two in-flight deliveries to one subscriber never share
  settlement bookkeeping. Dedup still applies where it belongs: a message already queued or in
  flight is never queued twice.

If you implement your own **broadcast** provider (`broadcast_deliveries = True`), `_send` must
attach the settlement barrier — synchronously, before any `await`:

```python
async def _send(self, message: MailMessage) -> None:
    deliveries = self.bus.publish(topic, message)  # one awaitable per delivery
    self._track_broadcast_settlement(message, deliveries)
```

A provider that forgets it does not lose messages silently: the base class falls back to a
whole-message requeue and warns you. Producer-style providers that build a message themselves and
hand it to a local subscriber must use `_deliver_to_local_subscription(subscription_id, message)`,
otherwise that delivery is never accounted for.

### 7. Instance isolation and state reset (`mem:` provider)

`MemoryProvider` simulates a broker on an in-memory event bus. By default every `MemoryProvider`
shares one process-wide bus, so all `Mailbox` instances in the process see the same `mem:` queues
and subscription tables, and that state outlives `stop()`. This is intentional — it is what lets
two `Mailbox` instances talk to each other — but it means you have to choose the boundary
explicitly when you want independence (host + plugin, kernel + provider, or tests that must not
leak into each other):

```python
# 1. Private bus — no one else in the process can see its queues or subscriptions.
isolated = MemoryProvider.create_isolated()

# 2. Named namespace — shared within a scope, invisible across scopes.
tenant = MemoryProvider.for_scope("tenant-a")

# 3. Bring your own bus — e.g. two Mailboxes that are meant to share one "server".
shared = MemoryProvider(bus=MemoryEventBus())
```

Reset state without tearing subscriptions down:

```python
await mailbox.clear()                    # drop every provider's backlog (queued + in-flight)
await mailbox.stop(purge=True)           # close subscriptions, then drop the backlog
provider.bus.clear("inbox@example.com")  # one topic only; returns how many messages were dropped
```

The topic handed to `MemoryEventBus.clear()` is the canonical identifier
(`protocol://user@host/path`, as produced by `get_canonical_mailbox_address_identifier()`), because
that is the key the bus files the queue under.

Caveats worth knowing:

- `clear()` drops messages, never subscriptions. Closing a channel is `unsubscribe()` /
  `Mailbox.stop()`'s job — a `Subscription` left `active` while nothing is ever delivered to it is
  worse than a visible backlog.
- `stop()` never deletes messages either: a provider that is closed and started again still sees the
  backlog left behind. Use `stop(purge=True)` to drop it.
- On the default bus, `clear()` clears the whole `mem:` namespace of the process — that is what a
  shared "server" means. Use `create_isolated()` / `for_scope()` when that is not what you want.
- One provider instance must not be registered on two Mailboxes: subscriptions live on the provider,
  so `stop()` on one unsubscribes the other. The library warns once when it sees this.
- The scope registry is never garbage-collected: a scope name generated per run (timestamp, random
  id) pins its bus for the lifetime of the process. Use `create_isolated()` for throwaway state.
- `MailboxProvider.clear()` is a **no-op by default**: purging a real broker (AMQP / SQS / IMAP) is
  a destructive operation and its messages do not live in this process, so the base class does not
  decide it for its subclasses. Providers that own their storage override it.

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
        # Implement sending logic. A *broadcasting* provider (one that hands the message to
        # several subscribers) must also attach the settlement barrier, synchronously and
        # before any await:
        #     deliveries = self.bus.publish(topic, message)   # one awaitable per delivery
        #     self._track_broadcast_settlement(message, deliveries)
        # `post()` never waits for the deliveries; the barrier decides, once they have all
        # settled, whether the message goes back to the queue.
        pass

    async def _subscribe(self, address: str, on_receive) -> Any:
        # Implement subscription logic.
        # `on_receive(message)` returns the delivery's final settlement (awaitable of
        # DeliveryResult); return an unsubscribe handle.
        pass

    async def _unsubscribe(self, subscription_id: str, handle: Any) -> None:
        # Implement unsubscription logic
        pass

    async def _fetch(self, address: str, options: FetchOptions):
        # Implement fetch logic. The base class never sends `peek=True` here: read-only
        # snapshots go to `_peek`.
        pass

    async def _peek(self, address: str, limit=None) -> list:
        # Optional: read-only snapshot — return copies of the queued messages (never dequeue,
        # never build in-flight state, never bump activity). A provider that does not implement
        # this raises UnsupportedCapabilityError on `fetch(FetchOptions(peek=True))` instead of
        # silently consuming the messages. `limit` is already normalized: None = the whole queue.
        pass

    async def _status(self, address: str) -> MailboxStatus:
        # Optional: status query logic. Not implementing it reports state='unknown' and leaves
        # `exists` unset ("cannot ask" is not "does not exist"). An address this provider has
        # never seen must report state='unknown' + exists=False, not 'online' + 0.
        pass

    async def list_addresses(self) -> list:
        # Optional: canonical ids of the addresses this provider knows. Only providers whose
        # messages live locally can implement it; the base class raises NotImplementedError and
        # `Mailbox.snapshot()` files such providers under `unsupported_providers`.
        pass

    async def _nack(self, message: MailMessage, requeue: bool) -> None:
        # Optional: for broadcast providers this is called ONCE per broadcast, after every
        # delivery settled (the barrier decides `requeue`); it is not called once per failing
        # subscriber. Implement it if your transport can put the message back.
        pass

    def _on_retry_exhausted(self, message: MailMessage, attempts: int) -> None:
        # Optional: alert or persist once the retry budget is spent. If nobody handled the
        # message it still goes back to the queue afterwards.
        pass

    def _on_delivery_deadlettered(self, message: MailMessage, failed) -> None:
        # Optional: a delivery failed and the message will NOT be requeued (partial failure,
        # or every failure fatal). `failed` is a list of DeliveryResult. Without this hook
        # those deliveries are simply invisible.
        pass
```

Two switches on the base class are worth knowing:

| 开关 | 默认 | 作用 |
| ---- | ---- | ---- |
| `broadcast_deliveries` | `False` | `True`：`subscribe` 是广播（一条消息 = N 次投递），失败按投递结算，整条消息回不回队交给 barrier；`False`：每次投递对应 broker 的一次 ack，保持 per-delivery `nack` |
| `delivery_settle_timeout` | `DEFAULT_DELIVERY_SETTLE_TIMEOUT`（30 秒） | 单次尝试的结算看门狗；`0` 关闭 |

Producer-style providers that build a message themselves and hand it to a local subscriber must
use `_deliver_to_local_subscription(subscription_id, message)` instead of calling the callback
directly — that is where the delivery gets accounted for (implicit ack, retries, settlement).

### Built-in Providers

- **MemoryProvider**: In-memory message bus for local communication
  - Topic-based routing
  - FIFO queue with manual/auto acknowledgment
  - Stale message requeueing, with requeue deduplication (a message is never queued twice)
  - Per-delivery settlement: `broadcast_deliveries = True`, `_send` attaches the barrier, so a
    partial failure never requeues a message somebody already handled
  - Opt-in subscription retry (`RetryPolicy`); by default a failed subscriber only requeues
    the message and nothing re-invokes the callback
  - Instance boundaries: the default constructor shares the process-wide bus (cross-`Mailbox`
    messaging, and the backlog surviving `stop()`); `create_isolated()` gives a provider a private
    bus, `for_scope(name)` a namespace, and the constructor takes `bus=` for anything custom
  - Purgeable: `MailMessageQueue.clear()` / `MemoryEventBus.clear(topic=None)` / `MemoryProvider.clear()`
    / `Mailbox.clear()` / `Mailbox.stop(purge=True)` drop queued **and** in-flight messages and
    report how many were dropped; a late `ack()` / `nack(requeue)` on a dropped message is a no-op
  - Read-only snapshot: `MemoryEventBus.peek(topic, limit=None)` / `MailMessageQueue.peek()` back
    `fetch(addr, FetchOptions(peek=True))` — copies, no dequeue, no in-flight, no activity bump
  - Honest status: `exists` tells "this address does not exist" from "idle", `last_activity_time`
    is `None` rather than a missing field, and reading status or peeking never refreshes it
  - Enumerable: `list_addresses()` / `MemoryEventBus.list_topics()` feed `Mailbox.snapshot()`;
    `MailMessageQueue.has_topic()` / `topics()` back the `exists` decision

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
- Honest status (`issue#4`): an address the provider never saw reports `state="unknown"` +
  `exists=False`; drained and subscriber-only addresses stay `online`; `last_activity_time` is
  `None` (survives a JSON round trip); reading status never refreshes it; a provider that cannot
  answer leaves `exists` unset
- Snapshots: `status()` / `snapshot()` list every topic sorted by `canonical_id`, aggregate the
  counts, and file providers that cannot enumerate their addresses under `unsupported_providers`
  (snapshot degrades to `state="degraded"`)
- Address rules (`issue#4`): query / hash (empty ones included) are rejected at `post()`,
  `subscribe()`, `fetch()` and `status()` instead of silently merging two logical partitions into
  one topic; opaque and hierarchical spellings of one address still land on the same topic;
  canonical ids are idempotent and can be queried back
- Read-only peek (`issue#4`): copies, no dequeue, no in-flight, no activity bump, `limit` bounds
  and validation, `peek` + `manual_ack` rejected, a provider without `_peek` raises
  `UnsupportedCapabilityError` instead of consuming the queue
- Subscribe and fetch are mutually exclusive per message
- Requeue deduplication and the retry policy helpers
- Opt-in subscription retry: targeted redelivery, exhausted budget falls back to the queue
- Per-delivery settlement: partial failure is not requeued, all-fail still requeues (one copy),
  the dead-letter hook sees what was dropped
- Settlement safety nets: a running callback is never prejudged, a missing barrier falls back to
  a whole-message requeue (plus a warning), concurrent deliveries of the same `id` settle
  independently, the settle watchdog bounds stuck callbacks without cutting retry chains short,
  and producer-style deliveries are accounted for
- Instance isolation and state reset: the shared default bus keeps working, `create_isolated()` /
  `for_scope()` / `bus=` do not leak into each other, the `clear()` chain (queue → bus → provider →
  mailbox) drops queued and in-flight messages without touching subscriptions, `stop(purge=True)`
  closes then purges, and reusing one provider instance across Mailboxes warns once

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
├── provider.py           # Abstract provider base class (delivery accounting, retry, barrier)
├── delivery.py           # DeliveryResult, delivery identity and the requeue decision
├── error.py              # Exception definitions (codes shared with the TS / Rust ports)
├── retry.py              # Retry policy, backoff and attempt accounting
├── utils.py              # Address parsing / canonical id / peek limit + copy helpers
├── providers/
│   ├── memory.py         # In-memory provider
│   └── queue.py          # Generic message queue
└── tests/
    ├── helpers.py                    # 唯一 topic、日志屏蔽、共用订阅者桩
    ├── test_mailbox.py               # 基础语义（含队列 clear）
    ├── test_delivery.py              # 投递记账工具
    ├── test_subscribe_partial_failure.py  # 部分失败不回队（issue#2）
    ├── test_delivery_settlement.py   # 结算竞态与兜底
    ├── test_memory_isolation.py      # 实例边界与状态清理（issue#3）
    ├── test_address_canonicalization.py  # 地址不再被 query / hash 静默合并（issue#4）
    ├── test_status_diagnostics.py    # status 的 exists / 全量快照（issue#4）
    └── test_peek.py                  # 只读快照（issue#4）
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
