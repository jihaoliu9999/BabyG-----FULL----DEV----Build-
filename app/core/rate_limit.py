"""Tiny in-process token-bucket rate limiter.

Sized for Phase 1's single-server deployment. When we go multi-process or
multi-host, swap this for Redis-backed limits (slowapi/fastapi-limiter).
The signature here is deliberately compatible with that future swap so
call sites don't change.

Buckets are keyed on `(scope, key)`. `key` should be a stable identifier
for the requester — typically the client IP for unauthenticated endpoints
and the user_id for authenticated ones.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict


class _TokenBucket:
    __slots__ = ("last", "tokens")

    def __init__(self, tokens: float, last: float) -> None:
        self.tokens = tokens
        self.last = last


class RateLimiter:
    def __init__(self, *, capacity: int, refill_per_second: float, max_keys: int = 4096) -> None:
        self.capacity = float(capacity)
        self.refill = float(refill_per_second)
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._buckets: OrderedDict[tuple[str, str], _TokenBucket] = OrderedDict()

    def allow(self, scope: str, key: str) -> bool:
        if not key:
            return True
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get((scope, key))
            if bucket is None:
                bucket = _TokenBucket(tokens=self.capacity, last=now)
                self._buckets[(scope, key)] = bucket
                if len(self._buckets) > self.max_keys:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end((scope, key))
                bucket.tokens = min(
                    self.capacity,
                    bucket.tokens + (now - bucket.last) * self.refill,
                )
                bucket.last = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False


# 5 sends per IP per 10 minutes (refill ~1 every 2 min).
magic_link_limiter = RateLimiter(capacity=5, refill_per_second=1 / 120.0)


def client_ip(request) -> str:
    # Trust X-Forwarded-For only one hop (Railway/Cloudflare set it).
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""
