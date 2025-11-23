from typing import TypeVar, Generic, Dict, Deque, Optional, List
from collections import deque
from datetime import datetime, timedelta
import time

T = TypeVar('T')

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
        """
        if ack_timeout:
            self.requeue_stale(topic, ack_timeout)

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
                self.requeue_internal(flight.topic, flight.message)

    def get_status(self, topic: str) -> int:
        return len(self.queues.get(topic, []))

    def requeue_internal(self, topic: str, message: T) -> None:
        if topic not in self.queues:
            self.queues[topic] = deque()
        self.queues[topic].appendleft(message)

    def requeue_stale(self, topic: str, timeout: float) -> None:
        now = time.time()
        stale_ids = []

        for msg_id, flight in self.in_flight.items():
            if flight.topic == topic and (now - flight.timestamp) > timeout:
                stale_ids.append(msg_id)

        for msg_id in stale_ids:
            flight = self.in_flight.pop(msg_id)
            self.requeue_internal(flight.topic, flight.message)
