"""Session Realism utilities — human-like delays and behavioral timing variation."""

from __future__ import annotations

import asyncio
import datetime
import math
import random
from typing import Optional


# ── Core primitives ──────────────────────────────────────────────────────────


async def human_delay(min_s: float = 1.5, max_s: float = 8.0) -> None:
    """Pause with a human-like distribution (beta-skewed toward lower values)."""
    delay = random.betavariate(2, 5) * (max_s - min_s) + min_s
    await asyncio.sleep(delay)


async def short_pause(min_s: float = 0.3, max_s: float = 1.5) -> None:
    """Quick pause between lightweight actions."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def micro_jitter() -> None:
    """Sub-second jitter between rapid successive actions."""
    await asyncio.sleep(random.betavariate(1.5, 4) * 0.4 + 0.05)


async def typing_delay(text: str) -> None:
    """Simulate reading/composing: ~50-100 ms per character, capped at 8s."""
    delay = min(8.0, len(text) * random.uniform(0.05, 0.1))
    await asyncio.sleep(delay)


async def bulk_item_pause(index: int, batch_size: int = 10) -> None:
    """Pause between bulk items; slightly longer every batch_size items."""
    if index > 0 and index % batch_size == 0:
        await asyncio.sleep(random.uniform(3.0, 8.0))
    else:
        await short_pause(0.5, 2.0)


# ── Timing multipliers ───────────────────────────────────────────────────────


def chaos_factor(base: float = 1.0, spread: float = 0.3) -> float:
    """Return a multiplier in [base-spread, base+spread] for timing variation."""
    return base + random.uniform(-spread, spread)


def time_of_day_factor(hour: Optional[int] = None) -> float:
    """
    Returns a timing multiplier based on the current hour of day.
    Humans are slower at night and during early morning.
    1.0 = normal speed, >1.0 = slower/longer pauses, <1.0 = faster.
    """
    if hour is None:
        hour = datetime.datetime.now().hour

    # Deep night (2-6 AM): very slow activity, long pauses
    if 2 <= hour <= 6:
        return random.uniform(2.5, 5.0)
    # Late night (23-1 AM): slower
    if hour >= 23 or hour <= 1:
        return random.uniform(1.6, 2.8)
    # Early morning (7-9 AM): waking up, slightly slow
    if 7 <= hour <= 9:
        return random.uniform(1.1, 1.6)
    # Lunch break (12-14): distracted, slightly slow
    if 12 <= hour <= 14:
        return random.uniform(0.9, 1.3)
    # Evening (20-22): winding down
    if 20 <= hour <= 22:
        return random.uniform(1.2, 1.8)
    # Peak activity (10-19): normal to fast
    return random.uniform(0.75, 1.15)


def session_fatigue(duration_minutes: float) -> float:
    """
    Returns a slowdown multiplier based on how long the session has been running.
    Fresh session = 1.0, very long session = up to ~3.0x slower.
    Based on exponential fatigue curve.
    """
    if duration_minutes <= 0:
        return 1.0
    # 15min: 1.05x, 30min: 1.12x, 60min: 1.28x, 2h: 1.6x, 4h: 2.2x
    fatigue = 1.0 + 0.28 * math.log1p(duration_minutes / 15)
    return min(3.0, fatigue)


# ── Behavioral patterns ───────────────────────────────────────────────────────


async def distraction_pause(probability: float = 0.06) -> bool:
    """
    Occasionally simulate the user getting distracted (tab switch, phone call, etc.).
    Returns True if a distraction pause occurred.
    Duration: 30 seconds to 8 minutes.
    """
    if random.random() < probability:
        # Short distraction (30-120s) is common; long (2-8min) is rare
        if random.random() < 0.7:
            duration = random.uniform(30, 120)
        else:
            duration = random.uniform(120, 480)
        await asyncio.sleep(duration)
        return True
    return False


async def reading_pause(content_length: int) -> None:
    """
    Simulate a human reading content before acting on it.
    ~200-300 words/min reading speed. content_length in characters.
    """
    words = max(1, content_length / 5)
    wpm = random.uniform(180, 320)
    read_time = (words / wpm) * 60
    # Add thinking time after reading
    think_time = random.betavariate(2, 4) * 4.0 + 0.5
    await asyncio.sleep(min(20.0, read_time + think_time))


async def action_hesitation(probability: float = 0.12) -> None:
    """
    Simulate brief hesitation before committing an action (2-8 seconds).
    Humans pause before clicking 'confirm', typing a command, etc.
    """
    if random.random() < probability:
        await asyncio.sleep(random.uniform(2.0, 8.0))


def activity_burst_factor(probability: float = 0.08) -> float:
    """
    Sometimes humans rush through tasks quickly (burst mode).
    Returns a speed multiplier: <1.0 = faster, >1.0 = slower.
    """
    r = random.random()
    if r < probability:
        # Rushing: 40-70% of normal speed (faster)
        return random.uniform(0.4, 0.7)
    if r < probability * 2.5:
        # Distracted/slow: 150-300% of normal speed (slower)
        return random.uniform(1.5, 3.0)
    return 1.0


async def between_accounts_pause(account_index: int) -> None:
    """
    Pause when switching between accounts during bulk operations.
    First switch is quick, subsequent ones get longer (account hopping pattern).
    """
    if account_index == 0:
        await asyncio.sleep(random.uniform(2.0, 6.0))
    elif account_index < 3:
        await asyncio.sleep(random.uniform(8.0, 20.0))
    else:
        # After 3+ accounts, take a longer break
        await asyncio.sleep(random.uniform(30.0, 90.0))


async def smart_batch_delay(
    item_index: int,
    total_items: int,
    base_min: float = 3.0,
    base_max: float = 12.0,
    fatigue_start_minutes: float = 0,
) -> None:
    """
    Intelligent delay between batch items combining multiple human factors:
    - Time of day
    - Session fatigue
    - Occasional distraction
    - Activity bursts
    - Longer cooldowns every N items
    """
    tod = time_of_day_factor()
    fatigue = session_fatigue(fatigue_start_minutes + item_index * (base_max / 60))
    burst = activity_burst_factor()

    base_delay = random.betavariate(2, 4) * (base_max - base_min) + base_min
    final_delay = base_delay * tod * fatigue * burst

    # Every 7-12 items: take a longer natural break
    cooldown_every = random.randint(7, 12)
    if item_index > 0 and item_index % cooldown_every == 0:
        final_delay += random.uniform(15.0, 60.0)

    # End-of-session: slower near the finish
    if total_items > 5 and item_index >= total_items - 2:
        final_delay *= random.uniform(1.2, 1.8)

    await asyncio.sleep(max(0.5, final_delay))
    await distraction_pause(probability=0.04)


# ── Realistic typing ─────────────────────────────────────────────────────────


async def realistic_type(text: str, session_minutes: float = 0) -> None:
    """
    Simulate realistic typing time for a text string.
    Takes into account: WPM variation, session fatigue, typo-correction pauses.
    """
    words = max(1, len(text.split()))
    # Variable WPM: 25-75 words/min (skilled typist vs mobile user)
    wpm = random.uniform(25, 75) / session_fatigue(session_minutes)
    base_time = (words / wpm) * 60

    # Occasional typo-correction (adds 1-3s)
    typo_extra = 0.0
    if len(text) > 20 and random.random() < 0.3:
        typo_extra = random.uniform(1.0, 3.5)

    total = min(20.0, base_time + typo_extra)
    await asyncio.sleep(total)


# ── Daily rhythm helpers ─────────────────────────────────────────────────────


def is_active_hours(hour: Optional[int] = None) -> bool:
    """Returns True if current hour is within typical human active hours (8 AM - 11 PM)."""
    if hour is None:
        hour = datetime.datetime.now().hour
    return 8 <= hour <= 23


def seconds_until_active_hours() -> float:
    """Returns seconds until 8 AM if currently in night hours."""
    now = datetime.datetime.now()
    if is_active_hours(now.hour):
        return 0.0
    target = now.replace(hour=8, minute=random.randint(0, 45), second=0)
    if now.hour >= 23:
        target += datetime.timedelta(days=1)
    diff = (target - now).total_seconds()
    return max(0.0, diff)


async def respect_daily_rhythm(hard_pause_at_night: bool = False) -> None:
    """
    If it's deep night (2-6 AM) and hard_pause_at_night is True,
    sleep until morning. Otherwise just apply a time_of_day multiplier.
    """
    hour = datetime.datetime.now().hour
    if hard_pause_at_night and 2 <= hour <= 5:
        sleep_secs = seconds_until_active_hours()
        if sleep_secs > 0:
            await asyncio.sleep(sleep_secs)
    elif not is_active_hours(hour):
        # Just slow down significantly without full stop
        await asyncio.sleep(random.uniform(60, 300))
