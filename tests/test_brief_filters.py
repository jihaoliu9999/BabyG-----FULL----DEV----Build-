"""Layer 1 junk filter for brief-worthy Gmail threads.

Locks: what MUST get killed (robotic senders, transactional/marketing
subjects, newsletter platforms, job boards) and what MUST get through
(brand outreach, freelance PR from personal Gmail, any real human).
"""

from __future__ import annotations

import pytest

from app.services.brief_filters import is_junk_gmail_sender


# ---------------------------------------------------------------------------
# 1. Robotic local-parts get killed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        # The exact incident that motivated this filter.
        "donotreply@match.indeed.com",
        # Common variants — every one is a robot sender.
        "noreply@sephora.com",
        "no-reply@nike.com",
        "no_reply@stripe.com",
        "donotreply@amazon.com",
        "do-not-reply@example.com",
        "do_not_reply@platform.com",
        "notifications@github.com",
        "notification@shopify.com",
        "notify@bank.com",
        "alerts@calendar.com",
        "alert@doorbell.com",
        "updates@blog.example.com",
        "update@service.com",
        "newsletter@substack.com",
        "newsletters@publisher.com",
        "news@company.com",
        "digest@platform.com",
        "digests@platform.com",
        "mailer@platform.com",
        "mailer-daemon@example.com",
        "bounces@example.com",
        "bounce@example.com",
        "postmaster@example.com",
        "automated@platform.com",
        "automation@platform.com",
        "system@platform.com",
        # Compound/leading variants of noreply.
        "hello-noreply@brand.com",
        "notify-noreply@brand.com",
        "noreply-1234@brand.com",
        "brand-no-reply@brand.com",
    ],
)
def test_robotic_local_parts_are_killed(email: str) -> None:
    assert is_junk_gmail_sender(email) is True, f"expected {email!r} to be junk"


# ---------------------------------------------------------------------------
# 2. Junk domains get killed regardless of local-part.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        # Job boards
        "sarah@indeed.com",
        "recruiter@ziprecruiter.com",
        "team@monster.com",
        "hi@glassdoor.com",
        # Subdomain of a junk root (this is what "donotreply@match.indeed.com"
        # matches).
        "team@match.indeed.com",
        "recruit@boards.ziprecruiter.com",
        # Newsletter platforms
        "editor@substack.com",
        "hello@beehiiv.com",
        "team@mailchimp.com",
        "hi@convertkit.com",
        "help@mailerlite.com",
        # Transactional infra
        "bounce@sendgrid.net",
        "reply@mailgun.net",
    ],
)
def test_junk_domains_are_killed(email: str) -> None:
    assert is_junk_gmail_sender(email) is True, f"expected {email!r} to be junk"


# ---------------------------------------------------------------------------
# 3. Junk subjects get killed even when the sender itself looks fine.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "Your receipt from Cheesecake Factory",
        "Your order #A12345 has shipped",
        "Order confirmation from Amazon",
        "Your invoice for July",
        "Invoice #INV-2091 from Stripe",
        "Payment received: $199.00",
        "Payment confirmation",
        "Verify your email address",
        "Verify your account",
        "Confirm your email",
        "Confirm your account with us",
        "Reset your password",
        "Password reset request",
        "New sign-in from Safari on iPhone",
        "New sign in to your Google account",
        "New login detected",
        "New device signed in",
        "Your weekly digest is ready",
        "Your daily digest — Sep 21",
        "Your monthly digest is here",
        "Weekly newsletter #12",
        "Daily newsletter — top picks",
        "Job alert: 5 new roles",
        "New job matches for you",
        "Jobs for you this week",
        "Jobs matching your search",
        "3 matches for you today",
        "Welcome to Notion!",
        "Welcome to your new subscription",
        "Please unsubscribe if you no longer want these emails",
    ],
)
def test_junk_subjects_are_killed(subject: str) -> None:
    assert is_junk_gmail_sender("sarah@brand.com", subject=subject) is True, (
        f"expected subject {subject!r} to trigger junk filter"
    )


# ---------------------------------------------------------------------------
# 4. Real business email PASSES — even from personal-domain senders.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email,subject",
    [
        # Brand outreach from a named brand address.
        ("sarah@nike.com", "Nike x You — winter campaign"),
        ("marketing@sephora.com", "Sephora holiday collab"),
        ("pr@lululemon.com", "PR gift for you"),
        # Named person at brand.
        ("mmartinez@revolve.com", "Interested in a partnership"),
        # Freelance PR / agency using personal Gmail — the exact case
        # the user flagged as MUST-KEEP.
        ("sarah.smith@gmail.com", "Partnership on our December campaign"),
        ("j.roberts@outlook.com", "Following up re: our brand deal"),
        # Real person at a domain that also has noreply (e.g., a Stripe
        # employee reaching out personally). This SHOULD pass.
        ("alex.morgan@stripe.com", "Coffee next week?"),
        # Empty subject is fine — sender pattern is what matters.
        ("bob@vans.com", ""),
        # Brand-role addresses that are clearly not robotic.
        ("collabs@brand.com", "Collab opportunity"),
        ("partnerships@brand.com", "Partnership pitch"),
        ("press@brand.com", "Press mention"),
        ("influencer@brand.com", "Ambassador program"),
    ],
)
def test_real_business_email_passes(email: str, subject: str) -> None:
    assert is_junk_gmail_sender(email, subject=subject) is False, (
        f"expected {email!r} with subject {subject!r} to pass"
    )


