"""Time-of-day greeting variants for the creator home hero.

The server picks one greeting per time slot each day using a stable hash
of (user_id, local date). Client-side JS then picks which of the four
pre-rendered variants to display based on the viewer's browser hour, so
a creator in California doesn't see "good morning" at 8pm because the
server is on UTC.

Same user + same day = same picks. Next day rotates. Different users on
the same day get different picks so two creators sitting side by side
don't see identical copy.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import TypedDict

_MORNING = (
    "good morning, {name}.",
    "morning, {name}.",
    "welcome back, {name}.",
    "up early, {name}.",
    "let's get into it, {name}.",
)
_AFTERNOON = (
    "good afternoon, {name}.",
    "afternoon, {name}.",
    "back at it, {name}.",
    "good to see you, {name}.",
    "here's what's up, {name}.",
)
_EVENING = (
    "good evening, {name}.",
    "evening, {name}.",
    "before you sign off, {name}.",
    "one more pass, {name}.",
    "let's tie things up, {name}.",
)
_NIGHT = (
    "still here, {name}?",
    "late night, {name}.",
    "burning the midnight oil, {name}.",
    "one last check, {name}?",
)


class DailyGreetings(TypedDict):
    morning: str
    afternoon: str
    evening: str
    night: str


def pick_daily(user_id: str, name: str, today: date | None = None) -> DailyGreetings:
    """Return one greeting per slot, stable per (user, day)."""
    day = today or date.today()
    seed = f"{user_id}|{day.isoformat()}"
    digest = hashlib.sha256(seed.encode()).digest()
    safe = (name or "").strip().lower() or "creator"
    return {
        "morning": _MORNING[digest[0] % len(_MORNING)].format(name=safe),
        "afternoon": _AFTERNOON[digest[1] % len(_AFTERNOON)].format(name=safe),
        "evening": _EVENING[digest[2] % len(_EVENING)].format(name=safe),
        "night": _NIGHT[digest[3] % len(_NIGHT)].format(name=safe),
    }
