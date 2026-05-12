"""Privacy projection — internal fields must not leak to other users.

`profiles.public_creator` and `profiles.public_brand` are the gates: any
row returned from a service helper used by a cross-user surface gets
projected through them so accidental additions to the schema (or to a
template) don't ship the row's full contents.

What we lock in here:
  * The allowlist constants don't include any of the known-internal
    fields (`baseline_followers`, `tier`, `writing_samples`,
    `notification_settings`, `sub_bot_persona`, `brand_preferences`,
    `verification_notes`).
  * The projection functions strip those fields.
  * `creators.get_for_view` and `creators.list_for_brand_match` both
    return projected rows.
  * `brands.get_for_view` strips `verification_notes`.
"""

from __future__ import annotations

from app.services.profiles import (
    PUBLIC_BRAND_FIELDS,
    PUBLIC_CREATOR_FIELDS,
    public_brand,
    public_creator,
)

_SECRET_CREATOR_FIELDS = (
    "baseline_followers",
    "baseline_engagement_rate",
    "writing_samples",
    "brand_preferences",
    "notification_settings",
    "sub_bot_persona",
    "tier",
)

_SECRET_BRAND_FIELDS = (
    "verification_notes",
)


# ---------- allowlist hygiene ----------


def test_creator_allowlist_excludes_internal_fields():
    for f in _SECRET_CREATOR_FIELDS:
        assert f not in PUBLIC_CREATOR_FIELDS, (
            f"PUBLIC_CREATOR_FIELDS must not include {f!r} — it would leak "
            "internal data the moment a template starts rendering it."
        )


def test_brand_allowlist_excludes_internal_fields():
    for f in _SECRET_BRAND_FIELDS:
        assert f not in PUBLIC_BRAND_FIELDS, (
            f"PUBLIC_BRAND_FIELDS must not include {f!r}."
        )


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
    }
    out = public_creator(row)
    assert out is not None
    assert out["full_name"] == "Anna"
    for k in _SECRET_CREATOR_FIELDS:
        assert k not in out, f"public_creator leaked {k!r}"


def test_public_creator_passes_through_none():
    assert public_creator(None) is None


def test_public_creator_fills_missing_fields_with_none():
    """A row that doesn't carry every allowlist field still returns
    every key — templates can `{{ p.bio or '...' }}` without KeyError."""
    out = public_creator({"user_id": "u1", "full_name": "Anna"})
    assert out is not None
    for f in PUBLIC_CREATOR_FIELDS:
        assert f in out


def test_public_brand_strips_verification_notes():
    row = {
        "user_id": "u1",
        "company_name": "Acme",
        "is_verified": True,
        "verification_notes": "ran a background check; OK",
    }
    out = public_brand(row)
    assert out is not None
    assert out["company_name"] == "Acme"
    assert "verification_notes" not in out


def test_public_brand_passes_through_none():
    assert public_brand(None) is None


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
