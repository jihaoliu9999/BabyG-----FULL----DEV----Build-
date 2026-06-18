"""Privacy projection — internal creator fields must not leak to other users.

`profiles.public_creator` is the gate: any row returned from a service
helper used by a cross-user surface gets projected through it so
accidental additions to the schema (or to a template) don't ship the
row's full contents.

What we lock in here:
  * `PUBLIC_CREATOR_FIELDS` doesn't include any of the known-internal
    fields (`baseline_followers`, `tier`, `writing_samples`,
    `notification_settings`, `sub_bot_persona`, `brand_preferences`).
  * `public_creator` strips those fields.
  * `creators.get_for_view` returns projected rows.

`public_brand` and `PUBLIC_BRAND_FIELDS` shipped in v1 but were
removed when brand scope deferred to v1.5 (brand-side-v1.5 branch).
"""

from __future__ import annotations

from app.services.profiles import PUBLIC_CREATOR_FIELDS, public_creator

_SECRET_CREATOR_FIELDS = (
    "baseline_followers",
    "baseline_engagement_rate",
    "writing_samples",
    "brand_preferences",
    "notification_settings",
    "sub_bot_persona",
    "tier",
    "location_lat",
    "location_lng",
    # Phase 3 owner-private prefs (migration 0016). These never appear
    # in peer/brand/operator views — they're the creator's own settings
    # for how babyg behaves and how their location/DMs are gated.
    "dm_preference",
    "location_display_level",
    "babyg_tone",
    "babyg_risk_tolerance",
    "babyg_auto_brief_dms",
    "babyg_email_assistance",
    # Phase 3 deal prefs (migration 0017). Surfaced to babyg-side
    # drafting only — peers / brands never see a creator's rate floor
    # or travel willingness through public_creator().
    "deal_min_rate_text",
    "deal_usage_rights_default",
    "deal_travel_willingness",
)


# ---------- allowlist hygiene ----------


def test_creator_allowlist_excludes_internal_fields():
    for f in _SECRET_CREATOR_FIELDS:
        assert f not in PUBLIC_CREATOR_FIELDS, (
            f"PUBLIC_CREATOR_FIELDS must not include {f!r} — it would leak "
            "internal data the moment a template starts rendering it."
        )


def test_creator_allowlist_includes_profile_photo_and_updated_at():
    """Avatars across the app render via the public projection — if
    these two fall out, every cross-user surface drops back to
    initials-only and the cache-buster goes silent."""
    assert "profile_photo_url" in PUBLIC_CREATOR_FIELDS
    assert "updated_at" in PUBLIC_CREATOR_FIELDS


def test_public_creator_surfaces_photo_and_cache_buster():
    row = {
        "user_id": "u1",
        "full_name": "Anna",
        "profile_photo_url": "https://example.test/u1.jpg",
        "updated_at": "2026-06-16T12:00:00Z",
        "writing_samples": "secret",
        "location_lat": 34.0,
    }
    out = public_creator(row)
    assert out is not None
    assert out["profile_photo_url"] == "https://example.test/u1.jpg"
    assert out["updated_at"] == "2026-06-16T12:00:00Z"
    # Privacy guards still hold for known-internal fields.
    assert "writing_samples" not in out
    assert "location_lat" not in out


# ---------- projection behavior ----------


def test_public_creator_strips_internal_fields():
    row = {
        "user_id": "u1",
        "full_name": "Anna",
        "tier": "vip",                                  # private
        "baseline_followers": 92873,                    # private
        "writing_samples": "draft text",                # private
        "notification_settings": {"dm": True},          # private
        "sub_bot_persona": "voice notes",               # private
        "location_city": "Los Angeles",
        "location_region": "California",
        "location_lat": 34.0522,                        # private
        "location_lng": -118.2437,                      # private
        # Phase 3 owner-private prefs — must be stripped even though
        # the cross-user fetcher does `select("*")`. Use 'city' here
        # so the label assertion below still holds; the level=hidden
        # behavior is covered separately in
        # test_location_label_hidden_returns_none.
        "dm_preference": "connections_only",
        "location_display_level": "city",
        "babyg_tone": "direct",
        "babyg_risk_tolerance": "cautious",
        "babyg_auto_brief_dms": False,
        "babyg_email_assistance": True,
        # Deal prefs (migration 0017) — never leak.
        "deal_min_rate_text": "$2.5k organic",
        "deal_usage_rights_default": "paid_with_usage",
        "deal_travel_willingness": "regional",
    }
    out = public_creator(row)
    assert out is not None
    assert out["full_name"] == "Anna"
    assert out["location_label"] == "Los Angeles, California"
    for k in _SECRET_CREATOR_FIELDS:
        assert k not in out, f"public_creator leaked {k!r}"


