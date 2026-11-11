"""In-memory pub/sub for Server-Sent Events.

Subscribers live in this process only, which is why NowDrop runs a single
Uvicorn worker. Correctness of the race never depends on this module: the
atomic SQL update decides winners, events only tell browsers about it.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, AsyncIterator

PING_SECONDS = 15


class Broker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def publish(self, channel: str, event: str, data: Any) -> None:
        for queue in list(self._subscribers.get(channel, ())):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                pass  # a stuck client must never block the race

    def publish_show(self, show: dict, event: str, data: Any) -> None:
        """Public event: goes to buyers and to the seller console."""
        self.publish(f"show:{show['slug']}", event, data)
        self.publish(f"seller:{show['id']}", event, data)

    def publish_seller(self, show_id: str, event: str, data: Any) -> None:
        self.publish(f"seller:{show_id}", event, data)

    def subscriber_count(self, channel: str) -> int:
        return len(self._subscribers.get(channel, ()))

    async def stream(self, channel: str, is_disconnected=None) -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers[channel].add(queue)
        try:
            yield ": connected\n\n"
            while True:
                if is_disconnected is not None and await is_disconnected():
                    break
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        finally:
            self._subscribers[channel].discard(queue)
            if not self._subscribers[channel]:
                self._subscribers.pop(channel, None)
