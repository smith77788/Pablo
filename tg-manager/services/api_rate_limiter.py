import time
import asyncio


class ApiRateLimiter:
    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[int, list[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, user_id: int) -> bool:
        async with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            reqs = self._requests.setdefault(user_id, [])
            self._requests[user_id] = [t for t in reqs if t > cutoff]
            if len(self._requests[user_id]) >= self.max_requests:
                return False
            self._requests[user_id].append(now)
            return True
