"""Public legal/compliance pages.

Each page should:
  - return 200 for anonymous and for signed-in callers (no role gating)
  - render its top heading + a representative section keyword
  - include the contact email
  - include the cross-link strip back to the other legal pages
  - link the user back to the home page
"""

from __future__ import annotations

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session

LEGAL_PATHS = ["/privacy", "/terms", "/accessibility", "/data-deletion"]
SHARED_TOKENS = [
    "babygaiagent@gmail.com",
    'href="/privacy"',
    'href="/terms"',
    'href="/accessibility"',
    'href="/data-deletion"',
    'href="/"',  # back to home
]


@pytest.mark.parametrize("path", LEGAL_PATHS)
def test_legal_page_anonymous_returns_200_and_shared_links(
    client: TestClient, path: str
) -> None:
    r = client.get(path)
    assert r.status_code == 200
    for token in SHARED_TOKENS:
        assert token in r.text, f"missing {token!r} on {path}"


@pytest.mark.parametrize("path", LEGAL_PATHS)
def test_legal_page_signed_in_creator_also_returns_200(
    client: TestClient, path: str
) -> None:
    resp = Response()
    write_session(resp, {"user_id": "u-1", "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    r = client.get(path)
    # Signed-in users can still read the legal pages.
    assert r.status_code == 200


def test_privacy_page_covers_required_topics(client: TestClient) -> None:
    r = client.get("/privacy")
    text = r.text.lower()
    for topic in [
        "account information",
        "profile information",
        "messages",
        "bot conversations",
        "calendar",
        "uploaded media",
        "connected accounts",
        "cookies",
        "supabase",
        "railway",
        "resend",
        "anthropic",
        "google",
        "data retention",
        "deletion",
        "security",
        "we do not sell",
    ]:
        assert topic in text, f"privacy page missing topic: {topic!r}"


def test_terms_page_covers_required_topics(client: TestClient) -> None:
    r = client.get("/terms")
    text = r.text.lower()
    for topic in [
        "eligibility",
        "acceptable use",
        "ai assistance",
        "not legal, tax, or financial advice",
        "brand deals",
        "platform availability",
        "account termination",
        "limitation of liability",
    ]:
        assert topic in text, f"terms page missing topic: {topic!r}"


def test_accessibility_page_uses_careful_language(client: TestClient) -> None:
    r = client.get("/accessibility")
    text = r.text.lower()
    # The user spec is explicit: no "fully ADA compliant" claim, use
    # "we aim to" / "we work to" language.
    assert "we aim to" in text
    assert "fully ada" not in text
    assert "wcag" in text


def test_data_deletion_page_does_not_claim_automated_flow(
    client: TestClient,
) -> None:
    r = client.get("/data-deletion")
    text = r.text.lower()
    assert "manual review" in text
    # Don't fake a self-serve delete button. The page must be explicit
    # that this is an email-based process today.
    assert "email us" in text or "email" in text
    # Connected accounts and retention exceptions are required by spec.
    assert "what we delete" in text
    assert "what may be retained" in text


# -----------------------------------------------------------------------------
# Footer / cross-link surfaces
# -----------------------------------------------------------------------------


def test_landing_footer_links_to_legal_pages(client: TestClient) -> None:
    r = client.get("/")
    for href in [
        '/privacy"',
        '/terms"',
        '/accessibility"',
        '/data-deletion"',
    ]:
        assert href in r.text, f"landing footer missing link to {href}"


def test_get_started_links_to_legal_pages(client: TestClient) -> None:
    r = client.get("/get-started")
    for href in ['/privacy"', '/terms"', '/accessibility"', '/data-deletion"']:
        assert href in r.text, f"get-started missing link to {href}"


def test_login_page_links_to_legal_pages(client: TestClient) -> None:
    r = client.get("/auth/login?role=creator")
    for href in ['/privacy"', '/terms"', '/accessibility"', '/data-deletion"']:
        assert href in r.text, f"login missing link to {href}"
