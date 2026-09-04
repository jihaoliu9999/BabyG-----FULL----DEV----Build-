"""Supabase Storage helpers for creator profile photos.

One canonical rendered variant per upload (512x512 JPEG, EXIF-stripped).
Uploads get a fresh object path so the database stores a new URL on every
photo change. That avoids stale CDN/browser cache issues where the user
picks a new photo but old `{user_id}.jpg` bytes keep rendering.

Writes always go through the service-role client because the bucket
intentionally has no RLS write policy: the backend validates uploads
before they reach storage. Reads are public (bucket.public = true).
"""

from __future__ import annotations

import io
import logging
from typing import Final
from urllib.parse import unquote, urlparse
from uuid import uuid4

from app.core import supabase_client

logger = logging.getLogger(__name__)

# PIL adds ~16ms to boot when imported at module load, and it's only
# ever used by _normalize_to_square_jpeg. Import it inside that function
# instead — every route boot pays the cost, but only photo-upload
# requests trigger the actual import. Also lets us defer setting
# Image.MAX_IMAGE_PIXELS (see the docstring inside the helper for
# why the cap exists).

# Pillow's decompression-bomb cap (~25 MP, well above any legitimate
# phone camera). Set on first call inside _normalize_to_square_jpeg
# since it needs the PIL module loaded. Idempotent — reassigning is
# cheap.
_MAX_IMAGE_PIXELS = 25_000_000

BUCKET: Final = "profile-photos"
# Raw upload cap, sized to match the Supabase bucket's own
# file_size_limit (6 MiB — see migrations/0010_profile_photos_bucket.sql).
# Keeping the app cap and the bucket cap in lockstep means a borderline
# upload either passes both gates or fails at the app gate with a
# clean PhotoTooLargeError, never silently 500s when the bucket
# rejects post-upload. The final stored object is always 512x512 JPEG
# (~30-80 KB) so the cap controls upload bandwidth, not storage
# footprint. The CSRF body cap in app/core/csrf.py is sized larger
# than this to cover multipart overhead.
MAX_UPLOAD_BYTES: Final = 6 * 1024 * 1024
OUTPUT_SIZE: Final = 512  # square px
OUTPUT_QUALITY: Final = 82
ALLOWED_CONTENT_TYPES: Final = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)


class PhotoTooLargeError(ValueError):
    """Raised when raw upload exceeds MAX_UPLOAD_BYTES."""


class PhotoUnsupportedTypeError(ValueError):
    """Raised when the content_type is not in ALLOWED_CONTENT_TYPES."""


class PhotoDecodeError(ValueError):
    """Raised when Pillow cannot decode the bytes as an image."""


class PhotoStorageError(RuntimeError):
    """Raised when the Supabase bucket itself rejects / errors during
    upload — distinct from decode/size/type problems that we catch
    earlier in the pipeline. The route maps this to a clean
    `?photo=storage_failed` flash instead of crashing the request."""


def upload_profile_photo(
    user_id: str, raw_bytes: bytes, content_type: str | None
) -> str:
    """Validate, resize, upload, and return the public URL.

    Each upload writes a fresh object under `{user_id}/...jpg` so the
    rendered URL changes immediately after save.
    """
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise PhotoTooLargeError(
            f"max {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB"
        )
    if (content_type or "").lower() not in ALLOWED_CONTENT_TYPES:
        raise PhotoUnsupportedTypeError(content_type or "(none)")

    jpeg_bytes = _normalize_to_square_jpeg(raw_bytes)
    object_name = f"{user_id}/{uuid4().hex}.jpg"

    bucket = supabase_client.get_service_client().storage.from_(BUCKET)
    # upsert=true → re-upload overwrites silently. file_options must be
    # all-strings; the SDK forwards them as multipart headers. Any
    # Supabase-side failure (network, RLS, bucket capacity, bad
    # credentials) is wrapped in PhotoStorageError so the route can
    # show a clean flash instead of 500'ing.
    try:
        bucket.upload(
            path=object_name,
            file=jpeg_bytes,
            file_options={
                "content-type": "image/jpeg",
                "cache-control": "3600",
                "upsert": "true",
            },
        )
    except Exception as exc:
        logger.exception("profile-photo upload failed for user_id=%s", user_id)
        raise PhotoStorageError("supabase storage upload failed") from exc
    try:
        return bucket.get_public_url(object_name)
    except Exception as exc:
        logger.exception(
            "profile-photo public-url lookup failed for user_id=%s", user_id
        )
        raise PhotoStorageError("supabase storage public-url failed") from exc


def delete_profile_photo(user_id: str, public_url: str | None = None) -> None:
    """Best-effort remove of the storage object. Idempotent.

    Failures are logged but not raised — clearing `profile_photo_url`
    in the database is the source of truth for "no photo".
    """
    bucket = supabase_client.get_service_client().storage.from_(BUCKET)
    object_names = []
    current = _object_name_from_public_url(public_url)
    if current:
        object_names.append(current)
    # Backward-compatible cleanup for older stable-path uploads.
    object_names.append(f"{user_id}.jpg")
    try:
        bucket.remove(list(dict.fromkeys(object_names)))
    except Exception:
        logger.exception("profile-photo remove failed for user_id=%s", user_id)


def _object_name_from_public_url(public_url: str | None) -> str | None:
    """Extract the Supabase object path from a stored public URL.

    Public URLs look like:
    /storage/v1/object/public/profile-photos/{object_name}
    """
    if not public_url:
        return None
    path = urlparse(public_url).path
    marker = f"/object/public/{BUCKET}/"
    if marker not in path:
        return None
    return unquote(path.split(marker, 1)[1]).strip("/") or None


def _normalize_to_square_jpeg(raw: bytes) -> bytes:
    """Decode, EXIF-orient, center-crop to a square, resize, recompress.

    Wrapping every Pillow call so we can return a single user-facing
    error from the route. EXIF orientation has to be applied before the
    crop or portrait photos come out sideways.

    PIL is imported lazily here so an idle web worker never pays the
    ~16ms Pillow import cost. First photo upload eats it, once per
    process.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")  # drop alpha, normalize to JPEG-compatible
            im = ImageOps.fit(
                im, (OUTPUT_SIZE, OUTPUT_SIZE), method=Image.Resampling.LANCZOS
            )
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=OUTPUT_QUALITY, optimize=True)
            return out.getvalue()
    # DecompressionBombError subclasses Image.DecompressionBombWarning in
    # some Pillow versions and Exception in others — catch both explicitly
    # so a malicious upload always becomes a clean PhotoDecodeError.
    except Image.DecompressionBombError as e:
        raise PhotoDecodeError(f"image too large to process: {e}") from e
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise PhotoDecodeError(str(e)) from e
