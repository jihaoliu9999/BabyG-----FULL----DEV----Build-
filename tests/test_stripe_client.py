"""Outbound Stripe client setup stays lazy, server-side, and offline."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import stripe

from app.services import stripe_client


def test_configured_client_initializes_without_api_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stripe_client,
        "get_settings",
        lambda: SimpleNamespace(stripe_secret_key="sk_test_not_a_real_key"),
    )

    def no_api_request(*args: object, **kwargs: object) -> None:
        pytest.fail("Stripe API was called during client initialization")

    monkeypatch.setattr(stripe._stripe_client._APIRequestor, "request", no_api_request)
    client = stripe_client.get_stripe_client()

    assert isinstance(client, stripe.StripeClient)


def test_missing_key_fails_without_leaking_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        stripe_client,
        "get_settings",
        lambda: SimpleNamespace(stripe_secret_key=" "),
    )
    with pytest.raises(RuntimeError, match="^Stripe is not configured$"):
        stripe_client.get_stripe_client()
    assert not caplog.records
    assert capsys.readouterr() == ("", "")


def test_client_initialization_error_does_not_expose_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key = "sk_test_secret_must_not_escape"
    monkeypatch.setattr(
        stripe_client,
        "get_settings",
        lambda: SimpleNamespace(stripe_secret_key=key),
    )

    def fail_constructor(*args: object, **kwargs: object) -> None:
        raise ValueError(f"bad key: {key}")

    monkeypatch.setattr(stripe, "StripeClient", fail_constructor)
    with pytest.raises(RuntimeError, match="^Stripe client initialization failed$") as exc:
        stripe_client.get_stripe_client()
    assert key not in str(exc.value)
    assert not caplog.records
    assert key not in str(capsys.readouterr())
