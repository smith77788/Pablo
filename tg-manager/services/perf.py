"""Performance measurement utilities for before/after comparison.
СТАТУС: НЕ ПОДКЛЮЧЁН (проверено 2026-07-27). Утилита замеров «до/после» для
разовых сравнений. Держится как инструмент разработки, в проде не участвует.
"""

from __future__ import annotations

import time
import logging
import asyncio
from contextlib import asynccontextmanager
from typing import Any

log = logging.getLogger(__name__)


class PerfTimer:
    """Async context manager for measuring operation duration."""

    def __init__(self, label: str):
        self.label = label
        self._start: float = 0
        self._end: float = 0
        self.duration_ms: float = 0

    async def __aenter__(self):
        self._start = time.perf_counter()
        return self

    async def __aexit__(self, *exc):
        self._end = time.perf_counter()
        self.duration_ms = (self._end - self._start) * 1000
        log.debug("perf[%s]: %.2fms", self.label, self.duration_ms)


def perf_timer(label: str):
    return PerfTimer(label)


@asynccontextmanager
async def measure(label: str):
    """Measure an async block and return duration."""
    t = PerfTimer(label)
    async with t:
        yield t


async def benchmark(label: str, fn, *args, iterations: int = 5, **kwargs) -> dict:
    """Benchmark an async function over N iterations."""
    durations = []
    result = None
    for _ in range(iterations):
        start = time.perf_counter()
        result = await fn(*args, **kwargs)
        durations.append((time.perf_counter() - start) * 1000)

    avg = sum(durations) / len(durations)
    mn = min(durations)
    mx = max(durations)
    log.info(
        "benchmark[%s]: avg=%.2fms min=%.2fms max=%.2fms (n=%d)",
        label, avg, mn, mx, iterations,
    )
    return {
        "label": label,
        "iterations": iterations,
        "avg_ms": round(avg, 2),
        "min_ms": round(mn, 2),
        "max_ms": round(mx, 2),
        "result": result,
    }


async def batch_coro(coro_iter, concurrency: int = 50):
    """Run coroutines in bounded concurrency batches.

    Args:
        coro_iter: iterable of awaitables
        concurrency: max simultaneous tasks

    Returns:
        list of results in order
    """
    sem = asyncio.Semaphore(concurrency)
    results = [None] * len(coro_iter)
    coros = list(coro_iter)

    async def _limited(idx, c):
        async with sem:
            results[idx] = await c

    await asyncio.gather(*[_limited(i, c) for i, c in enumerate(coros)])
    return results
