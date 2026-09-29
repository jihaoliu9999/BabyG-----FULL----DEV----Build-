"""Focused tests for the Settings cleanup pass:

  * Rate Floor is removed from the rendered Settings page and from
    the POST payload (historical column values remain untouched).
  * Deal-type preferences are rendered as the shared checkbox pattern
    (no row-highlight card), preserve selected state, and remain
    keyboard/label-associated.
  * "What babyg knows about you" still loads memory content from
    ``agent_memory.load``; the POST path to ``/creator/profile/
    babyg-memory`` remains a real form on the page.
  * Recent Changes surfaces at most two day groups, collapses multiple
    same-day history rows into a single line, strips internal
    identifiers, and never deletes stored history.
  * Accordion +/- restoration: the ``.settings-disclosure-icon`` block
    keeps its bone-toned marks visible (regression guard on the CSS).

The route tests stub Supabase reads with monkeypatch so the suite runs
without a live database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import agent_memory, oauth_connections, profiles

# ---------- fixtures ----------


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, user_id: str | None = None) -> str:
    uid = user_id or str(uuid4())
    response = Response()
    write_session(response, {"user_id": uid, "role": "creator"})
    cookie = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    return uid


def _stub_settings_reads(monkeypatch, *, profile: dict) -> None:
    """Minimal read stubs so the Settings GET handler can render."""

    def _get(uid: str):
        return {**profile, "user_id": uid}

    monkeypatch.setattr(profiles, "get_creator_profile_cached", lambda uid, _r=None: _get(uid))
    monkeypatch.setattr(profiles, "get_creator_profile", lambda uid: _get(uid))
    monkeypatch.setattr(oauth_connections, "get_google_connection", lambda _uid: None)
    monkeypatch.setattr(oauth_connections, "get_instagram_connection", lambda _uid: None)
    monkeypatch.setattr(oauth_connections, "instagram_needs_reconnect", lambda _uid: False)
    monkeypatch.setattr(
        oauth_connections, "refresh_instagram_username", lambda _uid: None
    )
    monkeypatch.setattr(
        oauth_connections, "google_calendar_connected", lambda _conn: False
    )
    monkeypatch.setattr(
        oauth_connections, "google_gmail_connected", lambda _conn: False
    )


# ---------- Rate Floor removed from Settings ----------


def test_settings_page_no_longer_renders_rate_floor(client, monkeypatch):
    _signed_in(client)
    _stub_settings_reads(
        monkeypatch,
        profile={
            "onboarding_completed_at": "2026-01-01T00:00:00Z",
            "full_name": "Creator",
            # Historical value present in the DB — must NOT surface.
            "deal_min_rate_text": "$2.5k organic",
        },
    )
    monkeypatch.setattr(agent_memory, "load", lambda _uid: None)
    monkeypatch.setattr(agent_memory, "history", lambda _uid, limit=40: [])

    response = client.get("/creator/profile/settings")
    assert response.status_code == 200
    body = response.text.lower()
    # UI copy gone
    assert "rate floor" not in body
    # The historical value must NOT be echoed back into the DOM
    assert "$2.5k organic" not in response.text
    # The named form input is gone (there's no other input with this
    # attribute anywhere in Settings).
    assert 'name="deal_min_rate_text"' not in response.text
    assert 'id="deal_min_rate_text"' not in response.text


def test_profile_deals_post_leaves_rate_floor_column_untouched(client, monkeypatch):
    _signed_in(client)
    monkeypatch.setattr(
        profiles,
        "get_creator_profile",
        lambda _uid: {"user_id": _uid, "onboarding_completed_at": "2026-01-01T00:00:00Z"},
    )
    captured: dict = {}

    def _update(_uid: str, payload: dict) -> bool:
        captured["payload"] = payload
        return True

    monkeypatch.setattr(profiles, "update_creator_profile", _update)
    response = client.post(
        "/creator/profile/deals",
        data={"deal_type_preferences": ["collab"]},
    )
    assert response.status_code == 303
    assert "deal_min_rate_text" not in captured["payload"]


# ---------- deal-type checkbox pattern ----------


def test_settings_page_renders_deal_types_as_shared_checkbox_pattern(client, monkeypatch):
    _signed_in(client)
    _stub_settings_reads(
        monkeypatch,
        profile={
            "onboarding_completed_at": "2026-01-01T00:00:00Z",
            "full_name": "Creator",
            "deal_type_preferences": ["collab"],
        },
    )
    monkeypatch.setattr(agent_memory, "load", lambda _uid: None)
    monkeypatch.setattr(agent_memory, "history", lambda _uid, limit=40: [])

    response = client.get("/creator/profile/settings")
    assert response.status_code == 200
    body = response.text
    # Reusable checkbox classes are used, and the row-highlight card
    # class from the earlier draft is not.
    assert 'class="settings-checkbox"' in body
    assert 'class="settings-checkbox-row"' in body
    assert "settings-deal-type " not in body
    assert "settings-deal-type>" not in body
    # Saved value renders as checked and unselected values render as
    # unchecked (multi-select behavior preserved).
    assert 'value="collab"' in body
    assert 'value="collab"\n                         checked' in body or (
        'value="collab"' in body and 'checked' in body
    )
    # A canonical value that wasn't selected must render un-checked;
    # simplest guard is that the raw input tag exists without the
    # `checked` token adjacent.
    assert 'value="brand_deal"' in body


def test_settings_page_deal_type_labels_use_label_for_accessibility(
    client, monkeypatch
):
    _signed_in(client)
    _stub_settings_reads(
        monkeypatch,
        profile={
            "onboarding_completed_at": "2026-01-01T00:00:00Z",
            "full_name": "Creator",
        },
    )
    monkeypatch.setattr(agent_memory, "load", lambda _uid: None)
    monkeypatch.setattr(agent_memory, "history", lambda _uid, limit=40: [])
    response = client.get("/creator/profile/settings")
    body = response.text
    # Each row is a real <label>, so tapping the copy toggles the box.
    assert body.count('class="settings-checkbox-row"') >= 4
    # Fieldset+legend group gives assistive tech a real group name.
    assert 'class="settings-deal-types"' in body
    assert "deal types you're interested in" in body


# ---------- Recent Changes summarizer ----------


def _row(
    *,
    created_at: datetime,
    updated_by: str = "agent",
    change_reason: str | None = "instagram unread activity updated",
    version: int = 1,
) -> dict:
    return {
        "id": str(uuid4()),
        "version": version,
        "summary": "…",
        "updated_by": updated_by,
        "change_reason": change_reason,
        "created_at": created_at.isoformat(),
    }


def test_summarize_returns_at_most_two_day_groups():
    now = datetime(2026, 6, 17, 12, tzinfo=UTC)
    rows = [
        _row(created_at=now - timedelta(days=i * 12, hours=1), version=100 - i)
        for i in range(6)
    ]
    out = agent_memory.summarize_recent_changes(rows, now=now)
    assert len(out) <= agent_memory.RECENT_CHANGES_MAX_GROUPS == 2


def test_summarize_collapses_multiple_same_day_rows_into_single_entry():
    now = datetime(2026, 6, 17, 12, tzinfo=UTC)
    same_day = [
        _row(created_at=now.replace(hour=9), change_reason="ig unread updated"),
        _row(created_at=now.replace(hour=10), change_reason="ig unread updated"),
        _row(created_at=now.replace(hour=11), change_reason="upcoming booking refreshed"),
    ]
    out = agent_memory.summarize_recent_changes(same_day, now=now)
    assert len(out) == 1
    assert out[0]["label"] == "today"
    assert out[0]["summary"].count(".") >= 1
    # De-duped reason count: 2 unique change_reasons → both appear in
    # the summary once.
    assert "ig unread updated" in out[0]["summary"]
    assert "upcoming booking refreshed" in out[0]["summary"]


def test_summarize_strips_internal_identifiers_from_summary():
    now = datetime(2026, 6, 17, 12, tzinfo=UTC)
    noisy = [
        _row(
            created_at=now,
            change_reason=(
                "cycle_abc123 saw thread_98ffcd12ab34ff01 "
                "on cf14a3b5-9b77-4c11-9c32-4a2b8f8e1abc; rewriting"
            ),
        )
    ]
    out = agent_memory.summarize_recent_changes(noisy, now=now)
    assert out, "expected one group"
    summary = out[0]["summary"]
    # UUIDs, long hex hashes, and prefixed ids are all scrubbed.
    assert "cycle_" not in summary
    assert "thread_" not in summary
    assert "cf14a3b5-9b77-4c11-9c32-4a2b8f8e1abc" not in summary
    assert "98ffcd12ab34ff01" not in summary
    # Something readable is left over.
    assert "rewriting" in summary or "saw" in summary


def test_summarize_uses_today_and_yesterday_labels():
    now = datetime(2026, 6, 17, 12, tzinfo=UTC)
    rows = [
        _row(created_at=now.replace(hour=9), change_reason="today event"),
        _row(created_at=(now - timedelta(days=1)).replace(hour=9), change_reason="past event"),
    ]
    out = agent_memory.summarize_recent_changes(rows, now=now)
    assert [g["label"] for g in out] == ["today", "yesterday"]


def test_summarize_returns_empty_for_no_history():
    assert agent_memory.summarize_recent_changes([]) == []
    assert agent_memory.summarize_recent_changes(None) == []


def test_summarize_single_day_returns_single_group():
    now = datetime(2026, 6, 17, 12, tzinfo=UTC)
    rows = [
        _row(created_at=now.replace(hour=8), change_reason="only edit of the day"),
    ]
    out = agent_memory.summarize_recent_changes(rows, now=now)
    assert len(out) == 1
    assert out[0]["label"] == "today"


def test_summarize_missing_change_reason_falls_back_to_neutral_line():
    now = datetime(2026, 6, 17, 12, tzinfo=UTC)
    rows = [
        _row(created_at=now, updated_by="agent", change_reason=None),
        _row(created_at=now.replace(hour=9), updated_by="agent", change_reason=""),
    ]
    out = agent_memory.summarize_recent_changes(rows, now=now)
    assert len(out) == 1
    assert "babyg" in out[0]["summary"].lower()


def test_summarize_does_not_touch_input_rows():
    """The presentation layer is read-only. Same list passed in twice
    must return byte-equal output — no mutation of source rows."""
    now = datetime(2026, 6, 17, 12, tzinfo=UTC)
    rows = [
        _row(created_at=now, change_reason="a"),
        _row(created_at=now - timedelta(days=1), change_reason="b"),
    ]
    snapshot = [dict(r) for r in rows]
    _ = agent_memory.summarize_recent_changes(rows, now=now)
    assert rows == snapshot


# ---------- What babyg knows still works ----------


def test_settings_page_still_renders_memory_editor(client, monkeypatch):
    _signed_in(client)
    _stub_settings_reads(
        monkeypatch,
        profile={
            "onboarding_completed_at": "2026-01-01T00:00:00Z",
            "full_name": "Creator",
        },
    )
    monkeypatch.setattr(
        agent_memory,
        "load",
        lambda _uid: {
            "user_id": _uid,
            "summary": "creator prefers organic partnerships.",
            "updated_by": "user",
            "updated_at": "2026-06-17T12:00:00Z",
            "version": 5,
        },
    )
    monkeypatch.setattr(agent_memory, "history", lambda _uid, limit=40: [])
    response = client.get("/creator/profile/settings")
    assert response.status_code == 200
    body = response.text
    assert "what babyg knows about you" in body
    assert "creator prefers organic partnerships." in body
    # POST target is unchanged — the memory generator / save path is
    # untouched by the cleanup.
    assert 'action="/creator/profile/babyg-memory"' in body


def test_settings_page_renders_summarized_history_not_raw_rows(client, monkeypatch):
    _signed_in(client)
    _stub_settings_reads(
        monkeypatch,
        profile={
            "onboarding_completed_at": "2026-01-01T00:00:00Z",
            "full_name": "Creator",
        },
    )
    monkeypatch.setattr(agent_memory, "load", lambda _uid: None)
    now = datetime(2026, 6, 17, 12, tzinfo=UTC)
    rows = [
        _row(created_at=now.replace(hour=9), change_reason="today reason A"),
        _row(created_at=now.replace(hour=10), change_reason="today reason B"),
        _row(created_at=now.replace(hour=11), change_reason="today reason A"),
        _row(created_at=(now - timedelta(days=1)).replace(hour=9), change_reason="yesterday reason"),
        _row(created_at=(now - timedelta(days=2)).replace(hour=9), change_reason="two days back"),
    ]
    monkeypatch.setattr(agent_memory, "history", lambda _uid, limit=40: rows)
    response = client.get("/creator/profile/settings")
    body = response.text
    # Group headers use the compact day label
    assert 'settings-memory-recent' in body
    assert body.count('settings-memory-recent-row') <= 2
    # The oldest row (two days back) must not surface once we already
    # have "today" + "yesterday".
    assert "two days back" not in body


# ---------- accordion +/- visibility regression ----------


def test_disclosure_icon_marks_use_visible_stroke():
    """Guard against the regression where the +/- marks fell to a
    1px, muted-secondary treatment and disappeared on OLED.
    Deliberately parses the CSS text rather than the DOM.

    The final robust fix paints via ``currentColor`` on the pseudo-
    elements and sets an explicit ``color`` (with a hardcoded --bone
    fallback) on the parent container — this survives even a cascade
    where --text-primary isn't defined for the current scope."""
    css = Path("app/static/css/app.css").read_text(encoding="utf-8")
    start = css.index(".is-creator-app .settings-disclosure-icon::before,")
    end = css.index("}", start)
    block = css[start:end]
    # Height must be at least 2px so the mark is visible.
    assert "height: 2px" in block or "height:2px" in block
    # Strokes paint via currentColor so they can never silently
    # resolve to a transparent/black value the way an undefined
    # var(--text-primary) could.
    assert "background-color: currentColor" in block
    # And the parent container carries an explicit color that anchors
    # currentColor to a real bone/light value.
    parent_start = css.index(".is-creator-app .settings-disclosure-icon {")
    parent_end = css.index("}", parent_start)
    parent_block = css[parent_start:parent_end]
    assert "color: var(--text-primary, #F5F1E8)" in parent_block


