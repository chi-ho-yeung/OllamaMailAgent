"""
================================================================================
RELEVANCY PROMPT MODULE — SINGLE-STAGE DECISION-MODEL TRIAGE
================================================================================
One request to a decision model (POST /v1/systemone via the ollama package) per email. All
questions share one state and are answered independently in one pass:

  record     (noul)   bill / payment / receipt / security alert / official notice?
  bulk       (noul)   sent automatically / in bulk rather than written for the recipient?
  actionable (noul)   does the recipient need to act, pay, reply, or keep it?
  personal   (noul)   written by a real person specifically to the recipient?

Design rules for these prompts:
  * Narrow, single-concept questions. "Safe to delete" is NOT asked directly: it
    is the conjunction bulk AND NOT actionable, computed in decide_action(). One
    compound question is the multi-factor arbitration small models are bad at.
  * Anything checkable in code is done in code and handed over as a plain fact:
    email age, unsubscribe/bulk headers, no-reply senders, Reply-To mismatch.
  * The model only supplies probabilities; thresholds live in config.py.
    Anything not confidently deletable stays ATTENTION.
================================================================================
"""
import re
from datetime import datetime
from email.utils import parseaddr

from config import (
    DELETE_MIN_PROB, DELETE_MIN_PROB_BULK_HEADER, HARD_KEEP_MIN, SOFT_KEEP_MIN,
)

# Gmail category label -> human-readable name (passed to the model as a fact).
CATEGORY_MAP = {
    "CATEGORY_PROMOTIONS": "Promotions",
    "CATEGORY_SOCIAL":     "Social",
    "CATEGORY_UPDATES":    "Updates",
    "CATEGORY_FORUMS":     "Forums",
    "CATEGORY_PERSONAL":   "Personal",
}

VALID_DECISIONS = ["DELETE", "ATTENTION"]

# Four short yes/no questions, one concept each. Each runs on a small model on modest hardware,
# and every extra question adds noticeable time per email, so keep this list short.
QUESTIONS = {
    "record": {
        "type": "noul",
        "instructions": "Is this email a bill, payment, transaction, order or payment receipt, "
                        "security alert, account change, or an official medical, tax or legal notice?",
    },
    "bulk": {
        "type": "noul",
        "instructions": "Was this email sent automatically or in bulk to many people "
                        "(marketing, newsletter, digest, notification) rather than written "
                        "for this specific recipient?",
    },
    "actionable": {
        "type": "noul",
        "instructions": "Does the recipient need to do something because of this email: "
                        "pay, reply, attend, respond to a deadline that has not passed, "
                        "or keep it as a record? Compare dates with Today.",
    },
    "personal": {
        "type": "noul",
        "instructions": "Is this a message written by a real person specifically to the recipient?",
    },
}

_AUTOMATED_LOCALPART_RE = re.compile(
    r"no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer-daemon"
    r"|^(notifications?|mailer|alerts?|newsletters?|news|updates?|marketing|info|editors?|digest|bounces?)([-_.+\d]|$)",
    re.IGNORECASE,
)


def get_category_hint(gmail_labels):
    """Return (category name or None, short fact string for the state)."""
    for label_id, name in CATEGORY_MAP.items():
        if label_id in gmail_labels:
            return name, f"Gmail category: {name}"
    return None, ""


def _domain(addr):
    return addr.rsplit("@", 1)[1].lower().strip(">") if "@" in addr else ""


def email_signals(sender, reply_to="", list_unsubscribe=False, precedence=""):
    """
    Deterministic checks done in code, not by the model.
    Returns (facts, signals): facts are plain sentences added to the state;
    signals are flags used by decide_action().
    """
    _, from_addr = parseaddr(sender)
    _, reply_addr = parseaddr(reply_to or "")
    local = from_addr.split("@", 1)[0] if "@" in from_addr else ""

    mismatch = bool(reply_addr and _domain(from_addr) and _domain(reply_addr) != _domain(from_addr))
    automated = bool(_AUTOMATED_LOCALPART_RE.search(local))
    bulk_header = bool(list_unsubscribe) or precedence.strip().lower() in ("bulk", "list", "junk")

    facts = []
    if automated:
        facts.append("Sender address looks automated (no-reply/notifications style).")
    if bulk_header:
        facts.append("Message carries an unsubscribe / bulk-mail header.")
    if mismatch:
        facts.append("Reply-To is on a different domain than From.")
    return facts, {"reply_to_mismatch": mismatch, "automated": automated, "bulk_header": bulk_header}


def build_state(sender, date, subject, body, category_hint="", days_old=None,
                user_name="", reply_to="", facts=None):
    """Plain-text description of the email shared by all questions."""
    lines = []
    if user_name:
        lines.append(f"Recipient: {user_name}")
    lines.append(f"From: {sender}")
    if reply_to:
        lines.append(f"Reply-To: {reply_to}")
    lines.append(f"Message Date: {date}")
    lines.append(f"Today: {datetime.now().strftime('%Y-%m-%d')}")
    if days_old is not None:
        lines.append(f"Email age: {days_old} days old")
    if category_hint:
        lines.append(category_hint)
    lines.extend(facts or [])
    lines.append(f"Subject: {subject}")
    lines.append(f"Body:\n{body.strip()}")
    return "\n".join(lines)


def _noul(answers, key):
    try:
        return float(answers[key]["noul"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"missing/invalid '{key}' answer") from e


def decide_action(answers, signals=None):
    """
    Map the model's probabilities to (decision, reason, details).
    decision is "ATTENTION" or "DELETE"; raises ValueError on a malformed answer
    (the caller turns that into ERROR).

    The user reviews every DELETE before trashing, so this leans toward recommending delete:
      1. HARD keep-signals (record/personal >= HARD_KEEP_MIN)                 -> ATTENTION
      2. SOFT keep-signal  (actionable >= SOFT_KEEP_MIN)                      -> ATTENTION
      3. bulk >= DELETE_MIN_PROB (lower if code saw a bulk header/automated sender) -> DELETE
      4. otherwise                                                            -> ATTENTION
    """
    signals = signals or {}
    p_record = _noul(answers, "record")
    p_bulk = _noul(answers, "bulk")
    p_action = _noul(answers, "actionable")
    p_personal = _noul(answers, "personal")

    stats = (f"[record={p_record:.2f}, bulk={p_bulk:.2f}, "
             f"actionable={p_action:.2f}, personal={p_personal:.2f}]")
    details = {"p_record": p_record, "p_bulk": p_bulk,
               "p_actionable": p_action, "p_personal": p_personal}

    if p_record >= HARD_KEEP_MIN:
        return "ATTENTION", f"Looks like a bill, receipt, alert or official notice. {stats}", details
    if p_personal >= HARD_KEEP_MIN and not signals.get("reply_to_mismatch"):
        return "ATTENTION", f"Looks like genuine personal correspondence. {stats}", details
    if p_action >= SOFT_KEEP_MIN:
        return "ATTENTION", f"May need action or be worth keeping. {stats}", details

    bulk_hint = bool(signals.get("bulk_header") or signals.get("automated"))
    delete_min = DELETE_MIN_PROB_BULK_HEADER if bulk_hint else DELETE_MIN_PROB
    if p_bulk >= delete_min:
        why = " (bulk header/automated sender)" if bulk_hint else ""
        return "DELETE", f"Bulk/automated with nothing to act on{why}. {stats}", details
    return "ATTENTION", f"Not confident enough to delete (bulk needs >={delete_min:.2f}) - kept for review. {stats}", details
