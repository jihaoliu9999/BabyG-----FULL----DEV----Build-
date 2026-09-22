"""Layer 1 brief-worthiness filters — pure heuristics, no IO.

Called by sweep_gmail_briefs BEFORE any expensive work (draft
generation, action_proposal insert, nudge drop). Skipping a thread
here means it never becomes a brief card in the first place.

Contract:
  * Pure functions, no side effects, no network, no db.
  * Conservative — when in doubt, PASS (let the thread through). Real
    business emails from personal Gmail accounts still pass. Personal
    friend/family email also still passes (needs Layer 2's AI
    classifier to distinguish).
  * Only kills things a human clearly did not personally write:
    receipts, transactional email, job alerts, newsletters, and any
    variant of noreply@ / donotreply@ / notifications@ senders.

Rationale for staying conservative: the cost of a false negative
(let junk through) is one noisy brief card. The cost of a false
positive (kill a real brand email) is a lost opportunity. So we
only add rules we are certain about.
"""

from __future__ import annotations

# Local-part patterns we always treat as robotic.
_ROBOT_LOCAL_EXACT: frozenset[str] = frozenset({
    "notifications", "notification", "notify",
    "alerts", "alert",
    "updates", "update",
    "newsletter", "newsletters", "news", "digest", "digests",
    "mailer", "mailer-daemon", "bounces", "bounce",
    "postmaster", "automated", "automation", "system",
})

# Substrings we always treat as robotic, regardless of what surrounds them.
_ROBOT_LOCAL_SUBSTRINGS: tuple[str, ...] = (
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "do_not_reply",
)

# Full domains (and any subdomain of these) we always treat as junk
# for creator-brief purposes.
_JUNK_DOMAINS: frozenset[str] = frozenset({
    # Job boards
    "indeed.com", "ziprecruiter.com", "monster.com", "glassdoor.com",
    "dice.com", "simplyhired.com",
    # Newsletter / email marketing platforms sending as themselves
    "substack.com", "beehiiv.com", "mailchimp.com", "mailchimpapp.com",
    "convertkit.com", "constantcontact.com", "mailerlite.com", "kit.com",
    # Transactional infrastructure (real brand mail rarely comes from
    # the raw platform domain — brands use their own domain)
    "sendgrid.net", "mailgun.net", "postmarkapp.com", "amazonses.com",
})

# Subject substrings that mark a thread as obvious transactional / marketing.
_JUNK_SUBJECT_SUBSTRINGS: tuple[str, ...] = (
    # Receipts / orders
    "your receipt",
    "your order",
    "order confirmation",
    "order shipped",
    "your invoice",
    "invoice #",
    "payment received",
    "payment confirmation",
    # Account admin
    "verify your email",
    "verify your account",
    "confirm your email",
    "confirm your account",
    "reset your password",
    "password reset",
    "new sign-in",
    "new sign in",
    "new login",
    "new device signed",
    # Marketing digests
    "your weekly digest",
    "your daily digest",
    "your monthly digest",
    "weekly newsletter",
    "daily newsletter",
    # Job boards
    "job alert",
    "new job",
    "job matches",
    "matches for you",
    "jobs for you",
    "jobs matching",
    # Welcomes / onboarding
    "welcome to ",
    # Unsubscribe reminders
    "please unsubscribe",
)


# ---------------------------------------------------------------------------
# Business-intent detector — used to override the "block personal domains"
# rule in sweep_gmail_briefs. Personal-domain senders (gmail.com,
# outlook.com, etc.) are noisy by default (family, friends, receipts from
# personal shopping accounts), but they DO carry real business email
# too: a brand rep pinging from their iPhone gmail, a freelance PR person
# using their personal address, one creator emailing another about a
# collab. This module answers "does this look like business?" so we can
# let those through without opening the floodgates.
#
# Applied AFTER is_junk_gmail_sender: we still kill noreply/receipts even
# on business-signalled threads.
# ---------------------------------------------------------------------------


