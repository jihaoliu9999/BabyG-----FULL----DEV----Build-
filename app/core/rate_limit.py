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

# AI brief budgets are intentionally separate from authentication traffic.
# Manual actions allow a small burst, while automatic generation is tighter
# and keyed by recipient + thread at the call site.
dm_brief_manual_limiter = RateLimiter(capacity=5, refill_per_second=1 / 120.0)
dm_brief_auto_limiter = RateLimiter(capacity=3, refill_per_second=1 / 600.0)


def client_ip(request) -> str:
    """Best-effort client IP for rate-limit keying.

    Trust model: assume exactly one reverse proxy (Railway/Cloudflare)
    appends the real client to `X-Forwarded-For`. Read the LAST value,
    not the first — the first is attacker-controlled (a client can send
    `X-Forwarded-For: 1.2.3.4` themselves and the proxy will append, not
    replace). Without the proxy hop, fall back to `request.client.host`.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        last = xff.rsplit(",", 1)[-1].strip()
        if last:
            return last
    return request.client.host if request.client else ""
