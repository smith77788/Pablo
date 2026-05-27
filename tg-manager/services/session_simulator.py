"""Session Realism utilities — human-like delays and timing variation."""
from __future__ import annotations

import asyncio
import random


async def human_delay(min_s: float = 1.5, max_s: float = 8.0) -> None:
    """Pause with a human-like distribution (beta-skewed toward lower values)."""
    delay = random.betavariate(2, 5) * (max_s - min_s) + min_s
    await asyncio.sleep(delay)


async def short_pause(min_s: float = 0.3, max_s: float = 1.5) -> None:
    """Quick pause between lightweight actions."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def typing_delay(text: str) -> None:
    """Simulate reading/composing: ~50-100 ms per character, capped at 8s."""
    delay = min(8.0, len(text) * random.uniform(0.05, 0.1))
    await asyncio.sleep(delay)


def chaos_factor(base: float = 1.0, spread: float = 0.3) -> float:
    """Return a multiplier in [base-spread, base+spread] for timing variation."""
    return base + random.uniform(-spread, spread)


async def bulk_item_pause(index: int, batch_size: int = 10) -> None:
    """Pause between bulk items; slightly longer every batch_size items."""
    if index > 0 and index % batch_size == 0:
        await asyncio.sleep(random.uniform(3.0, 8.0))
    else:
        await short_pause(0.5, 2.0)