def test_public_creator_passes_through_none():
    assert public_creator(None) is None


# ---------- location_display_level (Phase 3, migration 0016) ----------


def test_location_label_defaults_to_city_when_pref_missing():
    """Rows that predate migration 0016 (or where the column is null)
    must still render the full city/region label — backwards compat."""
    row = {
        "location_city": "Los Angeles",
        "location_region": "California",
        "location_country": "United States",
    }
    out = public_creator(row)
    assert out is not None
    assert out["location_label"] == "Los Angeles, California"


def test_location_label_city_level_renders_city_first():
    row = {
        "location_city": "Brooklyn",
        "location_region": "New York",
        "location_country": "United States",
        "location_display_level": "city",
    }
    out = public_creator(row)
    assert out is not None
    assert out["location_label"] == "Brooklyn, New York"


def test_location_label_region_level_drops_city():
    """A creator who wants region/state but not their specific neighborhood
    sets level=region. Output skips the city entirely."""
    row = {
        "location_city": "Brooklyn",
        "location_region": "New York",
        "location_country": "United States",
        "location_display_level": "region",
    }
    out = public_creator(row)
    assert out is not None
    assert out["location_label"] == "New York, United States"


def test_location_label_region_level_falls_back_to_country_when_region_missing():
    row = {
        "location_city": "Brooklyn",
        "location_region": "",
        "location_country": "United States",
        "location_display_level": "region",
    }
    out = public_creator(row)
    assert out is not None
    assert out["location_label"] == "United States"


def test_location_label_hidden_returns_none():
    """When level=hidden, no location text leaks — not even country."""
    row = {
        "location_city": "Brooklyn",
        "location_region": "New York",
        "location_country": "United States",
        "location_display_level": "hidden",
    }
    out = public_creator(row)
    assert out is not None
    assert out["location_label"] is None


def test_location_label_unknown_level_falls_back_to_city():
    """Defensive: an unexpected value in the column doesn't crash and
    doesn't accidentally leak more than `city` would."""
    row = {
        "location_city": "Brooklyn",
        "location_region": "New York",
        "location_display_level": "country_only_or_whatever",
    }
    out = public_creator(row)
    assert out is not None
    assert out["location_label"] == "Brooklyn, New York"


def test_public_creator_fills_missing_fields_with_none():
    """A row that doesn't carry every allowlist field still returns
    every key — templates can `{{ p.bio or '...' }}` without KeyError."""
    out = public_creator({"user_id": "u1", "full_name": "Anna"})
    assert out is not None
    for f in PUBLIC_CREATOR_FIELDS:
        assert f in out


# ---------- end-to-end through creators.get_for_view ----------


def test_creators_get_for_view_returns_projected_row(monkeypatch):
    from types import SimpleNamespace

    from app.core import supabase_client
    from app.services import creators

    full_row = {
        "user_id": "u1",
        "full_name": "Anna",
        "tier": "pro",
        "writing_samples": "secret draft",
        "baseline_followers": 12345,
    }

    monkeypatch.setattr(
        supabase_client,
        "get_service_client",
        lambda: SimpleNamespace(
            table=lambda *_: SimpleNamespace(
                select=lambda *_a, **_k: SimpleNamespace(
                    eq=lambda *_a, **_k: SimpleNamespace(
                        limit=lambda *_a, **_k: SimpleNamespace(
                            execute=lambda: SimpleNamespace(data=[full_row])
                        )
                    )
                )
            )
        ),
    )

    out = creators.get_for_view("u1")
    assert out is not None
    assert out["full_name"] == "Anna"
    assert "tier" not in out
    assert "writing_samples" not in out
    assert "baseline_followers" not in out
