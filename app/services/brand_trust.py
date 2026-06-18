"""Brand trust projection and operator check helpers.

Raw checks and operator notes stay operator-only. Creator-facing callers should
only use ``public_trust`` so the UI gets careful, non-accusatory language.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlparse

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)

VERIFICATION_STATUSES: Final[tuple[str, ...]] = (
    "unverified",
    "likely_legitimate",
    "verified",
    "needs_review",
    "high_risk",
    "blocked",
)
CHECK_RESULTS: Final[tuple[str, ...]] = ("pass", "warn", "fail", "inconclusive")
CHECK_TYPES: Final[tuple[str, ...]] = (
    "domain_match",
    "website_reachable",
    "web_presence",
    "operator_review",
    "creator_report",
    "profile_completeness",
    "email_domain",
    "suspicious_language",
)

_STATUS_COPY: Final[dict[str, dict[str, str]]] = {
    "verified": {
        "label": "verified",
        "tone": "positive",
        "summary": "reviewed by babyg.",
    },
    "likely_legitimate": {
        "label": "likely legitimate",
        "tone": "positive",
        "summary": "basic signals look consistent.",
    },
    "unverified": {
        "label": "unverified",
        "tone": "neutral",
        "summary": "ask for a business email before committing.",
    },
    "needs_review": {
        "label": "needs review",
        "tone": "caution",
        "summary": "babyg is still reviewing this brand.",
    },
    "high_risk": {
        "label": "risk signals present",
        "tone": "risk",
        "summary": "treat with caution and confirm details first.",
    },
    "blocked": {
        "label": "treat with caution",
        "tone": "risk",
        "summary": "this brand is not currently visible in discover.",
    },
}


def normalize_domain(value: str | None) -> str | None:
    """Normalize a URL, email, or bare host to a registrable-enough domain.

    This intentionally avoids a dependency on a public suffix list; for trust
    checks we only need stable, conservative matching between the website host
    and a business email domain already stored on the profile.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if "@" in raw:
        raw = raw.rsplit("@", 1)[-1]
    if "://" not in raw:
        raw = f"https://{raw}"
    host = urlparse(raw).netloc or urlparse(raw).path
    host = host.split("@")[-1].split(":")[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return None
    return host


def domain_match_status(
    *, website: str | None, email_domain: str | None
) -> dict[str, Any]:
    website_domain = normalize_domain(website)
    contact_domain = normalize_domain(email_domain)
    if not website_domain and not contact_domain:
        result = "inconclusive"
        summary = "no website or business email domain to compare."
    elif not website_domain:
        result = "inconclusive"
        summary = "business email domain exists, website is missing."
    elif not contact_domain:
        result = "inconclusive"
        summary = "website exists, business email domain is missing."
    elif (
        website_domain == contact_domain
        or contact_domain.endswith(f".{website_domain}")
        or website_domain.endswith(f".{contact_domain}")
    ):
        result = "pass"
        summary = "website and business email domain match."
    else:
        result = "warn"
        summary = "website and business email domain do not match."
    return {
        "check_type": "domain_match",
        "result_status": result,
        "website_domain": website_domain,
        "contact_email_domain": contact_domain,
        "summary": summary,
    }


def compute_trust_summary(
    brand: dict[str, Any], *, report_count: int = 0
) -> dict[str, Any]:
    status = clean_status(brand.get("verification_status"))
    if status == "unverified" and brand.get("is_verified"):
        status = "verified"
    if status == "unverified" and report_count > 0:
        status = "needs_review"

    domain_check = domain_match_status(
        website=brand.get("brand_website") or brand.get("website_domain"),
        email_domain=brand.get("contact_email_domain"),
    )
    if status == "unverified":
        complete = _profile_completeness(brand)
        if domain_check["result_status"] == "pass" and complete >= 4:
            status = "likely_legitimate"
        elif domain_check["result_status"] == "warn" or report_count >= 2:
            status = "needs_review"

    copy = _STATUS_COPY[status]
    return {
        "status": status,
        "label": copy["label"],
        "tone": copy["tone"],
        "summary": copy["summary"],
        "domain_check": domain_check,
        "report_count": max(0, int(report_count or 0)),
    }


def public_trust(brand: dict[str, Any] | None, *, report_count: int = 0) -> dict[str, Any]:
    if not brand:
        return compute_trust_summary({}, report_count=report_count)
    summary = compute_trust_summary(brand, report_count=report_count)
    return {
        "status": summary["status"],
        "label": summary["label"],
        "tone": summary["tone"],
        "summary": summary["summary"],
    }


def list_checks(brand_user_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
    try:
        result = (
            supabase_client.get_service_client()
            .table("brand_trust_checks")
            .select("*")
            .eq("brand_user_id", brand_user_id)
            .order("created_at", desc=True)
            .limit(max(1, min(limit, 100)))
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("brand trust checks list failed: %s", brand_user_id)
        return []
    return getattr(result, "data", None) or []


def record_check(
    *,
    brand_user_id: str,
    check_type: str,
    result_status: str,
    details: dict[str, Any] | None = None,
    created_by_user_id: str | None = None,
    created_by_role: str = "operator",
    confidence_score: float | None = None,
    source_url: str | None = None,
) -> bool:
    if check_type not in CHECK_TYPES or result_status not in CHECK_RESULTS:
        return False
    payload: dict[str, Any] = {
        "brand_user_id": brand_user_id,
        "check_type": check_type,
        "result_status": result_status,
        "details": details or {},
        "created_by_user_id": created_by_user_id,
        "created_by_role": created_by_role,
        "confidence_score": confidence_score,
        "source_url": source_url,
    }
    try:
        result = (
            supabase_client.get_service_client()
            .table("brand_trust_checks")
            .insert(payload)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("brand trust check insert failed: %s", brand_user_id)
        return False
    return bool(getattr(result, "data", None))


def run_domain_check(
    *, brand: dict[str, Any], operator_user_id: str | None
) -> dict[str, Any]:
    check = domain_match_status(
        website=brand.get("brand_website") or brand.get("website_domain"),
        email_domain=brand.get("contact_email_domain"),
    )
    record_check(
        brand_user_id=str(brand.get("user_id") or ""),
        check_type="domain_match",
        result_status=str(check["result_status"]),
        details={
            "summary": check["summary"],
            "website_domain": check["website_domain"],
            "contact_email_domain": check["contact_email_domain"],
        },
        created_by_user_id=operator_user_id,
        created_by_role="operator",
    )
    _update_brand_domains(
        str(brand.get("user_id") or ""),
        website_domain=check["website_domain"],
        contact_email_domain=check["contact_email_domain"],
    )
    return check


def clean_status(value: Any) -> str:
    candidate = str(value or "unverified").strip().lower()
    return candidate if candidate in VERIFICATION_STATUSES else "unverified"


def _profile_completeness(brand: dict[str, Any]) -> int:
    fields = (
        "company_name",
        "brand_website",
        "industry",
        "contact_full_name",
        "product_description",
    )
    return sum(1 for field in fields if brand.get(field))


def _update_brand_domains(
    brand_user_id: str,
    *,
    website_domain: str | None,
    contact_email_domain: str | None,
) -> None:
    if not brand_user_id:
        return
    payload = {
        "website_domain": website_domain,
        "contact_email_domain": contact_email_domain,
        "trust_updated_at": datetime.now(UTC).isoformat(),
    }
    try:
        (
            supabase_client.get_service_client()
            .table("brand_profiles")
            .update(payload)
            .eq("user_id", brand_user_id)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("brand domain cache update failed: %s", brand_user_id)
