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


def test_robots_disallows_indexing(client: TestClient) -> None:
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "Disallow: /" in response.text


def test_csp_allows_same_origin_static_css(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/static/css/app.css"' in response.text
    assert 'href="http://testserver/static/css/app.css"' not in response.text
    assert 'href="https://testserver/static/css/app.css"' not in response.text
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "style-src-elem 'self'" in csp
    assert "frame-ancestors 'none'" in csp

    css = client.get("/static/css/app.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "babyg - premium dark theme" in css.text


def test_forwarded_proto_keeps_static_url_root_relative(client: TestClient) -> None:
    response = client.get("/", headers={"x-forwarded-proto": "https"})

    assert response.status_code == 200
    assert 'href="/static/css/app.css"' in response.text
    assert 'href="http://testserver/static/css/app.css"' not in response.text
    assert 'href="https://testserver/static/css/app.css"' not in response.text


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
    assert 'href="/static/css/app.css"' in response.text
