"""Creator deal destination tests."""

from __future__ import annotations

from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.routes import creator as creator_routes


def _signed_in(client: TestClient, *, user_id: str = "creator-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def test_deals_list_renders_terms_first_destination(monkeypatch) -> None:
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile_cached",
        lambda uid, request: {"onboarding_completed_at": "2026-09-01T00:00:00Z"},
    )
    monkeypatch.setattr(
        creator_routes.babyg_deals,
        "list_deals",
        lambda uid, *, active_only=True, limit=40: [
            {
                "id": "deal-1",
                "brand_name": "Acme",
                "stage": "negotiating",
                "agreed_amount_cents": 400000,
                "paid_amount_cents": None,
                "platform": "instagram",
                "last_touch_at": "2026-09-08T10:00:00Z",
            }
        ],
    )

    response = client.get("/creator/deals")

    assert response.status_code == 200
    assert "Acme" in response.text
    assert "$4,000" in response.text
    assert "Keep the counter tied to scope" in response.text
    assert "/creator/deals/deal-1" in response.text


def test_deal_detail_404s_when_not_owned(monkeypatch) -> None:
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile_cached",
        lambda uid, request: {"onboarding_completed_at": "2026-09-01T00:00:00Z"},
    )
    monkeypatch.setattr(
        creator_routes.babyg_deals,
        "get_deal",
        lambda deal_id, *, creator_id: None,
    )

    response = client.get("/creator/deals/not-mine")

    assert response.status_code == 404
