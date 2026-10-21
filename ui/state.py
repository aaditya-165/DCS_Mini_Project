from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Event:
    ts: float
    kind: str
    message: str
    node: str | None = None
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        item = asdict(self)
        item["time"] = time.strftime("%H:%M:%S", time.localtime(self.ts))
        return item


class EventBus:
    def __init__(self, max_events: int = 250):
        self._events: deque[Event] = deque(maxlen=max_events)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(
        self,
        kind: str,
        message: str,
        node: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        event = Event(time.time(), kind, message, node, data)
        self._events.append(event)
        payload = event.to_dict()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._publish, payload)

    def _publish(self, payload: dict[str, Any]) -> None:
        stale = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                stale.append(queue)
        for queue in stale:
            self._subscribers.discard(queue)

    def recent(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events]

    async def subscribe(self):
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)


event_bus = EventBus()
