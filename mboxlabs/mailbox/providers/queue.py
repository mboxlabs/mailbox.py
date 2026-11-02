from typing import TypeVar, Generic, Dict, Deque, Optional, List
from collections import deque
from datetime import datetime, timedelta
import time

T = TypeVar('T')

# 手动 ACK 模式下未显式指定 ack_timeout 时使用的默认超时（秒）。
# in-flight 消息必须有界：否则消费者崩溃后既取不回来、也不会
# 在状态里可见，等于静默丢失。传 0 可显式关闭超时（常驻 in-flight）。
# 该缺省值与 ack_timeout 的三态语义在 TS / Python / Rust 三端保持一致。
DEFAULT_ACK_TIMEOUT = 30.0

class InFlightMessage(Generic[T]):
    def __init__(self, message: T, topic: str):
        self.message = message
        self.topic = topic
        self.timestamp = time.time()

class MailMessageQueue(Generic[T]):
    """
    A generic message queue that supports ack/nack and in-flight tracking.
    """
    def __init__(self):
        self.queues: Dict[str, Deque[T]] = {}
        self.in_flight: Dict[str, InFlightMessage[T]] = {}

    def enqueue(self, topic: str, message: T) -> None:
        if topic not in self.queues:
            self.queues[topic] = deque()
        self.queues[topic].append(message)

    def dequeue(self, topic: str) -> Optional[T]:
        if topic in self.queues and self.queues[topic]:
            return self.queues[topic].popleft()
        return None

    def dequeue_for_ack(self, topic: str, ack_timeout: Optional[float], get_id_fn: callable) -> Optional[T]:
        """
        Dequeues a message and moves it to in-flight.
        get_id_fn: A function that takes a message (T) and returns its ID (str).
        ack_timeout: 秒。None/负值 → DEFAULT_ACK_TIMEOUT；0 → 关闭 stale 扫描（消息常驻
        in-flight 直到 ack/nack）；正值 → 该超时。不能把“不传”当作“永不超时”，
        那会让未 ack 的消息永久泄漏。
        """
        timeout = DEFAULT_ACK_TIMEOUT if ack_timeout is None or ack_timeout < 0 else ack_timeout
        if timeout != 0:
            self.requeue_stale(topic, timeout)

        if topic in self.queues and self.queues[topic]:
            message = self.queues[topic].popleft()
            msg_id = get_id_fn(message)

            self.in_flight[msg_id] = InFlightMessage(message, topic)
            return message
        return None

    def ack(self, message_id: str) -> None:
        if message_id in self.in_flight:
            del self.in_flight[message_id]

    def nack(self, message_id: str, requeue: bool) -> None:
        if message_id in self.in_flight:
            flight = self.in_flight.pop(message_id)
            if requeue:
                self.requeue_internal(flight.topic, flight.message, msg_id=message_id)

    def get_status(self, topic: str) -> Dict[str, int]:
        """unread_count = 队列里等待的消息；in_flight_count = 已交给消费者、尚未 ack/nack。
        只有 unread_count 可以当作“待处理”；in-flight 的消息未 ack 时会回到队列。"""
        return {
            'unread_count': len(self.queues.get(topic, deque())),
            'in_flight_count': self.in_flight_count(topic),
        }

    def in_flight_count(self, topic: str) -> int:
        return sum(1 for flight in self.in_flight.values() if flight.topic == topic)

    def requeue(self, topic: str, message: T, get_id_fn: Optional[callable] = None) -> bool:
        """Public: 把消息放回队列头部。用于订阅者处理失败后的回队。

        去重：同一 id 已在该 topic 队列里、或已被某个消费者取走（in-flight）时不再插副本。
        一个地址有多个订阅者时，每个失败的订阅者都会触发一次回队；不去重就会在队列里
        留下 N 份同一消息（实测：2 个订阅者失败 → unread_count 为 2）。

        返回 True 表示确实回到队列；False 表示因重复而跳过。
        """
        return self.requeue_internal(topic, message, get_id_fn=get_id_fn)

    def requeue_internal(self, topic: str, message: T, msg_id: Optional[str] = None,
                         get_id_fn: Optional[callable] = None) -> bool:
        if msg_id is None:
            msg_id = self._id_of(message, get_id_fn)
        if msg_id is not None:
            if msg_id in self.in_flight:
                return False  # 已有消费者在处理，再插一份就是重复投递
            for queued in self.queues.get(topic, ()):  # 已在队列里等待，不需要第二份
                if self._id_of(queued, get_id_fn) == msg_id:
                    return False
        if topic not in self.queues:
            self.queues[topic] = deque()
        self.queues[topic].appendleft(message)
        return True

    @staticmethod
    def _id_of(message: T, get_id_fn: Optional[callable]) -> Optional[str]:
        """取消息 id：优先用调用方给的函数，否则退到 `message.id`。"""
        if get_id_fn is not None:
            return get_id_fn(message)
        return getattr(message, 'id', None)

    def has(self, message_id: str, get_id_fn: Optional[callable] = None) -> bool:
        """某个消息是否已在队列中等待或已被取走（in-flight）。与 TS 的 `has` 对齐。"""
        if message_id in self.in_flight:
            return True
        for queued in self.queues.values():
            for message in queued:
                if self._id_of(message, get_id_fn) == message_id:
                    return True
        return False

    def clear(self, topic: Optional[str] = None) -> int:
        """
        清空队列状态：不传 `topic` 则清空所有 topic。

        in-flight 的消息**也**会被丢弃：它同样是队列状态的一部分，只清「等待中」的那一半，
        下一轮（或同进程的另一个消费者 / 另一个测试用例）还是会看到残留消息。
        被丢弃的 in-flight 消息之后 `ack()` / `nack(requeue)` 都是 no-op —— 不会复活成幽灵消息。

        :param topic: 只清这个 topic；省略则清空全部。
        :return: 被丢弃的消息数（含 in-flight），便于诊断与断言。
        """
        if topic is None:
            dropped = len(self.in_flight)
            for queued in self.queues.values():
                dropped += len(queued)
            self.queues.clear()
            self.in_flight.clear()
            return dropped

        dropped = 0
        queued = self.queues.pop(topic, None)
        if queued:
            dropped += len(queued)
        for msg_id, flight in list(self.in_flight.items()):
            if flight.topic == topic:
                del self.in_flight[msg_id]
                dropped += 1
        return dropped

    def requeue_stale(self, topic: str, timeout: float) -> None:
        now = time.time()
        stale_ids = []

        for msg_id, flight in self.in_flight.items():
            if flight.topic == topic and (now - flight.timestamp) > timeout:
                stale_ids.append(msg_id)

        for msg_id in stale_ids:
            flight = self.in_flight.pop(msg_id)
            self.requeue_internal(flight.topic, flight.message, msg_id=msg_id)
