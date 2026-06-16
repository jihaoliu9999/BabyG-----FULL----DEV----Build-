"""Location validation helpers.

Location is optional and permissioned. Browser coordinates are stored only
after an explicit user click and are never suitable for public rendering.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


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
    if source == "browser" and has_coords and not has_label:
        return {}, "enter a city or region with your browser location."
    clean_source = "browser" if source == "browser" and has_coords else "manual"
    if not has_label and not has_coords:
        clean_source = ""

    return {
        "location_city": city or None,
        "location_region": region or None,
        "location_country": country or None,
        "location_lat": lat,
        "location_lng": lng,
        "location_source": clean_source or None,
        "location_updated_at": datetime.now(UTC).isoformat() if clean_source else None,
    }, None


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
