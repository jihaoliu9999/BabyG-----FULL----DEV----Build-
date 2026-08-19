"""Locks in the jsonb-string safety for bot_messages.tool_calls.

Supabase-py returns ``jsonb`` as either a dict or (in some code paths)
the raw JSON string. bot.list_messages normalises this to a dict before
the template ever sees it, so the render layer can safely use attribute
access. This test file exists because a template rewrite that assumed
"tool_calls is always a dict" took the bot page down in production —
a regression we do not want repeated.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.core.templating import templates
from app.services import bot as bot_module


class _FakeQuery:
    """Chainable Supabase-py-shaped stub — every builder call returns self,
    execute() returns the canned rows."""

    def __init__(self, rows):
        self._rows = rows

    def __getattr__(self, _name):
        return lambda *a, **kw: self

    def execute(self):
        result = MagicMock()
        result.data = self._rows
        return result


def _run_list_messages_with(rows):
    fake_client = MagicMock()
    fake_client.table.return_value = _FakeQuery(rows)
    with patch.object(bot_module.supabase_client, "get_service_client", return_value=fake_client):
        return bot_module.list_messages("u-1")


# ---------------------------------------------------------------------------
# Normalisation at the service layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        ({"kind": "nudge", "chips": [{"label": "go"}]},
         {"kind": "nudge", "chips": [{"label": "go"}]}),
        (json.dumps({"kind": "proposed_action", "status": "pending"}),
         {"kind": "proposed_action", "status": "pending"}),
        ("{not json", None),
        ('"just-a-string"', None),  # parses to a str, not a dict
        ("[1,2,3]", None),          # parses to list, not dict
        (42, None),
    ],
)
def test_list_messages_normalises_tool_calls(raw, expected):
    rows = [
        {
            "id": "m-1",
            "role": "assistant",
            "content": "hello",
            "tool_calls": raw,
        }
    ]
    out = _run_list_messages_with(rows)
    assert out[0]["tool_calls"] == expected


# ---------------------------------------------------------------------------
# End-to-end: even without coercion, the template must not throw
# ---------------------------------------------------------------------------


def _render_partial(rows):
    request = MagicMock()
    request.cookies = {}
    request.headers = {}
    original = templates.env.globals.get("csrf_token")
    templates.env.globals["csrf_token"] = lambda req: "test-token"
    try:
        return templates.get_template("_partials/bot_messages.html").render(
            {"messages": rows, "error": None, "request": request}
        )
    finally:
        if original is not None:
            templates.env.globals["csrf_token"] = original


def test_template_renders_nudge_message_with_chips():
    """A nudge row with chips renders both the nudge tag and every chip."""
    rows = [
        {
            "id": "m-nudge",
            "role": "assistant",
            "content": "chobani posted a brief — pitch?",
            "tool_calls": {
                "kind": "nudge",
                "nudge_key": "new_match:opportunity:c-1",
                "nudge_category": "new_match",
                "chips": [
                    {"kind": "fill", "label": "pitch it", "text": "draft pitch", "primary": True},
                    {"kind": "nav", "label": "see it", "href": "/creator/discover"},
                    {"kind": "fill", "label": "skip", "text": "skip"},
                ],
            },
        }
    ]
    html = _render_partial(rows)
    assert "bot-message-nudge" in html
    assert "babyg · new match" in html
    assert "pitch it" in html
    assert "see it" in html
    assert 'data-chip-fill="draft pitch"' in html
    assert 'data-chip-submit="1"' in html  # primary auto-submits
    assert 'href="/creator/discover"' in html


def test_template_survives_string_tool_calls_row():
    """If a bot_message somehow makes it into the render with tool_calls
    still a string (pre-coercion code path, or malformed row), the
    template must fall back to a plain bubble — never raise. Attribute
    access on strings yields Jinja Undefined; .get() would throw."""
    rows = [
        {
            "id": "m-legacy",
            "role": "assistant",
            "content": "hello",
            "tool_calls": '{"kind": "proposed_action", "status": "pending"}',
        }
    ]
    html = _render_partial(rows)
    # Plain bubble renders; no action card, no chip row (since tool_calls
    # is a string, .kind returns Undefined, and the {% if %} guards drop
    # every conditional branch).
    assert "hello" in html
    assert "bot-action-pending" not in html
    assert "bot-chip-row" not in html


def test_template_renders_legacy_proposed_action():
    """The existing action-card path still fires for dict-typed
    proposed_action rows — regression guard for the old flow."""
    rows = [
        {
            "id": "m-legacy",
            "role": "assistant",
            "content": "here's the draft",
            "tool_calls": {
                "kind": "proposed_action",
                "status": "pending",
                "action_type": "gmail.create_draft",
                "payload": {"to": "x@y.z", "subject": "hi", "body": "hello"},
            },
        }
    ]
    html = _render_partial(rows)
    assert "bot-action-pending" in html
    assert "x@y.z" in html
    # Confirm/cancel chips render as forms.
    assert 'action="/creator/bot/actions/m-legacy/confirm"' in html
    assert 'action="/creator/bot/actions/m-legacy/cancel"' in html
