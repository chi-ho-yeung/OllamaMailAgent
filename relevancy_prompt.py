"""
================================================================================
RELEVANCY PROMPT MODULE — TWO-STAGE PIPELINE
================================================================================
Builds the two LLM prompts used to triage an email, plus the Gmail-category
hint text and deterministic script filter that feed into them.

STAGE 1 (build_stage1_prompt): cheap, narrow pass. Sorts every email into
KEEP / DISCARD / UNSURE against the objective rules (money, deadline,
notice, valid promo) or confidently-junk criteria. Most emails should
resolve here.

STAGE 2 (build_stage2_prompt): only run for emails stage 1 marked UNSURE.
This is where the soft judgment call lives (recency, addressed by name,
the PERSON rule, sales-pitch tone) — it gets the full JSON schema and
more reasoning room since only the ambiguous minority reach it. PERSON
(is this from a real individual, not an automated system?) lives here
rather than in stage 1 on purpose: sender identity can be spoofed by a
personal-looking "From" name paired with a business-looking "Reply-To",
and catching that needs the closer, cross-referenced look stage 2 gives
it — not a fast pattern match.

Splitting the work this way means each individual call asks a small model
to do less at once — narrow, low-ambiguity classification is where small
models tend to be reliable; multi-factor arbitration in a single pass is
where they aren't.
================================================================================
"""
from datetime import datetime

# Gmail category label -> (human-readable name, short triage lean).
CATEGORY_MAP = {
    "CATEGORY_PROMOTIONS": ("Promotions", "Lean DISCARD — exception: a limited-time offer, expiring deal, or discount that may be genuinely useful."),
    "CATEGORY_SOCIAL":     ("Social",     "Lean DISCARD."),
    "CATEGORY_UPDATES":    ("Updates",    "Neutral — judge case-by-case against the rules below."),
    "CATEGORY_FORUMS":     ("Forums",     "Lean DISCARD."),
    "CATEGORY_PERSONAL":   ("Personal",   "Lean KEEP."),
}

VALID_DECISIONS = ["DELETE", "ATTENTION"]        # stage 2's final call
VALID_VERDICTS = ["KEEP", "DISCARD", "UNSURE"]   # stage 1's triage call


def get_category_hint(gmail_labels):
    for label_id, (label_name, hint_text) in CATEGORY_MAP.items():
        if label_id in gmail_labels:
            return label_name, f"Gmail category: '{label_name}'. {hint_text}"
    return None, ""


# Human-readable reason text for each stage-1 matched_rule code. Generated
# in code rather than asked of the model for KEEP/DISCARD cases — these are
# meant to be the "obvious" cases, so the reason should be consistent and
# doesn't need per-email prose from a (possibly small) model.
MATCHED_RULE_REASONS = {
    "MONEY":    "Stage 1: matches the MONEY rule — a bill/payment due or account activity with a specific dollar amount.",
    "DEADLINE": "Stage 1: matches the DEADLINE/REPLY rule — requires a reply, or has an appointment/deadline that hasn't passed.",
    "NOTICE":   "Stage 1: matches the NOTICE rule — a security alert, account change, receipt, or medical/tax/legal notice.",
    "PROMO":    "Stage 1: matches the PROMOTION rule — a promotion or deal with an expiration date that hasn't passed.",
    "JUNK":     "Stage 1: generic marketing, newsletter, social/forum digest, expired promotion, or automated status update with no keep-signal.",
}


