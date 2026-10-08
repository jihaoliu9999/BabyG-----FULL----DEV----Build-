"""Smoke test that the scaffold boots and /healthz responds."""

from fastapi.testclient import TestClient

from app.config import get_settings


def test_healthz_returns_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # `env` is intentionally omitted to avoid leaking deployment metadata
    # to drive-by scanners.
    assert "env" not in body


def test_robots_allows_public_indexing(client: TestClient) -> None:
    response = client.get("/robots.txt")
    assert response.status_code == 200
    lines = response.text.splitlines()
    assert "Allow: /" in lines
    assert "Disallow: /" not in lines
    assert "Disallow: /creator/" in lines


def test_csp_allows_same_origin_static_css(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    # CSS link is root-relative + cache-busted with a content hash.
    assert 'href="/static/css/app.css?v=' in response.text
    assert 'href="http://testserver/static/css/app.css' not in response.text
    assert 'href="https://testserver/static/css/app.css' not in response.text
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "style-src-elem 'self'" in csp
    assert "frame-ancestors 'none'" in csp

    css = client.get("/static/css/app.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "babyg - premium dark theme" in css.text


def _csp_directives(csp: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for part in csp.split(";"):
        tokens = part.split()
        if tokens:
            directives[tokens[0]] = tokens[1:]
    return directives


def test_csp_form_action_allows_oauth_and_stripe_redirects(client: TestClient) -> None:
    """The Google OAuth picker, "Pay" (Stripe Checkout) and "set up
    payouts" (Stripe Connect onboarding) all POST same-origin and then
    redirect off-site. Browsers enforce form-action across the entire
    navigation chain, so each redirect target must be allow-listed or the
    redirect silently fails. Exactly these origins — nothing broader."""
    response = client.get("/")
    assert response.status_code == 200
    csp = _csp_directives(response.headers["content-security-policy"])
    assert csp["form-action"] == [
        "'self'",
        "https://accounts.google.com",
        "https://connect.stripe.com",
        "https://checkout.stripe.com",
    ]


def test_csp_other_directives_unchanged(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    csp = _csp_directives(response.headers["content-security-policy"])
    assert csp["default-src"] == ["'self'"]
    assert csp["img-src"][:2] == ["'self'", "data:"]
    assert csp["script-src"] == ["'self'"]
    assert csp["style-src"] == ["'self'", "'unsafe-inline'"]
    assert csp["style-src-elem"] == ["'self'"]
    assert csp["connect-src"] == ["'self'", "https://api.bigdatacloud.net"]
    assert csp["frame-ancestors"] == ["'none'"]
    assert set(csp) == {
        "default-src", "img-src", "script-src", "style-src", "style-src-elem",
        "connect-src", "form-action", "frame-ancestors",
    }


def test_csp_connect_src_allows_reverse_geocode(client: TestClient) -> None:
    """The location flow reverse-geocodes browser coords client-side via
    BigDataCloud's keyless endpoint. Without an explicit connect-src
    entry the fetch is blocked by default-src 'self'."""
    response = client.get("/")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "connect-src 'self' https://api.bigdatacloud.net" in csp


def test_forwarded_proto_keeps_static_url_root_relative(client: TestClient) -> None:
    response = client.get("/", headers={"x-forwarded-proto": "https"})

    assert response.status_code == 200
    assert 'href="/static/css/app.css?v=' in response.text
    assert 'href="http://testserver/static/css/app.css' not in response.text
    assert 'href="https://testserver/static/css/app.css' not in response.text


def test_production_csp_upgrades_insecure_subresources(
    monkeypatch, client: TestClient
) -> None:
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "x" * 48)
    get_settings.cache_clear()

    response = client.get("/")

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "upgrade-insecure-requests" in csp
    assert 'href="/static/css/app.css?v=' in response.text
