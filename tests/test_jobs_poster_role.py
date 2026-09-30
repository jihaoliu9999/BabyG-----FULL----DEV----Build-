"""Listing writes use the authenticated caller's role, never payload identity."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import supabase_client
from app.services import jobs


@pytest.mark.parametrize("role,spoofed", [("brand", "creator"), ("creator", "brand")])
def test_create_persists_authoritative_poster_identity(
    monkeypatch: pytest.MonkeyPatch, role: str, spoofed: str
) -> None:
    captured: dict = {}

    class FakeTable:
        def insert(self, body: dict) -> FakeTable:
            captured.update(body)
            return self

        def execute(self) -> SimpleNamespace:
            return SimpleNamespace(data=[{"id": "listing-1"}])

    class FakeClient:
        def table(self, name: str) -> FakeTable:
            assert name == "creator_job_listings"
            return FakeTable()

    monkeypatch.setattr(supabase_client, "get_service_client", FakeClient)
    listing_id = jobs.create(
        poster_id="authenticated-user",
        poster_role=role,
        payload={
            "title": "A real opportunity",
            "listing_type": "brand_deal",
            "poster_user_id": "spoofed-user",
            "poster_role": spoofed,
        },
    )

    assert listing_id == "listing-1"
    assert captured == {
        "title": "A real opportunity",
        "listing_type": "brand_deal",
        "poster_user_id": "authenticated-user",
        "poster_role": role,
    }


def test_create_rejects_unsupported_poster_role() -> None:
    with pytest.raises(ValueError, match="poster_role"):
        jobs.create(poster_id="authenticated-user", poster_role="operator", payload={})
