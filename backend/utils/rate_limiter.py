import asyncio
import time
from typing import Dict
from collections import deque

class RateLimiter:
    
    def __init__(self, calls_per_second: float):
        self.calls_per_second = calls_per_second
        self.min_interval = 1.0 / calls_per_second
        self.last_call = 0.0
        self._lock = asyncio.Lock()
    
    async def acquire(self):
        async with self._lock:
            current_time = time.time()
            time_since_last_call = current_time - self.last_call
            
            if time_since_last_call < self.min_interval:
                wait_time = self.min_interval - time_since_last_call
                await asyncio.sleep(wait_time)
            
            self.last_call = time.time()


class SlidingWindowRateLimiter:
 
    def __init__(self, max_calls: int, window_seconds: float):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.calls: deque = deque()
        self._lock = asyncio.Lock()
    
    async def acquire(self):
        """Acquire permission to make an API call."""
        async with self._lock:
            current_time = time.time()

            while self.calls and self.calls[0] < current_time - self.window_seconds:
                self.calls.popleft()

            if len(self.calls) >= self.max_calls:
                oldest_call = self.calls[0]
                wait_time = oldest_call + self.window_seconds - current_time
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                    current_time = time.time()
                    while self.calls and self.calls[0] < current_time - self.window_seconds:
                        self.calls.popleft()
            
            self.calls.append(current_time)


_rate_limiters: Dict[str, RateLimiter] = {}
_quota_limiters: Dict[str, SlidingWindowRateLimiter] = {}

def get_rate_limiter(api_name: str, calls_per_second: float = 10.0) -> RateLimiter:

    if api_name not in _rate_limiters:
        _rate_limiters[api_name] = RateLimiter(calls_per_second)
    return _rate_limiters[api_name]


def get_quota_limiter(api_name: str, max_calls: int, window_seconds: float) -> SlidingWindowRateLimiter:
    """Enforce a real call quota over a window.

    Prefer this over ``get_rate_limiter`` for published API quotas. Passing a
    daily quota to the interval-based ``RateLimiter`` converts it into a minimum
    gap between consecutive calls (500/day became a 172-second sleep between
    every call), which starved sources until they hit the orchestration timeout
    and were dropped. A sliding window lets calls proceed at full speed until
    the quota is genuinely exhausted.
    """
    if api_name not in _quota_limiters:
        _quota_limiters[api_name] = SlidingWindowRateLimiter(max_calls, window_seconds)
    return _quota_limiters[api_name]


def reset_limiters() -> None:
    """Clear cached limiters (test isolation)."""
    _rate_limiters.clear()
    _quota_limiters.clear()
