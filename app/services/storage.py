"""Supabase Storage helpers for creator profile photos.

One canonical variant per creator (512x512 JPEG, EXIF-stripped). Object
name is the user_id, so a re-upload overwrites the previous photo and
the URL stays stable. Cache-busting is the caller's job — append
`?v={updated_at}` to the rendered <img src=...>.

Writes always go through the service-role client because the bucket
intentionally has no RLS write policy: the backend validates uploads
before they reach storage. Reads are public (bucket.public = true).
"""

from __future__ import annotations

import io
import logging
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core import supabase_client

logger = logging.getLogger(__name__)

BUCKET: Final = "profile-photos"
MAX_UPLOAD_BYTES: Final = 5 * 1024 * 1024  # raw upload cap before resize
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


def upload_profile_photo(
    user_id: str, raw_bytes: bytes, content_type: str | None
) -> str:
    """Validate, resize, upload, and return the public URL.

    Re-uploads overwrite the existing object at `{user_id}.jpg`. The
    returned URL is stable across uploads; cache-busting via a query
    string is the template's responsibility.
    """
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise PhotoTooLargeError(
            f"max {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB"
        )
    if (content_type or "").lower() not in ALLOWED_CONTENT_TYPES:
        raise PhotoUnsupportedTypeError(content_type or "(none)")

    jpeg_bytes = _normalize_to_square_jpeg(raw_bytes)
    object_name = f"{user_id}.jpg"

    bucket = supabase_client.get_service_client().storage.from_(BUCKET)
    # upsert=true → re-upload overwrites silently. file_options must be
    # all-strings; the SDK forwards them as multipart headers.
    bucket.upload(
        path=object_name,
        file=jpeg_bytes,
        file_options={
            "content-type": "image/jpeg",
            "cache-control": "3600",
            "upsert": "true",
        },
    )
    return bucket.get_public_url(object_name)


def delete_profile_photo(user_id: str) -> None:
    """Best-effort remove of the storage object. Idempotent.

    Failures are logged but not raised — clearing `profile_photo_url`
    in the database is the source of truth for "no photo"; a leftover
    object will be overwritten on the next upload anyway.
    """
    bucket = supabase_client.get_service_client().storage.from_(BUCKET)
    try:
        bucket.remove([f"{user_id}.jpg"])
    except Exception:
        logger.exception("profile-photo remove failed for user_id=%s", user_id)


def _normalize_to_square_jpeg(raw: bytes) -> bytes:
    """Decode, EXIF-orient, center-crop to a square, resize, recompress.

    Wrapping every Pillow call so we can return a single user-facing
    error from the route. EXIF orientation has to be applied before the
    crop or portrait photos come out sideways.
    """
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
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise PhotoDecodeError(str(e)) from e
