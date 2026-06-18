"""Magic-link auth routes.

Flow:

  1. GET  /auth/login?role=<creator|brand|operator>
       Renders the email form. The role lives in the query so the user
       can hit /auth/login?role=creator straight from a card and we don't
       need server-side state yet.

  2. POST /auth/magic-link  (form: email, role)
       a. Validates email + role.
       b. For operator: refuses if no pre-existing public.users row with
          role='operator' exists for that email. We do NOT want the
          magic-link send itself to confirm/deny operator emails (that
          would be a username oracle), so we always render the same
          "check your email" page — but we skip the actual send for
          unauthorized operator emails.
       c. Calls supabase.auth.sign_in_with_otp(email, options={
              'email_redirect_to': PUBLIC_APP_URL + '/auth/callback',
              'data': {'requested_role': role},  # stored on user_metadata
          }).
       d. Sets a 10-minute signed cookie remembering the requested role.
          The callback also accepts the signed-up user's metadata as a
          creator/brand fallback when the email opens in another browser.
       e. Renders auth/magic_link_sent.html.

  3. GET/POST /auth/callback
       a. Accepts token_hash/type, code, or ConfirmationURL fragment tokens.
       b. Verifies/exchanges the Supabase auth params.
       c. Reads the auth.users id from the returned session.
       d. Looks up public.users; if missing, creates it using the role
          from the pending-role cookie (creator/brand only — operator must
          already exist). Creators and brands also get an empty profile row.
       e. Writes the long-lived session cookie. Redirects to the role's
          dashboard (or onboarding if profile is incomplete; we'll wire
          the onboarding redirect in a later step).

  4. POST /auth/logout
       Clears the session cookie. Redirects home.

Notes on the operator gate: returning identical UX for both authorized
and unauthorized operator emails avoids leaking which emails are
operators. We still skip the send for unauthorized ones to avoid wasting
quota and confusing admin inboxes.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from gotrue.errors import AuthApiError
from postgrest.exceptions import APIError as PostgrestAPIError

from app.config import get_settings
from app.core import supabase_client
from app.core.rate_limit import client_ip, magic_link_limiter
from app.core.security import (
    clear_pending_role,
    clear_session,
    read_pending_role,
    write_pending_role,
    write_session,
)
from app.core.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

VALID_ROLES = {"creator", "brand", "operator"}
SELF_SIGNUP_ROLES = {"creator", "brand"}      # operator is invite-only

# Supabase Auth verify_otp `type` values we accept on /auth/callback. Anything
# outside this set is rejected before we forward to Supabase — passing
# attacker-controlled values to verify_otp could surface unintended OTP types
# (recovery, invite, etc.).
ALLOWED_OTP_TYPES = frozenset({"magiclink", "email", "signup", "recovery"})

# Conservative email format check. Anything past this still goes through
# Supabase Auth, which does its own validation; we just want to block obvious
# nonsense ("@", "a@b", whitespace) before we spend a magic-link send.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@router.get("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    role: str = Query("creator"),
    hint: str = Query(""),
):
    if role not in VALID_ROLES:
        role = "creator"
    # `hint=expired` is set by the callback error CTA so the form can
    # surface a soft "your last link expired — request a new one" line
    # without dropping the user back at the harsher /auth/error page.
    hint = hint if hint in {"expired"} else ""
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {"role": role, "error": None, "email": None, "hint": hint},
    )


@router.post("/magic-link", response_class=HTMLResponse)
async def magic_link(
    request: Request, email: str = Form(...), role: str = Form(...)
):
    role = role.strip().lower()
    email = email.strip().lower()

    if role not in VALID_ROLES:
        return _login_error(request, role="creator", email=email, message="Invalid role.")
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return _login_error(request, role=role, email=email, message="Enter a valid email.")

    # Per-IP rate limit. 5 sends / 10 min. Mitigates email-bombing and
    # operator-email enumeration sweeps.
    ip = client_ip(request)
    if not magic_link_limiter.allow("magic-link", ip):
        return _login_error(
            request,
            role=role,
            email=email,
            message="Too many sign-in attempts. Wait a couple of minutes.",
        )

    settings = get_settings()
    redirect_to = settings.magic_link_callback_url

    # Operator gate: only existing operators may receive a link. We render
    # the same confirmation page either way to avoid an enumeration oracle.
    # Run the lookup unconditionally so the response time doesn't reveal
    # which emails are operators (timing-side-channel mitigation).
    is_known_operator = _operator_email_authorized(email)
    should_send = True if role != "operator" else is_known_operator

    if not should_send:
        # role=operator + unknown email: equalize timing with the
        # success path so the response time can't be used to enumerate
        # which emails are operators. Jitter to make the difference
        # less mechanical when an attacker measures across many probes.
        await asyncio.sleep(random.uniform(0.15, 0.30))

    if should_send:
        try:
            supabase_client.get_anon_client().auth.sign_in_with_otp(
                {
                    "email": email,
                    "options": {
                        "email_redirect_to": redirect_to,
                        "data": {"requested_role": role},
                    },
                }
            )
        except AuthApiError:
            # User-facing error (rate limit, malformed email Supabase
            # rejected, etc.). Client should retry.
            logger.warning(
                "auth API rejected magic-link for email_hash=%s",
                hash(email) & 0xFFFF,
            )
            return _login_error(
                request,
                role=role,
                email=email,
                message="We couldn't send your link. Try again in a minute.",
            )
        except Exception:
            # Network / infra failure. Different signal.
            logger.exception(
                "magic-link infra failure for email_hash=%s",
                hash(email) & 0xFFFF,
            )
            return _login_error(
                request,
                role=role,
                email=email,
                message="We're having trouble sending email right now. Try again shortly.",
            )

    response = templates.TemplateResponse(
        request, "auth/magic_link_sent.html", {"role": role, "email": email}
    )
    write_pending_role(response, role)
    return response


@router.api_route("/callback", methods=["GET", "POST"])
async def callback(request: Request):
    params = await _callback_params(request)
    token_hash = params.get("token_hash")
    otp_type = params.get("type")
    code = params.get("code")
    access_token = params.get("access_token")
    refresh_token = params.get("refresh_token")
    error_description = params.get("error_description")
    callback_error = params.get("callback_error")

    if error_description:
        # Supabase forwards its own error_description verbatim. Most
        # commonly this is "Email link is invalid or has expired" — surface
        # as expired so the CTA copy matches.
        kind = "expired" if "expired" in error_description.lower() else "invalid"
        return _callback_error(request, message=error_description, kind=kind)
    if callback_error:
        return _callback_error(
            request,
            message="Your sign-in link is missing required parameters.",
            kind="invalid",
        )

    if not any((token_hash, code, access_token, refresh_token)):
        return templates.TemplateResponse(request, "auth/callback_bridge.html", {})

    user = None
    if access_token or refresh_token:
        if not access_token or not refresh_token:
            return _callback_error(
                request,
                message="Your sign-in link is missing required parameters.",
                kind="invalid",
            )
        try:
            session_result = supabase_client.get_anon_client().auth.set_session(
                access_token,
                refresh_token,
            )
        except AuthApiError:
            logger.info("set_session rejected callback tokens")
            return _callback_error(
                request,
                message="Your sign-in link is invalid or has expired. Try again.",
                kind="expired",
            )
        except Exception:
            logger.exception("set_session infra failure")
            return _callback_error(
                request,
                message="We couldn't verify your link right now. Try again in a minute.",
                kind="infra",
            )
        user = _user_from_auth_response(session_result)
    elif code:
        try:
            code_result = supabase_client.get_anon_client().auth.exchange_code_for_session(
                {"auth_code": code}  # type: ignore[typeddict-item]
            )
        except AuthApiError:
            logger.info("exchange_code_for_session rejected code")
            return _callback_error(
                request,
                message="Your sign-in link is invalid or has expired. Try again.",
                kind="expired",
            )
        except Exception:
            logger.exception("exchange_code_for_session infra failure")
            return _callback_error(
                request,
                message="We couldn't verify your link right now. Try again in a minute.",
                kind="infra",
            )
        user = _user_from_auth_response(code_result)
    elif not token_hash or not otp_type:
        return _callback_error(
            request,
            message="Your sign-in link is missing required parameters.",
            kind="invalid",
        )
    if user is None and otp_type not in ALLOWED_OTP_TYPES:
        # Don't forward attacker-supplied `type` values to Supabase; refuse early.
        logger.warning("rejected callback with unknown otp type: %r", otp_type)
        return _callback_error(
            request,
            message="Your sign-in link is invalid. Please request a new one.",
            kind="invalid",
        )

    if user is None:
        # Step 1: exchange token_hash/type links for a Supabase session.
        try:
            verify_result = supabase_client.get_anon_client().auth.verify_otp(
                {"token_hash": token_hash, "type": otp_type}  # type: ignore[typeddict-item]
            )
        except AuthApiError:
            # Token expired / already used / wrong type. User-actionable.
            logger.info("verify_otp rejected token (expired or invalid)")
            return _callback_error(
                request,
                message="Your sign-in link is invalid or has expired. Try again.",
                kind="expired",
            )
        except Exception:
            # Network / infra. Surface a different message so support can tell the
            # difference from real link expiries.
            logger.exception("verify_otp infra failure")
            return _callback_error(
                request,
                message="We couldn't verify your link right now. Try again in a minute.",
                kind="infra",
            )

        user = _user_from_auth_response(verify_result)
    if user is None or not getattr(user, "id", None):
        return _callback_error(
            request, message="Sign-in failed. Please try again.", kind="invalid"
        )

    auth_user_id = str(user.id)
    auth_email = (getattr(user, "email", None) or "").lower()
    if not auth_email:
        return _callback_error(
            request,
            message="Your account is missing an email address.",
            kind="invalid",
        )

    # Step 2: figure out the babyg role. Existing user wins; otherwise prefer
    # the signed pending-role cookie. Email apps commonly open links in a
    # different browser, so the cookie can be absent on first signup. In that
    # case user_metadata is only a role hint: it is accepted exclusively for
    # server-approved self-signup roles and never for operator authorization.
    pending_role = read_pending_role(request)
    user_metadata = getattr(user, "user_metadata", None)
    metadata_role = (
        user_metadata.get("requested_role") if isinstance(user_metadata, dict) else None
    )
    if pending_role in VALID_ROLES:
        requested_role = pending_role
    elif metadata_role in SELF_SIGNUP_ROLES:
        requested_role = metadata_role
    else:
        requested_role = None

    role, _just_created = _ensure_user_row(
        auth_user_id=auth_user_id,
        email=auth_email,
        requested_role=requested_role,
    )
    if role is None:
        # Unauthorized operator (or any other rejection from _ensure_user_row).
        return _callback_error(
            request,
            message=(
                "This account isn't set up for the requested role. "
                "If you're an operator, ask an admin to invite you."
            ),
            kind="role",
        )

    # Step 3: write the session cookie and bounce to the dashboard.
    response = RedirectResponse(_dashboard_path(role), status_code=302)
    write_session(response, {"user_id": auth_user_id, "role": role})
    clear_pending_role(response)
    return response


@router.get("/code", response_class=HTMLResponse)
async def code_form(
    request: Request,
    role: str = Query("creator"),
    email: str = Query(""),
):
    """Code-entry fallback for users whose magic-link click was blocked by
    link-rewriting webmail, opened on the wrong device, or otherwise
    didn't survive the round-trip. Supabase OTP sends both a magic link
    and a 6-digit code in the same email; this form verifies the code
    directly so users have a recovery path that doesn't depend on the
    link arriving intact.
    """
    if role not in VALID_ROLES:
        role = "creator"
    email = email.strip().lower()[:254]
    return templates.TemplateResponse(
        request,
        "auth/code.html",
        {"role": role, "email": email, "error": None},
    )


@router.post("/code", response_class=HTMLResponse)
async def code_submit(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    role: str = Form(...),
):
    role = role.strip().lower()
    email = email.strip().lower()
    code = code.strip().replace(" ", "").replace("-", "")

    if role not in VALID_ROLES:
        return _code_error(request, role="creator", email=email, message="Invalid role.")
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return _code_error(request, role=role, email=email, message="Enter a valid email.")
    if not code.isdigit() or not 4 <= len(code) <= 10:
        return _code_error(
            request, role=role, email=email, message="Enter the code from your email."
        )

    # Per-IP rate limit. Reuses the magic-link bucket so an attacker who
    # spent five sends can't pivot to brute-forcing six-digit codes.
    ip = client_ip(request)
    if not magic_link_limiter.allow("code-verify", ip):
        return _code_error(
            request,
            role=role,
            email=email,
            message="Too many attempts. Wait a couple of minutes.",
        )

    try:
        verify_result = supabase_client.get_anon_client().auth.verify_otp(
            {"email": email, "token": code, "type": "email"}  # type: ignore[typeddict-item]
        )
    except AuthApiError:
        logger.info("verify_otp rejected code for email_hash=%s", hash(email) & 0xFFFF)
        return _code_error(
            request,
            role=role,
            email=email,
            message="That code is invalid or has expired. Request a new link.",
        )
    except Exception:
        logger.exception("verify_otp infra failure on code path")
        return _code_error(
            request,
            role=role,
            email=email,
            message="We couldn't verify the code right now. Try again in a minute.",
        )

    user = _user_from_auth_response(verify_result)
    if user is None or not getattr(user, "id", None):
        return _code_error(
            request, role=role, email=email, message="Sign-in failed. Please try again."
        )
    auth_user_id = str(user.id)
    auth_email = (getattr(user, "email", None) or "").lower()
    if not auth_email:
        return _code_error(
            request,
            role=role,
            email=email,
            message="Your account is missing an email address.",
        )

    pending_role = read_pending_role(request)
    requested_role = pending_role if pending_role in VALID_ROLES else role
    resolved_role, _just_created = _ensure_user_row(
        auth_user_id=auth_user_id, email=auth_email, requested_role=requested_role
    )
    if resolved_role is None:
        return _callback_error(
            request,
            message=(
                "This account isn't set up for the requested role. "
                "If you're an operator, ask an admin to invite you."
            ),
            kind="role",
        )

    response = RedirectResponse(_dashboard_path(resolved_role), status_code=302)
    write_session(response, {"user_id": auth_user_id, "role": resolved_role})
    clear_pending_role(response)
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse("/", status_code=302)
    clear_session(response)
    # Also drop the role-hint cookie so a sign-out + sign-in cycle
    # within the 10-min window doesn't resurrect the previous role
    # and land the user on the wrong onboarding flow.
    clear_pending_role(response)
    return response


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def _callback_params(request: Request) -> dict[str, str]:
    if request.method == "POST":
        form = await request.form()
        return {str(key): str(value) for key, value in form.items()}
    return {str(key): str(value) for key, value in request.query_params.items()}


def _user_from_auth_response(auth_response):
    user = getattr(auth_response, "user", None)
    if user is not None:
        return user
    session = getattr(auth_response, "session", None)
    return getattr(session, "user", None)


def _login_error(request: Request, *, role: str, email: str, message: str) -> Response:
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {"role": role, "error": message, "email": email, "hint": ""},
        status_code=400,
    )


def _code_error(request: Request, *, role: str, email: str, message: str) -> Response:
    return templates.TemplateResponse(
        request,
        "auth/code.html",
        {"role": role, "email": email, "error": message},
        status_code=400,
    )


CALLBACK_ERROR_KINDS = frozenset({"expired", "invalid", "infra", "role"})


def _callback_error(
    request: Request, *, message: str, kind: str = "invalid"
) -> Response:
    """Render auth/error.html with an error-kind hint so the template can
    pick the right title + recovery CTA.

    The CTA pre-selects the role the user requested (from the pending-role
    cookie set at /auth/magic-link time), so the "send me a new link"
    button lands on /auth/login?role=<their role> instead of the generic
    /get-started chooser — saves an extra click on the most common
    "link expired" flow.
    """
    if kind not in CALLBACK_ERROR_KINDS:
        kind = "invalid"
    role = read_pending_role(request)
    if role not in VALID_ROLES:
        role = "creator"
    return templates.TemplateResponse(
        request,
        "auth/error.html",
        {"message": message, "error_kind": kind, "role": role},
        status_code=400,
    )


def _operator_email_authorized(email: str) -> bool:
    """Return True iff a public.users row already exists with role=operator.

    The lookup runs unconditionally for every magic-link POST (regardless of
    requested role) to equalize timing — otherwise, a request with role=operator
    that doesn't issue a Supabase OTP returns measurably faster than one that
    does, leaking which emails are operators. Callers always invoke this and
    only branch on the boolean.
    """
    try:
        result = (
            supabase_client.get_service_client()
            .table("users")
            .select("id")
            .eq("email", email)
            .eq("role", "operator")
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("operator gate lookup failed")
        return False
    return bool(getattr(result, "data", None))


def _ensure_user_row(
    *, auth_user_id: str, email: str, requested_role: str | None
) -> tuple[str | None, bool]:
    """Look up public.users by id; create on first signup for creator/brand.

    Returns (role, created). If the role can't be resolved (e.g. an
    unknown user requesting operator), returns (None, False).
    """
    client = supabase_client.get_service_client()

    existing = (
        client.table("users").select("id, role").eq("id", auth_user_id).limit(1).execute()
    )
    rows = getattr(existing, "data", None) or []
    if rows:
        return rows[0]["role"], False

    # First-time signup. Creators and brands can self-sign up; operators
    # stay invite-only and must already exist in public.users.
    if requested_role not in SELF_SIGNUP_ROLES:
        # We refuse to create the row. The auth.users row will still exist
        # but without a public.users row the user has no babyg role and
        # every other route will 401/403.
        return None, False

    # Idempotent insert: two near-simultaneous callbacks for the same auth
    # user would PK-conflict on a plain insert. Upsert and re-select so the
    # second caller sees the existing row instead of the "account isn't set
    # up" error page.
    try:
        client.table("users").upsert(
            {"id": auth_user_id, "email": email, "role": requested_role},
            on_conflict="id",
            ignore_duplicates=True,
        ).execute()
        # Seed an empty profile row so onboarding has something to update.
        # Same upsert pattern — duplicate key races are silently ignored.
        if requested_role == "creator":
            client.table("creator_profiles").upsert(
                {"user_id": auth_user_id},
                on_conflict="user_id",
                ignore_duplicates=True,
            ).execute()
        elif requested_role == "brand":
            client.table("brand_profiles").upsert(
                {
                    "user_id": auth_user_id,
                    "company_name": "",
                    "contact_full_name": "",
                },
                on_conflict="user_id",
                ignore_duplicates=True,
            ).execute()
    except PostgrestAPIError:
        logger.exception("failed to provision public.users row for %s", auth_user_id)
        return None, False

    # Re-select to recover the role even if a parallel callback wrote first
    # with a different `requested_role` (shouldn't happen, but defends).
    confirm = (
        client.table("users").select("role").eq("id", auth_user_id).limit(1).execute()
    )
    rows = getattr(confirm, "data", None) or []
    if not rows:
        return None, False
    return rows[0]["role"], True


def _dashboard_path(role: str) -> str:
    return {"creator": "/creator", "brand": "/brand", "operator": "/operator"}.get(
        role, "/"
    )
