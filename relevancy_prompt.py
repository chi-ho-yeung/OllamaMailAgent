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

# Gmail category label -> (human-readable name, triage hint)
CATEGORY_MAP = {
    "CATEGORY_PROMOTIONS": ("Promotions", "Lean DELETE — but use ATTENTION if there is a limited-time offer, expiring deal, or discount that may be genuinely useful."),
    "CATEGORY_SOCIAL":     ("Social",     "Lean DELETE — social media notifications rarely need action."),
    "CATEGORY_UPDATES":    ("Updates",    "Neutral — could go either way. Use ATTENTION for: appointments, bill due dates, payment confirmations, security alerts, account changes, shipping with action needed, or any deadline. Use DELETE for: generic digests, read receipts, routine status updates with no action required."),
    "CATEGORY_FORUMS":     ("Forums",     "Lean DELETE — forum digests are rarely urgent."),
    "CATEGORY_PERSONAL":   ("Personal",   "Lean ATTENTION — likely from a real person or a service tied to personal commitments."),
}

# Languages the user can read without help. Anything else must be translated
# by the model into English as part of its JSON response (summary/reason are
# always written in English regardless of the email's original language).
USER_LANGUAGES = ["English", "Spanish"]

FINANCIAL_KEYWORDS = (
    "balance", "due date", "minimum payment", "amount due",
    "payment due", "past due", "autopay", "statement balance"
)

# Only DELETE and ATTENTION are valid LLM outputs; ERROR is reserved in
# mailAgent.py for processing failures (unreachable model, bad JSON, etc.)
VALID_DECISIONS = ["DELETE", "ATTENTION"]


def get_category_hint(gmail_labels):
    """
    Return (gmail_category_name, hint_text) for the first Gmail category
    label present in gmail_labels, or (None, "") if none match.
    """
    for label_id, (label_name, hint_text) in CATEGORY_MAP.items():
        if label_id in gmail_labels:
            return label_name, f"Gmail category: '{label_name}'. {hint_text}"
    return None, ""

def get_financial_hint(body):
    """
    Detect whether `body` contains a concrete financial figure (dollar amount
    + a balance/due-date keyword). Returns (has_financial_detail: bool, hint_text: str).
    """
    has_financial_detail = (
        re.search(r'\$[\d,]+\.\d{2}', body) is not None
        and any(kw in body.lower() for kw in FINANCIAL_KEYWORDS)
    )
    hint = (
        "IMPORTANT: This email contains a specific dollar amount together with "
        "a balance/due-date keyword (e.g. a statement balance, minimum payment, "
        "or payment due date). Even if the subject line sounds like a routine "
        "'statement is ready' notification, this is ACTIONABLE financial "
        "information — use ATTENTION, not DELETE."
        if has_financial_detail else ""
    )
    return has_financial_detail, hint

def build_triage_prompt(sender, date, subject, body, category_hint="", financial_hint=""):
    """
    Build the full triage prompt sent to the local LLM.

    Handles emails written in languages the user doesn't read: the model is
    instructed to make its decision from the actual content regardless of
    language, then report back in English (the user's languages are English
    and Spanish) with the detected language and an English translation of
    the subject line so the user isn't left guessing what a Chinese/French/
    etc. subject line said.
    """
    languages_str = " or ".join(USER_LANGUAGES)
    prompt = f"""You are an advanced AI email triage assistant. Analyze the email below and decide whether it needs attention or should be deleted.

Be ruthless but practical. Prioritize actionability, personal relevance, and important commitments over generic updates.

{category_hint}
{financial_hint}

Evaluate the email across three dimensions:
1. SENDER TYPE: Is it a real person, an automated system, a newsletter, a service notification, or spam?
2. URGENCY & ACTION: Does it require a reply, an action, or has a deadline? Or is it purely informational/promotional?
3. RELEVANCE: Is it directly tied to personal life, finances, health, legal matters, or active commitments?

LANGUAGE HANDLING: The user only reads {languages_str}. If the subject or body is written in any other language (for example Chinese), still base the triage decision on its actual content — never default to DELETE or ATTENTION merely because the language is unfamiliar. Detect the language the email is actually written in. Regardless of the email's original language, always write "summary" and "reason" IN ENGLISH so the user can understand them. Set "detected_language" to the name of that language (e.g. "Chinese", "English", "Spanish"). Set "translated_subject" to an English translation of the subject line if it is not already in {languages_str}; otherwise leave "translated_subject" as an empty string.

DECISION CRITERIA:
- Use "ATTENTION" for: personal emails, bills or payment due, appointments or reminders, security alerts, account changes, receipts, medical, tax, legal notices, shipping requiring action, anything with a deadline or requiring a reply. This includes "your statement/bill is now available" emails whenever the body contains a specific balance, amount due, or payment due date — the phrase "statement is ready" is just packaging; the balance and due date inside are what matter.
- Use "DELETE" for: newsletters, marketing with no expiring offer, social media digests, routine shipping notifications already delivered, automated digests with no action required, read receipts, generic status updates that contain no dollar amounts, deadlines, or account-specific figures.

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
  "sender_type": "string",
  "action_required": "string",
  "relevance_score": 1-5,
  "reason": "explanation, written in English",
  "detected_language": "language the email is written in, e.g. Chinese, English, Spanish",
  "translated_subject": "English translation of the subject if not already in English/Spanish, else empty string"
}}"""
    return prompt