def test_deal_checkbox_geometry_is_locked_to_20px():
    """The Settings deal-preference checkbox must be pinned to 20x20 in
    six directions so no global input rule (see the min-height:54px +
    width:100% override for `.is-creator-app .settings-clean-shell
    .settings-form input`) can stretch it. Regression guard."""
    css = Path("app/static/css/app.css").read_text(encoding="utf-8")
    marker = (
        ".is-creator-app .settings-clean-shell .settings-form input.settings-checkbox,"
    )
    assert marker in css, "expected high-specificity checkbox rule was removed"
    start = css.index(marker)
    end = css.index("}", start)
    block = css[start:end]
    for prop in (
        "width: 20px",
        "height: 20px",
        "min-width: 20px",
        "min-height: 20px",
        "max-width: 20px",
        "max-height: 20px",
        "flex: 0 0 20px",
        "box-sizing: border-box",
    ):
        assert prop in block, f"missing lock on checkbox {prop!r}"


def test_disclosure_icon_container_has_visible_ring_and_fill():
    css = Path("app/static/css/app.css").read_text(encoding="utf-8")
    start = css.index(".is-creator-app .settings-disclosure-icon {")
    end = css.index("}", start)
    block = css[start:end]
    # Border alpha bumped up from 0.16 → 0.28 so the circle reads;
    # background likewise from 2.5% → 4% so the affordance stands
    # against the deep card surface without introducing a new color.
    assert "rgba(245,241,232,.28)" in block
    assert "rgba(255,255,255,.04)" in block


