"""
================================================================================
RELEVANCY PROMPT MODULE
================================================================================
Builds the LLM prompt used to triage an email as DELETE or ATTENTION, plus the
small pieces of "hint" text (Gmail category, financial-detail detection) that
feed into it.

Kept separate from mailAgent.py so the triage wording/logic can be tuned,
tested, or swapped independently of the Gmail/Ollama plumbing.
================================================================================
"""
import re

# Gmail category label -> (human-readable name, short triage lean).
# Kept intentionally brief: the full ATTENTION/DELETE criteria live once, in
# DECISION CRITERIA below, so these hints only need to say which way a
# category leans by default (plus any category-specific exception).
CATEGORY_MAP = {
    "CATEGORY_PROMOTIONS": ("Promotions", "Lean DELETE — exception: a limited-time offer, expiring deal, or discount that may be genuinely useful."),
    "CATEGORY_SOCIAL":     ("Social",     "Lean DELETE."),
    "CATEGORY_UPDATES":    ("Updates",    "Neutral — judge case-by-case against DECISION CRITERIA below."),
    "CATEGORY_FORUMS":     ("Forums",     "Lean DELETE."),
    "CATEGORY_PERSONAL":   ("Personal",   "Lean ATTENTION."),
}

USER_LANGUAGES = ["English", "Spanish"]

# Keywords indicating a bill/obligation (something owed, with a due date).
BILL_KEYWORDS = (
    "balance", "due date", "minimum payment", "amount due",
    "payment due", "past due", "autopay", "statement balance",
)

# Keywords indicating account activity that already happened (money moved).
# Not actionable, but still important — see get_financial_hint().
ACCOUNT_ACTIVITY_KEYWORDS = (
    "transfer", "transferred", "deposit", "deposited", "withdrawal",
    "withdrew", "debited", "credited", "you sent", "sent you",
    "payment received", "payment sent", "wire transfer", "direct deposit",
    "zelle", "venmo",
)

VALID_DECISIONS = ["DELETE", "ATTENTION"]

# Matches "$500", "$1,200", "$500.00" — decimals optional so plain-dollar
# transfer amounts (no cents) still count as a "specific dollar amount".
_DOLLAR_AMOUNT_RE = re.compile(r'\$[\d,]+(?:\.\d{2})?')


def get_category_hint(gmail_labels):
    for label_id, (label_name, hint_text) in CATEGORY_MAP.items():
        if label_id in gmail_labels:
            return label_name, f"Gmail category: '{label_name}'. {hint_text}"
    return None, ""


def get_financial_hint(body):
    """
    Detects a specific dollar amount paired with either bill/obligation
    language or account-activity language, and returns a hint telling the
    model this is ATTENTION-worthy — regardless of whether it requires
    action. See DECISION CRITERIA in build_triage_prompt() for the policy
    this hint is reinforcing.
    """
    has_amount = _DOLLAR_AMOUNT_RE.search(body) is not None
    body_lower = body.lower()
    is_bill = has_amount and any(kw in body_lower for kw in BILL_KEYWORDS)
    is_activity = has_amount and any(kw in body_lower for kw in ACCOUNT_ACTIVITY_KEYWORDS)

    if is_bill:
        hint = (
            "IMPORTANT: This email pairs a specific dollar amount with bill/"
            "payment language (e.g. a balance, minimum payment, or due date). "
            "Use ATTENTION, not DELETE — this holds even if the subject line "
            "sounds like a routine 'statement is ready' notification."
        )
    elif is_activity:
        hint = (
            "IMPORTANT: This email reports a specific dollar amount actually "
            "moving in one of the user's accounts (a transfer, deposit, "
            "withdrawal, or similar). Use ATTENTION, not DELETE, even though "
            "no action is required — a record of money moving in a real "
            "account is inherently important information, not routine noise."
        )
    else:
        hint = ""

    return (is_bill or is_activity), hint


def build_triage_prompt(sender, date, subject, body, category_hint="", financial_hint=""):
    languages_str = " or ".join(USER_LANGUAGES)
    prompt = f"""You are an advanced AI email triage assistant. Analyze the email below and decide whether it needs attention or should be deleted.

Prioritize actionability, personal relevance, and important account/financial/legal/health information over generic marketing or automated noise.

{category_hint}
{financial_hint}

Evaluate the email across three dimensions:
1. SENDER TYPE: A real person, an automated system, a newsletter, a service notification, or spam?
2. URGENCY & ACTION: Does it require a reply or action, or have a deadline? Or is it purely informational/promotional?
3. RELEVANCE: Is it tied to personal life, finances, health, legal matters, or active commitments — even if no action is needed?

LANGUAGE HANDLING: The user only reads {languages_str}. Base the triage decision on the email's actual content regardless of language — never default to DELETE or ATTENTION merely because the language is unfamiliar. Always write "summary" and "reason" IN ENGLISH. Set "detected_language" to the language the email is written in. Set "translated_subject" to an English translation of the subject if it is not already in {languages_str}, otherwise leave it as an empty string.

DECISION CRITERIA:
- Use "ATTENTION" for: personal emails, bills or payments due, appointments or reminders, security alerts, account changes, receipts, medical/tax/legal notices, shipping requiring action, anything with a deadline or requiring a reply — AND, importantly, any email reporting a specific dollar amount tied to a real account event (a transfer, deposit, withdrawal, or payment made/received), even when nothing needs to be done. Information about money that actually moved in an account is ATTENTION-worthy purely because it happened, not because it's actionable.
- Use "DELETE" for: newsletters, marketing with no expiring offer, social media digests, routine shipping notifications already delivered, automated digests with no action required, read receipts, and generic status updates that contain no dollar amounts, deadlines, or account-specific figures.

PRIORITY WHEN SIGNALS CONFLICT: If the Gmail category hint above suggests one lean but the email's actual content matches an ATTENTION case in DECISION CRITERIA (especially a financial-detail hint above), the content-based DECISION CRITERIA always wins — category hints are defaults, not overrides.

[EMAIL CONTENT START]
From: {sender}
Date: {date}
Subject: {subject}
Body: {body.strip()}
[EMAIL CONTENT END]

IMPORTANT: Respond ONLY with a valid JSON object. Do not include any other text, markdown blocks, or commentary.
{{
  "decision": "{VALID_DECISIONS[0]}" or "{VALID_DECISIONS[1]}",
  "summary": "1 sentence summary, written in English",
  "sender_type": "short label, e.g. person / automated service / newsletter / financial institution / spam",
  "action_required": "short description of what action is needed, or empty string if none",
  "relevance_score": "1-5, where 5 = requires action or is critical financial/legal/medical/personal information, 3 = informational but genuinely worth knowing (e.g. a completed transaction), 1 = no personal relevance, safe to ignore",
  "reason": "explanation, written in English",
  "detected_language": "language the email is written in, e.g. Chinese, English, Spanish",
  "translated_subject": "English translation of the subject if not already in English/Spanish, else empty string"
}}"""
    return prompt
