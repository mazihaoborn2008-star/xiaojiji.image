from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException


class RateLimiter:
    def __init__(self):
        self.events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: int, *, limit_type: str = "rate_limit") -> None:
        now = time.monotonic()
        queue = self.events[key]
        window = max(1, int(window_seconds))
        while queue and now - queue[0] > window:
            queue.popleft()
        if len(queue) >= max(1, int(limit)):
            retry_after = max(1, int(window - (now - queue[0]))) if queue else window
            raise HTTPException(
                status_code=429,
                detail={
                    "code": limit_type,
                    "message": "请求过于频繁,请稍后重试。",
                    "retry_after": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )
        queue.append(now)


limiter = RateLimiter()
