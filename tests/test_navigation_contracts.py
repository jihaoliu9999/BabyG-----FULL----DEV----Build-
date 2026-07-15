"""Static navigation contracts for every non-landing Jinja surface."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from app.main import app

TEMPLATES = Path(__file__).parents[1] / "app" / "templates"
STATIC_TARGET = re.compile(r'''(?:href|action)\s*=\s*["']([^"']+)["']''', re.I)


def test_non_landing_static_navigation_targets_registered_routes() -> None:
    route_patterns = [
        route.path_regex
        for route in app.routes
        if getattr(route, "path_regex", None) is not None
    ]
    missing: list[str] = []

    for template in TEMPLATES.rglob("*.html"):
        if template.as_posix().endswith("marketing/landing.html"):
            continue
        source = template.read_text(encoding="utf-8")
        for match in STATIC_TARGET.finditer(source):
            target = match.group(1)
            if not target.startswith("/") or "{{" in target or "{%" in target:
                continue
            path = urlsplit(target).path
            if path.startswith("/static/"):
                continue
            if not any(pattern.match(path) for pattern in route_patterns):
                line = source.count("\n", 0, match.start()) + 1
                missing.append(f"{template.relative_to(TEMPLATES)}:{line} -> {target}")

    assert not missing, "Unregistered navigation targets:\n" + "\n".join(missing)