# High-signal business words / phrases — any single hit is enough to
# treat the message as business-intent. Kept intentionally tight; a
# creator-emailing-creator "hey want to make a track together" won't
# match, but "hey want to collab" will.
_BUSINESS_HIGH_SIGNAL: tuple[str, ...] = (
    # Commercial relationship terms
    "collab",             # covers collab, collabs, collaborate, collaboration, collaborating
    "partnership",
    "partnerships",
    "sponsor",            # sponsor, sponsored, sponsoring, sponsorship, sponsorships
    "campaign",           # campaign, campaigns
    "ambassador",         # ambassador, ambassadors, ambassadorship
    "influencer",         # influencer, influencers, influencer program
    "creator program",
    "creator fund",
    "affiliate program",
    "affiliate link",
    "affiliate code",
    "ugc",
    "rate card",
    "rate sheet",
    # Money / commercial intent phrases
    "paid partnership",
    "sponsored post",
    "sponsored content",
    "brand deal",
    "brand opp",          # covers brand opp, brand opportunity
    "brand collab",
    "sponsorship opp",
    "collab opp",
    "collab opportunity",
    "paid opportunity",
    "get you paid",
    "paying creators",
    "we pay",
    "compensated",
    "compensation for",
    # Gifting / PR flows
    "gifting",            # gifting, product gifting
    "product seed",       # product seed, product seeding
    "pr package",
    "pr box",
    "pr gift",
    "pr mailer",
    "press mention",
    "press inquiry",
    "press feature",
    "press kit",
    "media kit",
    # Direct outreach phrases with commercial framing
    "reached out about",
    "want to work with you",
    "would love to work with you",
    "interested in working with you",
    "featuring you",
    "feature you in",
    "book you for",
    "hire you for",
    "invite you to be",
    "in exchange for",
    "in return for",
)


def has_business_intent(subject: str, body: str = "") -> bool:
    """True if the email surface (subject + body preview) carries a
    high-confidence commercial signal.

    Used as an override for personal-domain senders: a brand rep
    emailing from `sarah@gmail.com` with subject "collab opportunity"
    should NOT be blocked just because the domain is gmail.com.

    Conservative — a single high-signal hit is enough; no combinations
    required. Keeps false positives low: "hey dad, coming to town for
    the campaign trail" would technically match "campaign" but that's
    an acceptable edge case (creator can dismiss).
    """
    text = " ".join(filter(None, [(subject or "").strip(), (body or "").strip()]))
    if not text:
        return False
    text_norm = text.casefold()
    return any(signal in text_norm for signal in _BUSINESS_HIGH_SIGNAL)


def is_junk_gmail_sender(email: str, subject: str = "") -> bool:
    """True if the email is obvious robot/marketing junk.

    Layer 1 pre-filter — catches unambiguous non-human senders and
    transactional/marketing subjects. Runs BEFORE any Claude call in
    sweep_gmail_briefs. Real business emails from personal Gmail
    accounts pass through.
    """
    if not email or "@" not in email:
        return True  # malformed → drop, nothing useful can be built from it

    local, _, domain = email.partition("@")
    local = local.strip().lower()
    domain = domain.strip().lower()
    subject_norm = (subject or "").strip().lower()

    if not local or not domain:
        return True

    # 1. Robotic local-part — exact match.
    if local in _ROBOT_LOCAL_EXACT:
        return True

    # 2. Robotic local-part — substring (catches "noreply-1234",
    #    "hello-noreply", "notify-noreply", "no-reply-us", etc.).
    for pattern in _ROBOT_LOCAL_SUBSTRINGS:
        if pattern in local:
            return True

    # 3. Junk domain — exact match or subdomain of a junk root.
    if domain in _JUNK_DOMAINS:
        return True
    if any(domain.endswith("." + junk) for junk in _JUNK_DOMAINS):
        return True

    # 4. Junk subject — anywhere in the subject line.
    for pattern in _JUNK_SUBJECT_SUBSTRINGS:
        if pattern in subject_norm:
            return True

    return False
