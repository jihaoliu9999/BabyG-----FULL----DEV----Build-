"""Profile photo upload + delete routes.

The Pillow + Supabase Storage layer is patched out — these tests cover
the route surface (auth, validation, redirect, persistence call) without
talking to real storage.
"""

from __future__ import annotations

import io

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from PIL import Image

from app.core.security import SESSION_COOKIE, write_session
from app.routes import creator as creator_routes
from app.services import storage as storage_module


def _signed_in(client: TestClient, *, role: str = "creator", user_id: str = "u-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def _png_bytes(size: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color="white").save(buf, format="PNG")
    return buf.getvalue()


# -----------------------------------------------------------------------------
# upload
# -----------------------------------------------------------------------------


def test_upload_requires_creator_session(client: TestClient) -> None:
    # No session → 401 (FastAPI dependency rejects before parsing the file).
    r = client.post(
        "/creator/profile/photo",
        files={"photo": ("p.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_upload_rejects_operator(client: TestClient) -> None:
    _signed_in(client, role="operator", user_id="op-1")
    r = client.post(
        "/creator/profile/photo",
        files={"photo": ("p.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_upload_happy_path(monkeypatch, client: TestClient) -> None:
    _signed_in(client)
    calls = {}

    def _fake_upload(user_id: str, raw: bytes, content_type: str | None) -> str:
        calls["upload"] = (user_id, len(raw), content_type)
        return "https://cdn.example/u-1.jpg"

    saved = {}

    def _fake_update(user_id: str, payload: dict) -> bool:
        saved.update(payload)
        saved["_user_id"] = user_id
        return True

    monkeypatch.setattr(storage_module, "upload_profile_photo", _fake_upload)
    monkeypatch.setattr(creator_routes.profiles, "update_creator_profile", _fake_update)

    r = client.post(
        "/creator/profile/photo",
        files={"photo": ("p.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/profile?photo=ok"
    assert calls["upload"][0] == "u-1"
    assert calls["upload"][2] == "image/png"
    assert saved["_user_id"] == "u-1"
    assert saved["profile_photo_url"] == "https://cdn.example/u-1.jpg"


def test_upload_empty_file_redirects_missing(monkeypatch, client: TestClient) -> None:
    _signed_in(client)
    # Storage isn't reached, but patch it so a regression that DOES call
    # storage on empty input would fail loudly instead of silently uploading.
    monkeypatch.setattr(
        storage_module,
        "upload_profile_photo",
        lambda *a, **kw: pytest.fail("storage was called for empty upload"),
    )

    r = client.post(
        "/creator/profile/photo",
        files={"photo": ("p.png", b"", "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/profile?photo=missing"


@pytest.mark.parametrize(
    "exc,location_suffix",
    [
        (storage_module.PhotoTooLargeError("too big"), "too_big"),
        (storage_module.PhotoUnsupportedTypeError("text/plain"), "bad_type"),
        (storage_module.PhotoDecodeError("not an image"), "corrupt"),
        (storage_module.PhotoStorageError("supabase down"), "storage_failed"),
    ],
)
def test_upload_storage_errors_map_to_flash_codes(
    monkeypatch, client: TestClient, exc: Exception, location_suffix: str
) -> None:
    _signed_in(client)
    monkeypatch.setattr(
        storage_module,
        "upload_profile_photo",
        lambda *a, **kw: (_ for _ in ()).throw(exc),
    )

    r = client.post(
        "/creator/profile/photo",
        files={"photo": ("p.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/creator/profile?photo={location_suffix}"


def test_upload_db_save_failure_surfaces(monkeypatch, client: TestClient) -> None:
    _signed_in(client)
    monkeypatch.setattr(
        storage_module,
        "upload_profile_photo",
        lambda *a, **kw: "https://cdn.example/u-1.jpg",
    )
    monkeypatch.setattr(
        creator_routes.profiles, "update_creator_profile", lambda *a, **kw: False
    )

    r = client.post(
        "/creator/profile/photo",
        files={"photo": ("p.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/profile?photo=save_failed"


# -----------------------------------------------------------------------------
# delete
# -----------------------------------------------------------------------------


def test_delete_clears_column_and_removes_object(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client)
    saved = {}

    def _fake_update(user_id: str, payload: dict) -> bool:
        saved.update(payload)
        saved["_user_id"] = user_id
        return True

    removed = {}
    monkeypatch.setattr(creator_routes.profiles, "update_creator_profile", _fake_update)
    monkeypatch.setattr(
        storage_module,
        "delete_profile_photo",
        lambda uid: removed.setdefault("user_id", uid),
    )

    r = client.post("/creator/profile/photo/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/profile?photo=removed"
    assert saved["profile_photo_url"] is None
    assert saved["_user_id"] == "u-1"
    assert removed["user_id"] == "u-1"


def test_delete_requires_creator_session(client: TestClient) -> None:
    r = client.post("/creator/profile/photo/delete", follow_redirects=False)
    assert r.status_code == 401


# -----------------------------------------------------------------------------
# storage service (in-process — no network)
# -----------------------------------------------------------------------------


def test_storage_normalize_strips_exif_and_resizes() -> None:
    # 1024x768 → expect 512x512 square JPEG after _normalize_to_square_jpeg.
    raw = io.BytesIO()
    Image.new("RGB", (1024, 768), color="red").save(raw, format="PNG")
    out = storage_module._normalize_to_square_jpeg(raw.getvalue())
    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "JPEG"
        assert im.size == (
            storage_module.OUTPUT_SIZE,
            storage_module.OUTPUT_SIZE,
        )


def test_storage_decode_error_on_garbage() -> None:
    with pytest.raises(storage_module.PhotoDecodeError):
        storage_module._normalize_to_square_jpeg(b"not an image at all")


def test_storage_raw_upload_cap_matches_bucket_limit() -> None:
    """The raw upload cap is pinned to the Supabase bucket's own
    file_size_limit (6 MiB — see migrations/0010_profile_photos_bucket.sql).
    If these drift apart, a borderline upload passes the app gate
    then 500s when the bucket rejects it."""
    assert storage_module.MAX_UPLOAD_BYTES == 6 * 1024 * 1024


def test_storage_pillow_decompression_bomb_cap_is_pinned() -> None:
    """Without an explicit cap Pillow only warns at ~89 MP. A 5 MB
    crafted JPEG can decompress to that and force `ImageOps.fit` to
    allocate hundreds of MB. The module sets a stricter cap at
    import time; this test pins it so a future refactor that drops
    the line gets caught."""
    from PIL import Image

    # 25 MP is well above any phone camera and well below the
    # ~89 MP default that lets bombs through silently.
    assert Image.MAX_IMAGE_PIXELS == 25_000_000


def test_storage_decompression_bomb_becomes_clean_decode_error() -> None:
    """A real bomb (image whose declared dimensions exceed the cap)
    must surface as PhotoDecodeError so the route returns a calm
    `?photo=corrupt` flash instead of bubbling a 500. Pillow only
    raises DecompressionBombError at 2x MAX_IMAGE_PIXELS (it only
    warns at 1x), so the test image is sized well above that line."""
    from PIL import Image

    # 8000x8000 = 64 MP > 50 MP (2x the 25M cap), so Pillow raises.
    raw = io.BytesIO()
    Image.new("RGB", (8000, 8000), color="white").save(raw, format="PNG")

    with pytest.raises(storage_module.PhotoDecodeError):
        storage_module._normalize_to_square_jpeg(raw.getvalue())
