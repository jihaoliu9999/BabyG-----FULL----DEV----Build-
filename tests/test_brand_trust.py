"""Brand trust helper behavior."""

from __future__ import annotations

from app.services import brand_trust


def test_normalize_domain_accepts_url_email_and_bare_domain() -> None:
    assert brand_trust.normalize_domain("https://www.example.com/path") == "example.com"
    assert brand_trust.normalize_domain("ops@mail.example.com") == "mail.example.com"
    assert brand_trust.normalize_domain("brand.example") == "brand.example"


def test_domain_match_passes_matching_domains() -> None:
    check = brand_trust.domain_match_status(
        website="https://studio.example",
        email_domain="studio.example",
    )
    assert check["result_status"] == "pass"
    assert check["website_domain"] == "studio.example"


def test_domain_match_warns_on_mismatch_without_accusatory_copy() -> None:
    check = brand_trust.domain_match_status(
        website="https://studio.example",
        email_domain="other.example",
    )
    assert check["result_status"] == "warn"
    assert "do not match" in check["summary"]


def test_public_trust_uses_careful_labels() -> None:
    summary = brand_trust.public_trust({"verification_status": "high_risk"})
    assert summary == {
        "status": "high_risk",
        "label": "risk signals present",
        "tone": "risk",
        "summary": "treat with caution and confirm details first.",
    }
    assert "fraud" not in " ".join(summary.values())
    assert "scam" not in " ".join(summary.values())


def test_compute_trust_can_derive_likely_legitimate_from_existing_fields() -> None:
    summary = brand_trust.compute_trust_summary(
        {
            "verification_status": "unverified",
            "company_name": "studio",
            "brand_website": "https://studio.example",
            "industry": "fashion",
            "contact_full_name": "alex",
            "product_description": "fragrance",
            "contact_email_domain": "studio.example",
        }
    )
    assert summary["status"] == "likely_legitimate"