# ---------------------------------------------------------------------------
# 5. Malformed / edge inputs never crash — treat as junk and drop.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    ["", "not-an-email", "@brand.com", "sender@", "  ", "sender", None],
)
def test_malformed_email_is_junk(email: str | None) -> None:
    assert is_junk_gmail_sender(email or "") is True  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 6. Case insensitivity — we normalize before matching.
# ---------------------------------------------------------------------------


def test_case_insensitive_matching() -> None:
    assert is_junk_gmail_sender("NoReply@Indeed.COM") is True
    assert is_junk_gmail_sender("DONOTREPLY@MATCH.INDEED.COM") is True
    assert (
        is_junk_gmail_sender("sarah@brand.com", subject="YOUR RECEIPT FROM AMAZON")
        is True
    )
    # Real business still passes despite mixed case.
    assert (
        is_junk_gmail_sender("Sarah.Smith@Gmail.COM", subject="Partnership Pitch")
        is False
    )


# ---------------------------------------------------------------------------
# 7. The specific real-world example the user pointed at gets killed.
# ---------------------------------------------------------------------------


def test_the_indeed_job_alert_example_gets_killed() -> None:
    """Exact reproduction of the noise the user surfaced from prod
    ('draft reply to match' — donotreply@match.indeed.com about a
    Full Stack Java Developer job)."""
    assert (
        is_junk_gmail_sender(
            "donotreply@match.indeed.com",
            subject="re: Full Stack Java Developer @ Class Boxes Technologies",
        )
        is True
    )


# ---------------------------------------------------------------------------
# 8. has_business_intent — the override that lets personal-domain
#    senders through when the message is clearly commercial.
# ---------------------------------------------------------------------------

from app.services.brief_filters import has_business_intent


@pytest.mark.parametrize(
    "subject,body",
    [
        # The exact test-email the user hit today.
        ("reel brand opp", "lets get you paid to make 4 reels. please respond"),
        # Drake-on-iPhone-gmail scenario.
        ("collab?", "hey, wanna collab on a track"),
        # Brand rep pinging from personal gmail.
        ("Sephora partnership", "Would love to work with you on the fall drop."),
        ("collab opportunity", "we're building our creator program roster"),
        ("Nike x You — winter campaign", ""),
        # Freelance PR person from personal address.
        ("PR gift for you", "sending you the PR box tomorrow"),
        ("gifting round", "want to include you in our product seeding"),
        # Creator-to-creator collab pitch.
        ("brand deal on hulu", "50k budget, want to co-create"),
        # Explicit money framing in body only, generic subject.
        ("hey", "we'd love to feature you in an upcoming sponsored post"),
        # UGC / rate card requests.
        ("UGC rates?", "share your rate card please"),
        ("ambassador application", "we saw your reels"),
        # Compensation phrases.
        ("quick q", "we pay per post, wanted to see if you're interested"),
        # Press.
        ("press inquiry", "writing a story on Miami creators"),
        # In-exchange-for gifting language.
        ("free product", "in exchange for one story mention"),
    ],
)
def test_business_intent_true(subject: str, body: str) -> None:
    assert has_business_intent(subject, body) is True, (
        f"expected subject={subject!r} body={body!r} to be flagged business"
    )


@pytest.mark.parametrize(
    "subject,body",
    [
        # Dad-coming-to-town case — pure personal life.
        ("coming to town saturday", "dinner at 7?"),
        ("dinner sunday", "grandma is making pasta"),
        # Friend chatter.
        ("saw this and thought of u", "https://example.com"),
        ("happy birthday!", ""),
        ("miss youuuu", ""),
        # Restaurant reservation confirmation (personal life, not
        # commercial for the creator).
        ("Cheesecake Factory reservation Sat 8pm", "confirmed for 4 guests"),
        # Empty case.
        ("", ""),
        # Generic non-commercial words.
        ("thanks for coming last night", "was fun catching up"),
    ],
)
def test_business_intent_false(subject: str, body: str) -> None:
    assert has_business_intent(subject, body) is False, (
        f"expected subject={subject!r} body={body!r} NOT to be flagged business"
    )


def test_business_intent_case_insensitive() -> None:
    assert has_business_intent("BRAND OPP", "GET YOU PAID") is True
    assert has_business_intent("Reel Brand Opp", "Get You Paid To Make 4 Reels") is True


def test_business_intent_signal_in_body_only() -> None:
    """A generic subject shouldn't block a message whose body clearly
    signals business (real DMs often have empty subjects)."""
    assert has_business_intent("", "quick collab pitch — we saw your reels") is True
