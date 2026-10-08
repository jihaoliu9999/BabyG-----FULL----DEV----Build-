"""Browser regression test: form posts that redirect to Stripe must work.

"Pay" POSTs to /creator/dm/deals/<id>/pay and "set up payouts" POSTs to
/creator/payouts/start; both answer 303 to a Stripe-hosted page. Chromium
applies the CSP form-action directive to every redirect of a form
submission, so a Stripe origin missing from form-action silently strands the
user on the page. This drives real Chromium against the real app (real
security headers, real POST routes, real 303) with the Stripe URL lookups
stubbed and the external pages answered locally — nothing leaves the
machine.

Needs Playwright + a Chromium build; skipped where they're unavailable
(CI pins the directive itself in test_healthz.py).
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import Response

from app.core import supabase_client
from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import creator_payouts, deal_payments, stripe_client

sync_api = pytest.importorskip("playwright.sync_api")

CHECKOUT = "https://checkout.stripe.com/c/pay/cs_test_regression"
CONNECT = "https://connect.stripe.com/setup/e/acct_test/regression"
GOOGLE = "https://accounts.google.com/o/oauth2/v2/auth?client_id=regression"
UNAPPROVED = "https://evil.example/collect"
DEAL_ID = "77777777-7777-4777-8777-000000000001"
EXTERNAL = re.compile(
    r"^https://(checkout\.stripe\.com|connect\.stripe\.com|accounts\.google\.com|evil\.example)/"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            pytest.skip("local server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def _installed_chromium_builds() -> list[str]:
    """Chromium builds under PLAYWRIGHT_BROWSERS_PATH, for when the installed
    Playwright expects a different build number than the one present."""
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        return []
    paths = [Path(root) / "chromium",
             *sorted(Path(root).glob("chromium-*/chrome-linux/chrome"), reverse=True)]
    return [str(p) for p in paths if p.exists()]


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as playwright:
        chromium = None
        for executable in [None, *_installed_chromium_builds()]:
            try:
                chromium = playwright.chromium.launch(executable_path=executable)
                break
            except Exception:
                continue
        if chromium is None:
            pytest.skip("no Chromium build available")
        yield chromium
        chromium.close()


@pytest.fixture()
def calls(monkeypatch) -> dict[str, Any]:
    """Stub the Stripe URL lookups; record any other Stripe or database use."""
    state: dict[str, Any] = {"payouts_start": 0, "pay": 0, "stripe_client": 0,
                             "database": 0, "payouts_url": CONNECT}

    def onboarding_url(_user_id: str, create_if_missing: bool = True) -> str:
        state["payouts_start"] += 1
        return state["payouts_url"]

    def pay_redirect(_deal_id: str, _payer: str, *, brand: bool) -> str:
        state["pay"] += 1
        return CHECKOUT

    def no_stripe() -> Any:
        state["stripe_client"] += 1
        raise RuntimeError("no Stripe API calls in this test")

    def no_database() -> Any:
        state["database"] += 1
        raise RuntimeError("no database in this test")

    monkeypatch.setattr(creator_payouts, "onboarding_url", onboarding_url)
    monkeypatch.setattr(deal_payments, "pay_redirect", pay_redirect)
    monkeypatch.setattr(stripe_client, "get_stripe_client", no_stripe)
    monkeypatch.setattr(supabase_client, "get_service_client", no_database)
    return state


def _submit(browser: Any, base_url: str, calls: dict[str, Any], action: str) -> dict[str, Any]:
    """Load a real app page (real CSP header), add a same-shape form, tap it."""
    context = browser.new_context()
    external: list[str] = []
    posts: list[str] = []
    violations: list[str] = []
    # Nothing leaves the machine: other off-site requests (fonts) are
    # aborted; the destinations under test are answered locally. The later
    # route wins for URLs both patterns match.
    context.route(re.compile(r"^https?://(?!127\.0\.0\.1[:/])"), lambda route: route.abort())
    context.route(EXTERNAL, lambda route: route.fulfill(
        status=200, body="<p>external</p>", headers={"content-type": "text/html"}))
    page = context.new_page()
    page.on("request", lambda r: external.append(r.url) if EXTERNAL.match(r.url) else None)
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    page.on("console", lambda m: violations.append(m.text) if "form-action" in m.text else None)
    page.goto(base_url + "/")
    page.wait_for_load_state("load")
    # Sign in after the public page loads so "/" isn't redirected.
    session = Response()
    write_session(session, {"user_id": "u-csp", "role": "creator"})
    context.add_cookies([{
        "name": SESSION_COOKIE,
        "value": session.headers["set-cookie"].split(";")[0].split("=", 1)[1],
        "url": base_url,
    }])
    page.evaluate(
        """(action) => {
             const form = document.createElement('form');
             form.method = 'post'; form.action = action; form.id = 'probe';
             const button = document.createElement('button');
             button.type = 'submit'; button.textContent = 'go';
             form.append(button); document.body.prepend(form);
           }""",
        action,
    )
    for key in ("payouts_start", "pay", "stripe_client", "database"):
        calls[key] = 0
    # A blocked submission never navigates, so don't wait for one.
    page.click("#probe button", no_wait_after=True)
    page.wait_for_timeout(1500)
    # Chromium can report a redirected navigation's request more than once;
    # what matters is which origins it went to.
    result = {"external": sorted(set(external)), "posts": posts, "violations": violations,
              "final_url": page.url}
    context.close()
    return result


def test_pay_redirect_reaches_stripe_checkout(browser, base_url, calls) -> None:
    result = _submit(browser, base_url, calls, f"/creator/dm/deals/{DEAL_ID}/pay")
    assert result["external"] == [CHECKOUT]
    assert not result["final_url"].startswith(base_url)  # left the app
    assert result["violations"] == []
    assert len(result["posts"]) == 1
    assert calls["pay"] == 1
    assert calls["stripe_client"] == 0 and calls["database"] == 0


def test_payout_setup_redirect_reaches_stripe_connect(browser, base_url, calls) -> None:
    result = _submit(browser, base_url, calls, "/creator/payouts/start")
    assert result["external"] == [CONNECT]
    assert not result["final_url"].startswith(base_url)  # left the app
    assert result["violations"] == []
    assert len(result["posts"]) == 1
    assert calls["payouts_start"] == 1
    assert calls["stripe_client"] == 0 and calls["database"] == 0


def test_google_oauth_redirect_still_allowed(browser, base_url, calls) -> None:
    calls["payouts_url"] = GOOGLE
    result = _submit(browser, base_url, calls, "/creator/payouts/start")
    assert result["external"] == [GOOGLE]
    assert not result["final_url"].startswith(base_url)  # left the app
    assert result["violations"] == []


def test_redirect_to_unapproved_origin_still_blocked(browser, base_url, calls) -> None:
    calls["payouts_url"] = UNAPPROVED
    result = _submit(browser, base_url, calls, "/creator/payouts/start")
    assert result["external"] == []
    assert result["violations"], "Chromium should report the form-action violation"
    assert result["final_url"] == base_url + "/"


def test_form_posting_straight_to_unapproved_origin_still_blocked(
    browser, base_url, calls
) -> None:
    result = _submit(browser, base_url, calls, UNAPPROVED)
    assert result["external"] == []
    assert result["posts"] == []
    assert result["violations"], "Chromium should report the form-action violation"
