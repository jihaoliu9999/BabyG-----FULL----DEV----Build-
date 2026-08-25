"""Timing helper for outbound calls to external services.

The audit flagged synchronous Anthropic, Google Calendar, Gmail,
Instagram, Tavily, and BigDataCloud calls inside request paths — but
we can't tell how much wall-time each contributes without measurement.

Rather than instrument every call site at once, this module ships a
tiny context-manager helper and the Anthropic integration adopts it
first as a proof of pattern. Other integrations can follow one at a
time in later patches.

Privacy: only the service name, outcome (ok / err), and duration are
logged. Never the prompt, response body, tokens, or headers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger("babyg.external_timing")


@contextmanager
def time_external(service: str) -> Iterator[None]:
    """Wall-time an outbound call and log one INFO line.

    Example::

        with time_external("anthropic"):
            response = client.messages.create(...)

    Produces (on success or failure):

        svc=anthropic outcome=ok duration_ms=812.4
        svc=anthropic outcome=err duration_ms=15003.2
    """
    start = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "err"
        raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000.0
        logger.info(
            "svc=%s outcome=%s duration_ms=%.1f",
            service,
            outcome,
            duration_ms,
        )
