"""babyg Brief page — ``/creator/brief``.

Dedicated production route the Home Brief "view all" link opens.
Aggregates real pending action_proposals + manager-worthy
notifications from persisted state (see ``app/services/brief.py``).
No provider fanout, no LLM calls, no schema change.

Everything visible on the page is derived from rows the rest of
babyg's manager UI already reads. This route only registers a
GET handler; per-item actions (send reply, ask babyg) reuse
existing endpoints:

* Gmail send      -> ``POST /creator/bot/actions/{message_id}/confirm``
                     (existing bot confirm path, unchanged)
* ask babyg       -> ``GET  /creator/bot?brief=<key>``
                     (existing bot chat with a small context param
                      the manager route now recognizes)
* Instagram      -> ``GET  <notification.link_path>`` when the
                     ingestion pipeline set a review destination.
                     Never a send.

The route is behind ``require_role("creator")`` and covered by
the request-scoped tabbar priming registered in ``app/main.py``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.core.security import SessionPayload
from app.core.templating import templates
from app.deps import require_role
from app.services import brief as brief_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["creator", "brief"])


@router.get("/creator/brief", response_class=HTMLResponse)
async def brief_page(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Render the dedicated Brief page for a signed-in creator."""
    user_id = session["user_id"]
    view = brief_service.build_brief(user_id)
    return templates.TemplateResponse(
        request,
        "creator/brief.html",
        {
            "brief": view,
        },
    )
