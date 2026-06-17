"""Unit tests for the server-side reverse-geocode helper.

The fallback is the load-bearing piece: when the client-side
reverse-geocode misses (race with the save click, blocked fetch,
stale deploy), the server must still produce a renderable city/region/
country label from the saved coords.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.services import locations as locations_service


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, *_args: object, **_kwargs: object) -> _FakeResponse:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _install_client(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    def factory(*_args: object, **_kwargs: object) -> _FakeClient:
        return _FakeClient(response)

    monkeypatch.setattr(locations_service.httpx, "Client", factory)


def test_reverse_geocode_returns_city_region_country(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "city": "Boca Raton",
                "principalSubdivision": "Florida",
                "countryName": "United States",
            },
        ),
    )

    result = locations_service._reverse_geocode(26.4184, -80.0754)

    assert result == {
        "city": "Boca Raton",
        "region": "Florida",
        "country": "United States",
    }


def test_reverse_geocode_falls_back_to_locality_when_city_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "city": "",
                "locality": "Some Town",
                "principalSubdivision": "Ontario",
                "countryName": "Canada",
            },
        ),
    )

    result = locations_service._reverse_geocode(43.6532, -79.3832)

    assert result is not None
    assert result["city"] == "Some Town"


def test_reverse_geocode_returns_none_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(monkeypatch, httpx.ConnectError("boom"))

    assert locations_service._reverse_geocode(0.0, 0.0) is None


def test_reverse_geocode_returns_none_on_non_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(monkeypatch, _FakeResponse(503, {}))

    assert locations_service._reverse_geocode(0.0, 0.0) is None


def test_reverse_geocode_returns_none_on_non_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(monkeypatch, _FakeResponse(200, ValueError("not json")))

    assert locations_service._reverse_geocode(0.0, 0.0) is None


def test_reverse_geocode_returns_none_when_all_fields_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(monkeypatch, _FakeResponse(200, {"city": "", "locality": ""}))

    assert locations_service._reverse_geocode(0.0, 0.0) is None


def test_profile_location_payload_invokes_server_geocode_for_browser_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: browser source + coords + no label → server fills label."""
    monkeypatch.setattr(
        locations_service,
        "_reverse_geocode",
        lambda lat, lng: {
            "city": "Boca Raton",
            "region": "Florida",
            "country": "United States",
        },
    )

    form = {
        "location_city": "",
        "location_region": "",
        "location_country": "",
        "location_source": "browser",
        "location_lat": "26.4184",
        "location_lng": "-80.0754",
    }

    payload, error = locations_service.profile_location_payload(form)

    assert error is None
    assert payload["location_city"] == "Boca Raton"
    assert payload["location_region"] == "Florida"
    assert payload["location_country"] == "United States"
    assert payload["location_lat"] == 26.4184
    assert payload["location_source"] == "browser"


def test_profile_location_payload_skips_geocode_for_manual_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual entry must never trigger an outbound HTTP call. The user
    is typing — there's nothing to geocode against and we don't want
    to leak coords we never had."""
    called: dict[str, bool] = {"hit": False}

    def _track(lat: float, lng: float) -> dict[str, str]:
        called["hit"] = True
        return {"city": "X", "region": "Y", "country": "Z"}

    monkeypatch.setattr(locations_service, "_reverse_geocode", _track)

    payload, error = locations_service.profile_location_payload(
        {
            "location_city": "New York",
            "location_region": "",
            "location_country": "",
            "location_source": "manual",
        }
    )

    assert error is None
    assert payload["location_city"] == "New York"
    assert called["hit"] is False