def build_stage1_prompt(sender, date, subject, body, category_hint=" ", days_old=None):
    today_now = datetime.now().strftime("%Y-%m-%d")
    age_hint = f"This email is {days_old} days old." if days_old is not None else ""
    prompt = f"""You are STAGE ONE of a two-stage email triage pipeline. Your only job is to sort this email into KEEP, DISCARD, or UNSURE. Do not agonize over borderline cases — that is what stage two is for. Be decisive on clear-cut cases, and honest about unclear ones.

{category_hint}
{age_hint}

Apply these checks:

KEEP if the email clearly matches ANY of:
  MONEY — a specific dollar amount tied to a bill (balance, minimum payment, amount due, due date) or to money that already moved (transfer, deposit, withdrawal, payment sent/received).
  DEADLINE — requires a reply, or has an appointment/deadline that has NOT yet passed (compare Message Date and any stated deadline to Current Date).
  NOTICE — a security alert, account change, receipt, or medical/tax/legal notice.
  PROMO — a promotion or deal with an expiration date that has NOT yet passed.
  Set "matched_rule" to whichever one applied.

DISCARD if the email is clearly generic marketing, a newsletter, a social/forum digest, an expired promotion, or a routine automated status update, with NONE of the signals above. Set "matched_rule" to "JUNK".

UNSURE if it doesn't cleanly fit KEEP or DISCARD — e.g. a borderline promotional email, an old but maybe-still-relevant update, an email that merely SOUNDS like it's from a real person but doesn't clearly match another KEEP rule (sender identity can be spoofed, so it needs a closer look), or anything else needing more judgment about the sender or the tone of the message. Set "matched_rule" to "NONE". When genuinely in doubt, choose UNSURE rather than guessing — a second, more careful pass will look at it.

[EMAIL CONTENT START]
From: {sender}
Message Date: {date}
Current Date (Today): {today_now}
Subject: {subject}
Body: {body.strip()}
[EMAIL CONTENT END]

IMPORTANT: Respond ONLY with a valid JSON object. Do not include any other text, markdown blocks, or commentary.
{{
  "verdict": "KEEP" or "DISCARD" or "UNSURE",
  "matched_rule": "MONEY" or "DEADLINE" or "NOTICE" or "PROMO" or "JUNK" or "NONE",
  "relevance_score": "1-5, where 5 = requires action or is critical financial/legal/medical/personal information, 3 = informational but genuinely worth knowing, 1 = no personal relevance"
}}"""
    return prompt


def build_stage2_prompt(sender, date, subject, body, category_hint=" ", user_name="", reply_to=""):
    today_now = datetime.now().strftime("%Y-%m-%d")
    name_line = (
        f'The user\'s name is "{user_name}" — check whether the email addresses '
        f"them personally (e.g. \"Hi {user_name}\") as one signal of genuine "
        f"correspondence rather than a mass sales pitch."
        if user_name else
        "The user's name is not provided, so skip that signal."
    )
    reply_to_line = (
        f'Reply-To: {reply_to} — compare this to the From address. If Reply-To '
        f"points somewhere different and looks like a business/support/sales/"
        f"no-reply alias, that undercuts the PERSON signal below — it's a sign "
        f"of a business or marketing account dressed up with a personal-looking "
        f"sender name, and should count AGAINST relevance even if the From name "
        f"looks like a real person."
        if reply_to else
        "No separate Reply-To header was present."
    )
    prompt = f"""You are STAGE TWO of a two-stage email triage pipeline. Stage one already checked the clear-cut rules (bills, deadlines, notices, valid promos, obvious junk) and could NOT confidently decide — that is why this email reached you. Decide ATTENTION (keep) or DELETE (trash) using judgment.

{category_hint}

Weigh these signals together:
- Gmail category lean given above (a starting point, not a rule).
- RECENCY: how old is this relative to Current Date? Older, stale-looking emails lean DELETE.
- {name_line}
- PERSON: Does the sender use a real personal name in the "From" field, or a generic/no-reply/automated-looking address? {reply_to_line}
- Does the body read like genuine, specific correspondence, or like a templated sales pitch / mass marketing (generic greeting, no specific details about the user, calls to "buy now" / "shop now" / "click here")?

In "reason", name which of these signals mattered most for this email — that record is meant to help the user spot patterns and turn them into new explicit stage-one rules later.

[EMAIL CONTENT START]
From: {sender}
Message Date: {date}
Current Date (Today): {today_now}
Subject: {subject}
Body: {body.strip()}
[EMAIL CONTENT END]

IMPORTANT: Respond ONLY with a valid JSON object. Do not include any other text, markdown blocks, or commentary.
{{
  "reason": "which signals mattered most and why, written in English",
  "decision": "{VALID_DECISIONS[0]}" or "{VALID_DECISIONS[1]}",
  "summary": "1 sentence summary, written in English",
  "relevance_score": "1-5, where 5 = requires action or is critical financial/legal/medical/personal information, 3 = informational but genuinely worth knowing, 1 = no personal relevance"
}}"""
    return prompt