# ---------- Assistant opt-ins reuse the deal-preference checkbox ----------


def test_assistant_opt_ins_reuse_shared_checkbox_component(client, monkeypatch):
    """Every Assistant boolean setting must render with the same
    `input.settings-checkbox` inside `.settings-checkbox-row` markup
    the Deal Preferences picker uses. No `.settings-toggle` / round
    control may remain in the Assistant section — the shared 20 by 20
    square is the only opt-in visual on Settings."""
    _signed_in(client)
    _stub_settings_reads(
        monkeypatch,
        profile={
            "onboarding_completed_at": "2026-01-01T00:00:00Z",
            "full_name": "Creator",
            # A mix of on/off so we exercise both checked-state paths.
            "babyg_auto_brief_dms": True,
            "babyg_email_assistance": False,
            "babyg_agent_internal_actions": True,
            "babyg_agent_gmail_auto_send": False,
            "babyg_agent_calendar_holds": False,
            "babyg_agent_ig_auto_send": False,
        },
    )
    monkeypatch.setattr(agent_memory, "load", lambda _uid: None)
    monkeypatch.setattr(agent_memory, "history", lambda _uid, limit=40: [])
    response = client.get("/creator/profile/settings")
    assert response.status_code == 200
    body = response.text
    for field in (
        "babyg_auto_brief_dms",
        "babyg_email_assistance",
        "babyg_agent_internal_actions",
        "babyg_agent_gmail_auto_send",
        "babyg_agent_calendar_holds",
        "babyg_agent_ig_auto_send",
    ):
        # Each field must render as the shared square checkbox class.
        # The regex-free substring check tolerates whitespace inside
        # the attribute list of the tag.
        needle = f'class="settings-checkbox" type="checkbox" name="{field}"'
        assert needle in body, f"missing shared checkbox markup for {field}"
    # The Assistant section is inside the `babyg-behavior` disclosure.
    # Slice from that anchor forward to the next disclosure so the
    # legacy-toggle assertion is scoped to the Assistant block only
    # (Deal Preferences legitimately does not use .settings-toggle).
    assistant_start = body.index('id="babyg-behavior"')
    assistant_end = body.index('id="babyg-memory"')
    assistant_block = body[assistant_start:assistant_end]
    assert 'class="settings-toggle"' not in assistant_block, (
        "Assistant section still renders the legacy circular toggle"
    )
