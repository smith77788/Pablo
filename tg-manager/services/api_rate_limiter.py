"""Per-user API rate limiter using sliding window counter.

Usage:
    limiter = ApiRateLimiter(max_requests=60, window_seconds=60)
    if await limiter.check(user_id):
        # proceed with request
    else:
        # rate limit exceeded
"""

import time
import asyncio


class ApiRateLimiter:
    """Sliding window rate limiter for per-user request throttling.

    Tracks request timestamps per user and rejects when the count
    exceeds ``max_requests`` within the sliding ``window_seconds``.

    Args:
        max_requests: Maximum allowed requests per window per user.
        window_seconds: Sliding window duration in seconds.
    """

    def __init__(self, max_requests: int = 60, window_seconds: int = 60) -> None:
        self.max_requests: int = max_requests
        self.window_seconds: int = window_seconds
        self._requests: dict[int, list[float]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    async def check(self, user_id: int) -> bool:
        """Check if user is within rate limit. Returns True if allowed, False if exceeded.

        Args:
            user_id: Telegram user ID to check.

        Returns:
            True if the request is allowed, False if rate limit exceeded.
        """
        async with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            reqs = self._requests.setdefault(user_id, [])
            self._requests[user_id] = [t for t in reqs if t > cutoff]
            if len(self._requests[user_id]) >= self.max_requests:
                return False
            self._requests[user_id].append(now)
            return True
