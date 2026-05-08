"""Smoke test that the scaffold boots and /healthz responds."""

from fastapi.testclient import TestClient


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
