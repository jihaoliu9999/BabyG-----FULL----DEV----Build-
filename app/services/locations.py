"""Location validation helpers.

Location is optional and permissioned. Browser coordinates are stored only
after an explicit user click and are never suitable for public rendering.

When a creator clicks "use my location" and we get coords with no
city/region/country, we reverse-geocode server-side via BigDataCloud's
free keyless endpoint so the saved row carries a renderable label.
The client also does its own reverse-geocode for instant UX, but the
server is the source of truth — it runs every save and survives
network races, JS failures, and stale deploys.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

REVERSE_GEOCODE_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"
# Tight timeout: a slow geocode shouldn't hold up the save. If the
# upstream is laggy we save coords without a label; the UI degrades
# but never blocks.
REVERSE_GEOCODE_TIMEOUT_SECONDS = 4.0


def profile_location_payload(form: Any) -> tuple[dict[str, Any], str | None]:
    city = _str(form.get("location_city"), 80)
    region = _str(form.get("location_region"), 80)
    country = _str(form.get("location_country"), 80)
    source = _str(form.get("location_source"), 20)
    raw_lat = _str(form.get("location_lat"), 40)
    raw_lng = _str(form.get("location_lng"), 40)
    lat, lng, coord_error = _parse_coordinates(raw_lat, raw_lng)
    if coord_error:
        return {}, coord_error

    has_label = bool(city or region or country)
    has_coords = lat is not None and lng is not None
    clean_source = "browser" if source == "browser" and has_coords else "manual"
    if not has_label and not has_coords:
        clean_source = ""

    # Server-side reverse-geocode safety net. The client tries first
    # (instant UX, populates the visible inputs), but if it raced the
    # save or the fetch was blocked, the server fills the gap so the
    # saved row always carries a renderable label when coords exist.
    if (
        clean_source == "browser"
        and lat is not None
        and lng is not None
        and not has_label
    ):
        geo = _reverse_geocode(lat, lng)
        if geo:
            city = city or geo.get("city", "")
            region = region or geo.get("region", "")
            country = country or geo.get("country", "")

    return {
        "location_city": city or None,
        "location_region": region or None,
        "location_country": country or None,
        "location_lat": lat,
        "location_lng": lng,
        "location_source": clean_source or None,
        "location_updated_at": datetime.now(UTC).isoformat() if clean_source else None,
    }, None


def _reverse_geocode(lat: float, lng: float) -> dict[str, str] | None:
    """Call BigDataCloud's keyless reverse-geocode endpoint and project
    the fields we render. Returns None on any failure (timeout,
    non-200, JSON parse) — caller saves coords without a label.

    Status-only logging — coords are not logged because they're
    private creator data."""
    try:
        with httpx.Client(timeout=REVERSE_GEOCODE_TIMEOUT_SECONDS) as client:
            response = client.get(
                REVERSE_GEOCODE_URL,
                params={
                    "latitude": str(lat),
                    "longitude": str(lng),
                    "localityLanguage": "en",
                },
            )
    except httpx.HTTPError:
        logger.info("reverse-geocode transport failure")
        return None
    if response.status_code != 200:
        logger.info("reverse-geocode status %s", response.status_code)
        return None
    try:
        data = response.json()
    except ValueError:
        logger.info("reverse-geocode non-json response")
        return None
    if not isinstance(data, dict):
        return None
    city = _clean(data.get("city")) or _clean(data.get("locality"))
    region = _clean(data.get("principalSubdivision"))
    country = _clean(data.get("countryName"))
    if not (city or region or country):
        return None
    return {"city": city, "region": region, "country": country}


def _parse_coordinates(
    raw_lat: str, raw_lng: str
) -> tuple[float | None, float | None, str | None]:
    if not raw_lat and not raw_lng:
        return None, None, None
    if not raw_lat or not raw_lng:
        return None, None, "location needs both latitude and longitude."
    try:
        lat = float(raw_lat)
        lng = float(raw_lng)
    except ValueError:
        return None, None, "location coordinates were invalid."
    if not (-90 <= lat <= 90):
        return None, None, "latitude must be between -90 and 90."
    if not (-180 <= lng <= 180):
        return None, None, "longitude must be between -180 and 180."
    return lat, lng, None


def _str(value: Any, max_len: int) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())[:max_len]


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())[:80]
