"""Operator brand review pages."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import abuse as abuse_module
from app.services import audit as audit_module
from app.services import brand_trust as brand_trust_module
from app.services import operator_brands as operator_brands_module
from app.services import profiles as profiles_module


class BrandWorld:
    def __init__(self) -> None:
        self.brands: dict[str, dict[str, Any]] = {}
        self.campaigns: list[dict[str, Any]] = []
        self.report_counts: dict[str, int] = {}
        self.audit_rows: list[dict[str, Any]] = []
        self.last_verification: dict[str, Any] | None = None
        self.last_audit: dict[str, Any] | None = None
        self.trust_checks: list[dict[str, Any]] = []
        self.last_domain_check: dict[str, Any] | None = None
        self.report: dict[str, Any] | None = None


@pytest.fixture()
def world(monkeypatch) -> BrandWorld:
    w = BrandWorld()

    def _list_brands(*, query=None, verification="all", limit=200):
        rows = list(w.brands.values())
        if verification == "verified":
            rows = [r for r in rows if r.get("is_verified")]
        elif verification == "needs_review":
            rows = [r for r in rows if not r.get("is_verified")]
        if query:
            needle = str(query).lower()
            rows = [
                r
                for r in rows
                if needle
                in " ".join(
                    str(r.get(k) or "").lower()
                    for k in ("company_name", "brand_website", "industry")
                )
            ]
        return rows[:limit]

    def _get_brand(user_id):
        return w.brands.get(user_id)

    def _update_verification(*, user_id, action, note, operator_id=None):
        if user_id not in w.brands:
            return False
        w.last_verification = {
            "user_id": user_id,
            "action": action,
            "note": note,
            "operator_id": operator_id,
        }
        w.brands[user_id]["is_verified"] = action == "verified"
        w.brands[user_id]["verification_status"] = action
        w.brands[user_id]["review_status"] = action
        w.brands[user_id]["verification_notes"] = note
        return True

    monkeypatch.setattr(operator_brands_module, "list_brands", _list_brands)
    monkeypatch.setattr(operator_brands_module, "get_brand", _get_brand)
    monkeypatch.setattr(
        operator_brands_module, "update_verification", _update_verification
    )
    monkeypatch.setattr(
        operator_brands_module, "list_brand_campaigns", lambda *, limit=200: w.campaigns[:limit]
    )
    monkeypatch.setattr(
        operator_brands_module,
        "report_counts_by_brand",
        lambda ids: {brand_id: w.report_counts.get(brand_id, 0) for brand_id in ids},
    )
    monkeypatch.setattr(
        operator_brands_module,
        "brand_counts",
        lambda: {
            "total_brands": len(w.brands),
            "verified_brands": sum(1 for b in w.brands.values() if b.get("is_verified")),
            "pending_brand_reviews": sum(
                1 for b in w.brands.values() if not b.get("is_verified")
            ),
            "blocked_or_flagged_brands": 0,
            "active_brand_campaigns": len(
                [
                    c
                    for c in w.campaigns
                    if c.get("is_active") and not c.get("is_taken_down")
                ]
            ),
        },
    )
    monkeypatch.setattr(audit_module, "list_recent", lambda *, limit=200: w.audit_rows)
    monkeypatch.setattr(brand_trust_module, "list_checks", lambda uid, *, limit=30: w.trust_checks)

    def _run_domain_check(*, brand, operator_user_id):
        w.last_domain_check = {
            "brand": brand.get("user_id"),
            "operator_user_id": operator_user_id,
        }
        return {
            "result_status": "pass",
            "summary": "website and business email domain match.",
            "website_domain": "studio.example",
            "contact_email_domain": "studio.example",
        }

    monkeypatch.setattr(brand_trust_module, "run_domain_check", _run_domain_check)

    def _record(*, actor_user_id, action, target_type=None, target_id=None, notes=None):
        w.last_audit = {
            "actor_user_id": actor_user_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "notes": notes,
        }
        return True

    monkeypatch.setattr(audit_module, "record", _record)
    monkeypatch.setattr(abuse_module, "get", lambda report_id: w.report)
    monkeypatch.setattr(profiles_module, "get_creator_profile", lambda uid: None)
    return w


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, *, role: str, user_id: str = "op-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def _brand(**kwargs) -> dict[str, Any]:
    row = {
        "user_id": kwargs.get("user_id", "brand-1"),
        "company_name": kwargs.get("company_name", "studio north"),
        "brand_website": kwargs.get("brand_website", "https://studio.example"),
        "industry": kwargs.get("industry", "beauty"),
        "contact_full_name": kwargs.get("contact_full_name", "Riley"),
        "contact_title": kwargs.get("contact_title", "founder"),
        "product_description": "skincare",
        "scale_descriptor": "emerging",
        "model_descriptor": "dtc",
        "positioning_descriptor": "quiet luxury",
        "campaign_types": ["paid posts"],
        "creator_size_preferences": ["mid"],
        "niche_preferences": ["beauty"],
        "budget_range": "$1k-$5k",
        "is_verified": kwargs.get("is_verified", False),
        "verification_status": kwargs.get(
            "verification_status",
            "verified" if kwargs.get("is_verified", False) else "needs_review",
        ),
        "review_status": "verified" if kwargs.get("is_verified", False) else "needs_review",
        "verification_notes": kwargs.get("verification_notes", "operator-only note"),
        "created_at": "2026-06-01T12:00:00Z",
        "updated_at": "2026-06-02T12:00:00Z",
        "last_activity_at": "2026-06-02T12:00:00Z",
    }
    return row


def test_brand_list_renders_searchable_operator_queue(client, world):
    _signed_in(client, role="operator")
    world.brands["brand-1"] = _brand()
    world.report_counts["brand-1"] = 2
    world.campaigns.append(
        {
            "id": "camp-1",
            "poster_user_id": "brand-1",
            "title": "summer launch",
            "is_active": True,
            "is_taken_down": False,
        }
    )

    r = client.get("/operator/brands?q=studio&verification=needs_review")
    assert r.status_code == 200
    assert "studio north" in r.text
    assert "needs review" in r.text
    assert ">2</td>" in r.text


def test_brand_detail_renders_private_review_context(client, world):
    _signed_in(client, role="operator")
    world.brands["brand-1"] = _brand()
    world.campaigns.append(
        {
            "id": "camp-1",
            "poster_user_id": "brand-1",
            "title": "summer launch",
            "listing_type": "brand_deal",
            "is_active": True,
            "is_taken_down": False,
            "discovery_eligible": True,
            "created_at": "2026-06-04T12:00:00Z",
        }
    )

    r = client.get("/operator/brands/brand-1")
    assert r.status_code == 200
    assert "studio north" in r.text
    assert "operator-only note" in r.text
    assert "summer launch" in r.text


def test_brand_verification_updates_existing_fields_and_audits(client, world):
    _signed_in(client, role="operator")
    world.brands["brand-1"] = _brand()

    r = client.post(
        "/operator/brands/brand-1/verification",
        data={"action": "verified", "verification_notes": "looks legitimate"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/operator/brands/brand-1"
    assert world.last_verification == {
        "user_id": "brand-1",
        "action": "verified",
        "note": "looks legitimate",
        "operator_id": "op-1",
    }
    assert world.last_audit is not None
    assert world.last_audit["action"] == "brand.verified"
    assert world.last_audit["target_type"] == "brand"


def test_brand_domain_check_records_operator_action(client, world):
    _signed_in(client, role="operator")
    world.brands["brand-1"] = _brand(
        brand_website="https://studio.example",
        verification_status="unverified",
    )

    r = client.post("/operator/brands/brand-1/domain-check")
    assert r.status_code == 303
    assert r.headers["location"] == "/operator/brands/brand-1"
    assert world.last_domain_check == {
        "brand": "brand-1",
        "operator_user_id": "op-1",
    }
    assert world.last_audit is not None
    assert world.last_audit["action"] == "brand.domain_check"


def test_brand_campaigns_page_renders_brand_opportunities(client, world):
    _signed_in(client, role="operator")
    world.brands["brand-1"] = _brand()
    world.campaigns.append(
        {
            "id": "camp-1",
            "poster_user_id": "brand-1",
            "title": "fall creator list",
            "listing_type": "brand_deal",
            "is_active": True,
            "is_taken_down": False,
            "discovery_eligible": True,
            "deadline": "2026-06-10T12:00:00Z",
        }
    )

    r = client.get("/operator/brand-campaigns")
    assert r.status_code == 200
    assert "fall creator list" in r.text
    assert "studio north" in r.text
    assert "visible" in r.text


def test_brand_pages_require_operator(client, world):
    _signed_in(client, role="creator", user_id="creator-1")
    assert client.get("/operator/brands").status_code == 403
    assert client.get("/operator/brand-campaigns").status_code == 403


def test_abuse_profile_preview_can_resolve_brand_targets(client, world):
    _signed_in(client, role="operator")
    world.brands["brand-1"] = _brand(is_verified=True)
    world.report = {
        "id": "report-1",
        "reporter_id": "creator-1",
        "target_type": "profile",
        "target_id": "brand-1",
        "reason": "This profile seems suspicious enough for review.",
        "status": "pending",
        "created_at": "2026-06-05T12:00:00Z",
        "reviewed_at": None,
        "reviewed_by": None,
        "action_notes": None,
    }

    r = client.get("/operator/abuse/report-1")
    assert r.status_code == 200
    assert "Brand" in r.text
    assert "studio north" in r.text
    assert "verified" in r.text
