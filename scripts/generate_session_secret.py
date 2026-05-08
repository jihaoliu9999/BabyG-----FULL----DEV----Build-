#!/usr/bin/env python3
"""Print a fresh URL-safe secret for SESSION_SECRET.

Usage:

    python scripts/generate_session_secret.py
    # → eyJ-zfV...3uM (48 random URL-safe bytes, ~64 chars)

Paste the output into your `.env` (local) or your Railway / Vercel /
host's environment variable store. Never commit it.
"""

from __future__ import annotations

import secrets


def main() -> None:
    print(secrets.token_urlsafe(48))


if __name__ == "__main__":
    main()
