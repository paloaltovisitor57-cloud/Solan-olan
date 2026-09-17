"""Tiny in-process event bus. Publishing never blocks the hot path; each subscriber has its own
bounded queue and consumer task, so a slow subscriber (storage) cannot stall the engine."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from solana_sniper.domain.events import Event, SnapshotObserved, TradeObserved
from solana_sniper.telemetry.logging import get_logger

log = get_logger(__name__)

Subscriber = Callable[[Event], Awaitable[None]]
DROPPABLE = (SnapshotObserved, TradeObserved)


class EventBus:
    def __init__(self, queue_size: int = 10_000) -> None:
        self._subs: list[tuple[str, Subscriber, asyncio.Queue[Event]]] = []
        self._queue_size = queue_size
        self._tasks: list[asyncio.Task[None]] = []
        self.dropped = 0
        self.published = 0

    def subscribe(self, name: str, handler: Subscriber) -> None:
        self._subs.append((name, handler, asyncio.Queue(maxsize=self._queue_size)))

    def publish(self, event: Event) -> None:
        self.published += 1
        for _name, _handler, queue in self._subs:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                if isinstance(event, DROPPABLE):
                    self.dropped += 1
                    continue
                # Non-droppable event: make room by discarding the oldest droppable item.
                self._evict_one(queue)
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    @staticmethod
    def _evict_one(queue: asyncio.Queue[Event]) -> None:
        kept: list[Event] = []
        evicted = False
        while not queue.empty():
            item = queue.get_nowait()
            if not evicted and isinstance(item, DROPPABLE):
                evicted = True
                continue
            kept.append(item)
        for item in kept:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(item)

    def start(self) -> None:
        for name, handler, queue in self._subs:
            self._tasks.append(
                asyncio.create_task(self._consume(name, handler, queue), name=f"bus-{name}")
            )

    async def _consume(self, name: str, handler: Subscriber, queue: asyncio.Queue[Event]) -> None:
        while True:
            event = await queue.get()
            try:
                await handler(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(
                    "bus_subscriber_error",
                    subscriber=name,
                    event_type=type(event).__name__,
                    error=str(exc),
                )
            finally:
                queue.task_done()

    async def drain(self, timeout_s: float = 5.0) -> None:
        """Wait for subscriber queues to empty (used at shutdown)."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(q.join() for _, _, q in self._subs)), timeout=timeout_s
            )

    async def stop(self) -> None:
        await self.drain()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
