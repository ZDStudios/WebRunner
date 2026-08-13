"""In-process publish/subscribe used to push live output and events to browsers."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

MAX_QUEUE = 512


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, topic: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._subscribers[topic].add(queue)
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(topic)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(topic, None)

    def publish(self, topic: str, message: Any) -> None:
        """Non-blocking fan-out. A browser that cannot keep up drops frames
        rather than stalling the scheduler."""
        for queue in list(self._subscribers.get(topic, ())):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()          # drop the oldest frame
                    queue.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def subscriber_count(self, topic: str) -> int:
        return len(self._subscribers.get(topic, ()))


bus = EventBus()


def publish_event(kind: str, **payload: Any) -> None:
    """Broadcast a UI event such as ``run.started`` or ``job.updated``."""
    bus.publish("events", {"event": kind, **payload})
