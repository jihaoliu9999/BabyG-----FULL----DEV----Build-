"""Single Jinja2Templates instance shared across all routers."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Lazy-import to avoid a circular: csrf.py imports from app.config which is
# safe, but app.core.security imports app.config too and we don't want any
# template-time gotchas. Importing here keeps the module graph linear.
from app.core.csrf import init_csrf_for_templates  # noqa: E402

init_csrf_for_templates(templates)
