"""Per-story serial execution and global SQLite write serialization.

Port of InterludeService.queues / databaseWriteQueue semantics:
- one asyncio chain per story id; a failed task never blocks the next
- a single global write chain so SQLite never sees concurrent writers
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


class SerialQueues:
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Future] = {}

    async def run(self, key: str, task: Callable[[], Awaitable[T]]) -> T:
        previous = self._queues.get(key)
        loop = asyncio.get_running_loop()
        gate: asyncio.Future = loop.create_future()
        if previous is not None and not previous.done():
            chained: asyncio.Future = asyncio.ensure_future(
                _chain(previous, task, loop)
            )
        else:
            chained = asyncio.ensure_future(_run_task(task))
        self._queues[key] = chained
        gate.set_result(True)

        def _release(fut: asyncio.Future) -> None:
            if self._queues.get(key) is chained:
                self._queues.pop(key, None)

        chained.add_done_callback(_release)
        return await chained

    def pending(self, key: str) -> bool:
        fut = self._queues.get(key)
        return fut is not None and not fut.done()


async def _chain(
    previous: asyncio.Future,
    task: Callable[[], Awaitable[T]],
    _loop: object,
) -> T:
    try:
        await asyncio.shield(_suppress(previous))
    except asyncio.CancelledError:  # pragma: no cover - cancellation passthrough
        raise
    except Exception:
        pass
    return await _run_task(task)


async def _run_task(task: Callable[[], Awaitable[T]]) -> T:
    return await task()


async def _suppress(fut: asyncio.Future) -> None:
    try:
        await fut
    except Exception:
        pass


class WriteQueue:
    """Global serialized write lane with bounded transient-error retry."""

    def __init__(self, max_retries: int = 7) -> None:
        self._tail: asyncio.Future | None = None
        self.max_retries = max_retries

    async def submit(self, task: Callable[[], Awaitable[T]], retryable=None) -> T:
        loop = asyncio.get_running_loop()
        prior = self._tail
        result_future: asyncio.Future = loop.create_future()

        async def runner() -> None:
            if prior is not None and not prior.done():
                try:
                    await asyncio.shield(_suppress(prior))
                except Exception:
                    pass
            attempt = 0
            while True:
                try:
                    value = await task()
                    if not result_future.done():
                        result_future.set_result(value)
                    return
                except asyncio.CancelledError:
                    if not result_future.done():
                        result_future.cancel()
                    raise
                except Exception as error:
                    if (
                        retryable is None
                        or not retryable(error)
                        or attempt >= self.max_retries
                    ):
                        if not result_future.done():
                            result_future.set_exception(error)
                        return
                    delay = min(5.0, 0.1 * (2 ** attempt)) + (attempt % 3) * 0.05
                    attempt += 1
                    await asyncio.sleep(delay)

        runner_future = asyncio.ensure_future(runner())

        def _advance(_: asyncio.Future) -> None:
            if self._tail is runner_future or self._tail is None:
                self._tail = None

        # Keep the tail alive even after completion so later submitters chain
        # behind in-flight work; cleared lazily on the next submit.
        self._tail = runner_future
        runner_future.add_done_callback(_advance)
        return await result_future


def is_transient_db_error(error: BaseException) -> bool:
    import re

    message = str(getattr(error, "message", "")) or " ".join(
        str(arg) for arg in getattr(error, "args", []) if arg
    )
    return bool(
        re.search(r"disk\s*i/o|database is locked|database table is locked|busy|unable to open", message, re.I)
    )


class BrowserSlots:
    """Bounded concurrent page slots (port of withBrowserSlot)."""

    def __init__(self, max_slots: int = 1) -> None:
        self._max = max(1, max_slots)
        self._active = 0
        self._waiters: list[asyncio.Future] = []

    async def run(self, task: Callable[[], Awaitable[T]]) -> T:
        while self._active >= self._max:
            waiter: asyncio.Future = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await waiter
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
        self._active += 1
        try:
            return await task()
        finally:
            self._active -= 1
            if self._waiters:
                waiter = self._waiters.pop(0)
                if not waiter.done():
                    waiter.set_result(None)


async def gather_safe(*awaitables: Awaitable[Any]) -> list[Any]:
    """asyncio.gather that keeps per-task exceptions as values."""
    results: list[Any] = []
    for coro in awaitables:
        try:
            results.append(await coro)
        except Exception as error:  # noqa: BLE001 - caller inspects
            results.append(error)
    return results
