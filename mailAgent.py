"""
================================================================================
EMAIL CLEANUP AGENT - TRIER MODE
================================================================================
GOALS:
1. SAFE TRIAGE: Use local LLM (Qwen 3.5 4B) to analyze and categorize emails
2. PERFORMANCE METRICS: Benchmark local inference speed
ENVIRONMENT:
- Runtime: Python 3.x with Gmail API & bs4
- LLM Engine: Ollama (Local Host)
- Ollama: qwen3.5:4b
"""

import os
import yaml
import sys
import time
import json
import re
import base64
import email
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from email.header import decode_header
from config import EMAIL_ACCOUNT, USER_NAME, OLLAMA_MODEL, OLLAMA_HOST, ollama_client, MODEL_CONFIGS, DEFAULT_MODEL_CONFIG
from refresh_oauth_token import get_gmail_service
from relevancy_prompt import (
    USER_LANGUAGES,
    VALID_DECISIONS,
    VALID_VERDICTS,
    MATCHED_RULE_REASONS,
    get_category_hint,
    build_stage1_prompt,
    build_stage2_prompt,
)
from bs4 import BeautifulSoup

BATCH_SIZE = 10  # Number of oldest untagged emails to process per run

CONTACTS_FILE = os.path.join(os.path.dirname(__file__), "secrets", "contacts.yml")

LABEL_NAMES = {
    "DELETE":    "1-ToDelete",
    "ATTENTION": "1-NeedAttention",
    "ERROR":     "1-ProcessError",
}

# Global stop event — set by Ctrl+C handler
_stop = threading.Event()

def _sigint_handler(sig, frame):
    print("\n\n⚠️  Ctrl+C detected — stopping after current operation...")
    _stop.set()

signal.signal(signal.SIGINT, _sigint_handler)


def print_progress(index, total, entry):
    """Compact single-line progress indicator printed once a message finishes processing."""
    cat_str = f"[{entry['category']}] " if entry.get("category") else ""
    max_sender_len = max(30, 55 - len(cat_str))
    elapsed = entry.get("elapsed")
    elapsed_str = f"{elapsed:.1f}s" if elapsed is not None else "skip"
    print(f"[{index}/{total}] {cat_str}From: {entry['sender'][:max_sender_len]} | 📅 {entry['date']} | 📝 {entry['subject'][:40]} | ⏱ {elapsed_str}")


def call_with_timeout(fn, *args, timeout=30, **kwargs):
    """Run fn(*args, **kwargs) in a thread. Raises TimeoutError or re-raises exceptions."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            raise TimeoutError(f"API call timed out after {timeout}s")


def _run_ollama_chat(prompt):
    """
    Send `prompt` to the configured Ollama model and return the raw response
    text. Shared by both triage stages so the chat_kwargs/model-options
    plumbing and the dict-vs-ChatResponse response handling only live once.
    """
    chat_kwargs = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    # Use the model's specific config, falling back to DEFAULT_MODEL_CONFIG
    # (num_ctx=8192, format=json) for any model not explicitly listed
    model_options = MODEL_CONFIGS.get(OLLAMA_MODEL, DEFAULT_MODEL_CONFIG).copy()
    if "format" in model_options:  # top-level param in ollama.chat
        chat_kwargs["format"] = model_options.pop("format")
    if "think" in model_options:   # top-level param for models that support it
        chat_kwargs["think"] = model_options.pop("think")
    if model_options:
        chat_kwargs["options"] = model_options

    response = ollama_client.chat(**chat_kwargs)
    # Handle both dict and object (ChatResponse) returns from ollama library
    if isinstance(response, dict):
        msg_obj = response.get("message", {})
        return msg_obj.get("content", "") if isinstance(msg_obj, dict) else str(msg_obj)
    elif hasattr(response, "message"):
        # ollama >= 0.2 returns a ChatResponse object: response.message.content
        msg_obj = response.message
        return msg_obj.content if hasattr(msg_obj, "content") else str(msg_obj)
    else:
        return str(response)


def _parse_json_response(response_text):
    """Strip <think>...</think> blocks / markdown fences a model might emit, then json.loads()."""
    clean = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL).strip()
    clean = re.sub(r"^```(?:json)?", "", clean).strip()
    clean = re.sub(r"```$", "", clean).strip()
    return json.loads(clean)


def _decode_mime_header(raw_header):
    """
    Fully decode an RFC 2047 MIME-encoded header (e.g. "=?UTF-8?B?...?=")
    into plain text. Used for Subject, From, and Reply-To — any of these can
    carry MIME-encoded display names when they contain non-ASCII characters
    (accents, trademark symbols, emoji, etc.).

    decode_header() can return MULTIPLE (bytes_or_str, encoding) segments for
    a single header — joining only the first segment (as earlier code did for
    Subject) silently truncates headers split across more than one encoded
    word. This joins all of them.

    Passing a raw, undecoded header straight to the LLM is worse than just a
    display bug: a model given "=?UTF-8?B?VGFyZ2V0...?=" instead of "Target
    Circle™ Card" has no readable language to identify and may report a
    language at random — which is exactly the failure mode this fixes.
    """
    if not raw_header:
        return ""
    try:
        parts = decode_header(raw_header)
        decoded = []
        for part, enc in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(enc or "utf-8", errors="ignore"))
            else:
                decoded.append(part)
        return "".join(decoded)
    except Exception:
        return raw_header


def clean_text(text_body):
    if not text_body:
        return ""
    soup = BeautifulSoup(text_body, "html.parser")
    # get_text() does NOT skip <style>/<script> content by default — without
    # this, HTML emails with large inline CSS blocks (common in bank/marketing
    # templates) dump hundreds of lines of raw CSS as "text" ahead of the
    # actual message content, burying anything meaningful past any reasonable
    # truncation length.
    for tag in soup(["style", "script"]):
        tag.decompose()
    return soup.get_text(separator="\n").strip()


def load_contacts():
    """Load trusted email addresses from YAML. Returns a set of lowercase addresses."""
    if not os.path.exists(CONTACTS_FILE):
        return set()
    with open(CONTACTS_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return set(addr.strip().lower() for addr in data.get("trusted", []))


def save_contact(name, email_addr, notes=""):
    """Add an email address to the trusted list. Skips if already present."""
    email_addr = email_addr.strip().lower()

    if os.path.exists(CONTACTS_FILE):
        with open(CONTACTS_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    trusted = data.setdefault("trusted", [])
    if email_addr in trusted:
        print(f"  ℹ️  {email_addr} is already in your trusted list.")
        return

    trusted.append(email_addr)

    with open(CONTACTS_FILE, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    label = f"{name} <{email_addr}>" if name else email_addr
    print(f"  ✅ Trusted: {label}")


def parse_sender(raw_from):
    """Extract (name, email_addr) from a raw From header like 'Name <addr@x.com>'."""
    import re as _re
    m = _re.match(r'^(.*?)\s*<([^>]+)>', raw_from.strip())
    if m:
        name = m.group(1).strip().strip('"')
        addr = m.group(2).strip()
    else:
        name = ""
        addr = raw_from.strip()
    return name, addr


def get_or_create_label(service, name):
    """Return the Gmail label ID for `name`, creating it if it doesn't exist."""
    existing = service.users().labels().list(userId="me").execute()
    for label in existing.get("labels", []):
        if label["name"] == name:
            return label["id"]
    result = service.users().labels().create(
        userId="me",
        body={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
    ).execute()
    print(f"  ✓ Created label: {name} ({result['id']})")
    return result["id"]


def fetch_untagged_emails(service, label_ids, batch_size):
    """
    Fetch the first batch_size INBOX emails that do not have any triage label.
    Uses newest-first order (Gmail default) — cheap: stops as soon as we have enough.
    """
    exclude_ids = set(label_ids.values())
    pool = []
    page_token = None

    while len(pool) < batch_size:
        if _stop.is_set():
            break
        params = {
            "userId": "me",
            "labelIds": ["INBOX"],
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        results = call_with_timeout(service.users().messages().list(**params).execute)
        candidates = results.get("messages", [])
        if not candidates:
            break

        for msg_ref in candidates:
            if _stop.is_set():
                break
            meta = call_with_timeout(
                service.users().messages().get(
                    userId="me", id=msg_ref["id"], format="metadata",
                    metadataHeaders=["Subject"]
                ).execute
            )
            applied = set(meta.get("labelIds", []))
            if not applied.intersection(exclude_ids):
                pool.append({"id": msg_ref["id"]})
                if len(pool) >= batch_size:
                    break

        page_token = results.get("nextPageToken")
        if not page_token:
            break

    return pool


def triage_and_label_emails():
    print("=" * 60 + "\n")

    # Connect to Gmail API
    try:
        service = get_gmail_service()
        print(f"✓ Gmail API connected ({EMAIL_ACCOUNT})")
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return
    except Exception as e:
        print(f"❌ Failed to connect to Gmail: {e}")
        return

    # Check Ollama & force cold start
    try:
        available = ollama_client.list()
        if not available.get("models"):
            print("⚠️  No Ollama models available")
            return

        print("Warming up model (cold start)...")
        chat_kwargs = {
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }
        model_options = MODEL_CONFIGS.get(OLLAMA_MODEL, DEFAULT_MODEL_CONFIG).copy()
        if "think" in model_options:
            chat_kwargs["think"] = model_options.pop("think")
        model_options.pop("format", None)
        if model_options:
            chat_kwargs["options"] = model_options

        ollama_client.chat(**chat_kwargs)
        print(f"✓ Model warmed up {OLLAMA_MODEL} ({OLLAMA_HOST})")
    except Exception as e:
        print(f"⚠️  Ollama unavailable or warmup failed: {e}")
        return

    # Ensure triage labels exist
    print("\nSetting up labels...")
    label_ids = {}
    for key, name in LABEL_NAMES.items():
        label_ids[key] = get_or_create_label(service, name)
        print(f"  ✓ Label ready: {name}")

    # Fetch oldest untagged emails
    try:
        messages = fetch_untagged_emails(service, label_ids, BATCH_SIZE)
    except Exception as e:
        print(f"❌ Failed to fetch emails: {e}")
        return

    if not messages:
        print("📭 Nothing to process — all inbox emails are already tagged.")
        return

    total_emails = len(messages)

    metrics = {key: 0 for key in LABEL_NAMES}  # DELETE, ATTENTION, ERROR
    metrics["ERROR_FALLBACK"] = 0
    ai_times = []
    delete_ids = []    # IDs labelled DELETE in this batch only
    batch_senders = [] # (msg_id, sender_raw, subject, decision) for each processed email
    trusted = load_contacts()


    for index, msg_ref in enumerate(messages, start=1):
        if _stop.is_set():
            print("\n⚠️  Stopped by user.")
            break

        # Fetch full message
        try:
            msg_data = call_with_timeout(
                service.users().messages().get(
                    userId="me", id=msg_ref["id"], format="raw"
                ).execute
            )
            # Capture Gmail category labels from the message metadata
            gmail_labels = msg_data.get("labelIds", [])
            raw = base64.urlsafe_b64decode(msg_data["raw"].encode("utf-8"))
            msg = email.message_from_bytes(raw)
        except (TimeoutError, Exception) as e:
            print(f"\n[{index}/{total_emails}] ⚠️  Failed to fetch: {e}")
            metrics["ERROR_FALLBACK"] += 1
            continue

        # Extract headers — decode_header() handles RFC 2047 MIME encoding
        # (e.g. "=?UTF-8?B?...?="), which Gmail/senders use for any non-ASCII
        # character in a header (accents, trademark symbols, emoji). Applied
        # to Subject, From, AND Reply-To — a raw, undecoded header passed to
        # the LLM looks like meaningless base64 noise, not real text.
        subject = _decode_mime_header(msg.get("Subject")) or "No Subject"
        sender = _decode_mime_header(msg.get("From")) or "Unknown"
        reply_to = _decode_mime_header(msg.get("Reply-To"))
        raw_date = msg.get("Date") or "Unknown"
        # Simplify date: remove time portion (e.g. Wed, 20 May 2026 21:26:42 +0000 -> Wed, 20 May 2026)
        date = re.sub(r'\d{2}:\d{2}:\d{2}.*', '', raw_date).strip()

        # Map Gmail category labels to human-readable hints (logic lives in relevancy_prompt.py)
        gmail_category, category_hint = get_category_hint(gmail_labels)

        _, sender_addr = parse_sender(sender)
        trusted_hint = (
            "IMPORTANT: This sender is in the user's trusted contact list — use ATTENTION, do not delete."
            if sender_addr.lower() in trusted else ""
        )

        # Track for end-of-batch menu and final detailed report (decision/timing filled in below)
        entry = {
            "id": msg_ref["id"],
            "sender": sender,
            "subject": str(subject)[:60],
            "date": date,
            "category": gmail_category or "",
            "decision": None,
            "relevance_score": "?",
            "elapsed": None,
            "summary": "",
            "reason": "",
            "trusted": bool(trusted_hint),
            "lang_note": "",
        }
        batch_senders.append(entry)
        # Extract body
        plain_body = ""
        html_fallback = ""
        if msg.is_multipart():
            for part in msg.walk():
                if "attachment" in str(part.get("Content-Disposition", "")).lower():
                    continue
                ct = part.get_content_type()
                try:
                    raw_body = part.get_payload(decode=True).decode(errors="ignore")
                except Exception:
                    continue
                if ct == "text/plain" and not plain_body:
                    plain_body = clean_text(raw_body)
                elif ct == "text/html" and not html_fallback:
                    html_fallback = clean_text(raw_body)
        else:
            try:
                raw_body = msg.get_payload(decode=True).decode(errors="ignore")
                plain_body = clean_text(raw_body)
            except Exception:
                plain_body = ""

        # Many transactional/bill emails (statements, invoices, receipts) ship a
        # plaintext alternative that LOOKS substantial (unsubscribe boilerplate,
        # privacy/security links, footer address) but never actually contains
        # the real numbers — those live only in the HTML version's summary
        # table. Plaintext length alone is not a reliable signal of substance,
        # so check for an actual dollar figure: if the HTML has one and the
        # plaintext doesn't, the HTML is the version worth reading.
        def _has_dollar_amount(text):
            return re.search(r'\$[\d,]+\.\d{2}', text) is not None

        MIN_PLAINTEXT_LEN = 150
        if html_fallback.strip() and _has_dollar_amount(html_fallback) and not _has_dollar_amount(plain_body):
            body = html_fallback
        elif len(plain_body.strip()) >= MIN_PLAINTEXT_LEN:
            body = plain_body
        elif html_fallback.strip():
            body = html_fallback
        else:
            body = plain_body

        body = body[:1500]

        if trusted_hint:
            entry["reason"] = "Trusted sender — skipped LLM, labelled ATTENTION directly."
            try:
                call_with_timeout(
                    service.users().messages().modify(
                        userId="me", id=msg_ref["id"],
                        body={"addLabelIds": [label_ids["ATTENTION"]], "removeLabelIds": ["INBOX"]}
                    ).execute
                )
            except (TimeoutError, Exception) as e:
                entry["reason"] += f" | ⚠️ Label failed: {e}"
            entry["decision"] = "ATTENTION"
            metrics["ATTENTION"] += 1
            print_progress(index, total_emails, entry)
            continue

        # ── Stage 1: cheap triage — sorts into KEEP / DISCARD / UNSURE ──────
        stage1_prompt = build_stage1_prompt(
            sender=sender,
            date=date,
            subject=subject,
            body=body,
            category_hint=category_hint,
        )

        summary = ""
        reason = ""
        relevance_score = "?"
        detected_language = ""
        decision = None

        ai_start = time.time()
        try:
            stage1_text = _run_ollama_chat(stage1_prompt)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            elapsed = time.time() - ai_start
            ai_times.append(elapsed)
            entry["elapsed"] = elapsed
            entry["reason"] = f"Ollama error (stage 1): {e}"
            entry["decision"] = "ERROR"
            metrics["ERROR_FALLBACK"] += 1
            try:
                call_with_timeout(
                    service.users().messages().modify(
                        userId="me", id=msg_ref["id"],
                        body={"addLabelIds": [label_ids["ERROR"]], "removeLabelIds": []}
                    ).execute
                )
            except Exception as label_err:
                entry["reason"] += f" | ⚠️ Could not apply error label: {label_err}"
            print_progress(index, total_emails, entry)
            continue

        verdict = None
        matched_rule = "NONE"
        try:
            stage1_result = _parse_json_response(stage1_text)
            verdict = stage1_result.get("verdict", "").upper()
            matched_rule = stage1_result.get("matched_rule", "NONE").upper()
            relevance_score = stage1_result.get("relevance_score", "?")
            detected_language = stage1_result.get("detected_language", "").strip()
        except Exception:
            reason = f"Could not parse stage 1 response. Raw: {stage1_text[:120]!r}"
            metrics["ERROR_FALLBACK"] += 1

        # Rule 1 (language) is ENFORCED HERE IN CODE, not left to the model's
        # own final verdict — the model is reliable at identifying what
        # language something is written in; it's much less reliable at also
        # correctly applying "therefore DELETE" once other content (a promo,
        # an appointment) is pulling it another way. So the model only
        # reports the language, and code makes the DELETE call — and skips
        # stage 2 entirely, since a foreign-language email doesn't need a
        # judgment call, it just needs deleting.
        is_foreign_language = bool(detected_language) and detected_language not in USER_LANGUAGES
        if is_foreign_language:
            entry["lang_note"] = f"🌐 Detected language: {detected_language}"

        if verdict is None:
            decision = "ERROR"
        elif is_foreign_language:
            decision = "DELETE"
            reason = f"Non-target-language email ('{detected_language}') — deleted regardless of stage 1 verdict."
        elif verdict == "KEEP":
            decision = "ATTENTION"
            reason = MATCHED_RULE_REASONS.get(matched_rule, "Stage 1 matched a keep-worthy rule.")
        elif verdict == "DISCARD":
            decision = "DELETE"
            reason = MATCHED_RULE_REASONS.get("JUNK", "Stage 1 matched generic/junk criteria.")
        elif verdict == "UNSURE":
            # ── Stage 2: only for genuinely ambiguous survivors ─────────────
            stage2_prompt = build_stage2_prompt(
                sender=sender,
                date=date,
                subject=subject,
                body=body,
                category_hint=category_hint,
                user_name=USER_NAME,
                reply_to=reply_to,
            )
            try:
                stage2_text = _run_ollama_chat(stage2_prompt)
                stage2_result = _parse_json_response(stage2_text)
                decision = stage2_result.get("decision", "").upper()
                summary = stage2_result.get("summary", "")
                reason = stage2_result.get("reason", "No reason provided.")
                relevance_score = stage2_result.get("relevance_score", relevance_score)
                if decision not in VALID_DECISIONS:
                    reason = f"Stage 2 returned unrecognised decision {decision!r}. {reason}"
                    decision = "ERROR"
                    metrics["ERROR_FALLBACK"] += 1
            except KeyboardInterrupt:
                raise
            except Exception as e:
                decision = "ERROR"
                reason = f"Stage 2 error/parse failure: {e}"
                metrics["ERROR_FALLBACK"] += 1
        else:
            decision = "ERROR"
            reason = f"Stage 1 returned unrecognised verdict: {verdict!r}"
            metrics["ERROR_FALLBACK"] += 1

        elapsed = time.time() - ai_start
        ai_times.append(elapsed)

        metrics[decision] = metrics.get(decision, 0) + 1

        entry["elapsed"] = elapsed
        entry["relevance_score"] = relevance_score
        entry["summary"] = summary
        entry["reason"] = reason

        # Apply label — DELETE removes from INBOX (archived), ATTENTION/ERROR keep it visible
        labels_to_remove = ["INBOX"] if decision == "DELETE" else []
        try:
            call_with_timeout(
                service.users().messages().modify(
                    userId="me", id=msg_ref["id"],
                    body={"addLabelIds": [label_ids[decision]], "removeLabelIds": labels_to_remove}
                ).execute
            )
            entry["decision"] = decision
            if decision == "DELETE":
                delete_ids.append(msg_ref["id"])
        except (TimeoutError, Exception) as e:
            entry["decision"] = decision
            entry["reason"] += f" | ⚠️ Label failed: {e}"
            metrics["ERROR_FALLBACK"] += 1

        print_progress(index, total_emails, entry)


    # ── Summary report ────────────────────────────────────────────────────────
    total_processed = sum(metrics[k] for k in LABEL_NAMES)
    avg_ai_time = sum(ai_times) / len(ai_times) if ai_times else 0

    print("\n" + "=" * 40)
    print("      BATCH PERFORMANCE REPORT")
    print("=" * 40)
    print(f"Emails Processed : {total_processed}")
    icons = {"DELETE": "🗑️ ", "ATTENTION": "👁️ ", "ERROR": "⚙️ "}
    for key, name in LABEL_NAMES.items():
        print(f"  {icons.get(key, '  ')}{name:<20}: {metrics[key]}")
    if metrics["ERROR_FALLBACK"]:
        print(f"⚠️  Errors         : {metrics['ERROR_FALLBACK']}")
    print(f"Avg Inference    : {avg_ai_time:.2f}s")
    print(f"Total Time       : {sum(ai_times):.2f}s")
    print("=" * 40 + "\n")

    # ── Detailed results ──────────────────────────────────────────────────────
    # Grouped NeedAttention → ProcessError → ToDelete (not processing order),
    # sorted within each group by relevance score (highest first; unscored —
    # e.g. trusted-sender skips — sort last). Message numbers below reflect
    # this displayed order, not the order emails were originally processed.
    GROUP_ORDER = ["ATTENTION", "ERROR", "DELETE"]
    GROUP_ICONS = {"ATTENTION": "👁️ ", "ERROR": "⚙️ ", "DELETE": "🗑️ "}

    def _relevance_sort_key(e):
        try:
            return -int(e.get("relevance_score", "?"))
        except (ValueError, TypeError):
            return 1  # unscored entries sort last within their group

    grouped_entries = []
    for key in GROUP_ORDER:
        group_items = [e for e in batch_senders if e["decision"] == key]
        group_items.sort(key=_relevance_sort_key)
        grouped_entries.extend(group_items)

    if grouped_entries:
        print("=" * 40)
        print("      DETAILED RESULTS")
        print("=" * 40)
        current_group = None
        for i, e in enumerate(grouped_entries, start=1):
            if e["decision"] != current_group:
                current_group = e["decision"]
                header = f" {LABEL_NAMES[current_group]} "
                print(f"\n{header:─^40}")
            cat_str = f"[{e['category']}] " if e.get("category") else ""
            print(f"[{i}/{len(grouped_entries)}] {cat_str}From: {e['sender'][:60]}")
            print(f"  📅 {e['date']} | 📝 {e['subject']}")
            elapsed_str = f"{e['elapsed']:.1f}s" if e.get("elapsed") is not None else "skip"
            print(f"  {GROUP_ICONS[e['decision']]}{LABEL_NAMES[e['decision']]} | ⏱ {elapsed_str} | 📊 Rel: {e.get('relevance_score', '?')}/5")
            if e.get("lang_note"):
                print(f"  {e['lang_note']}")
            if e.get("summary"):
                print(f"  💬 {e['summary']}")
            if e.get("reason"):
                print(f"  💡 {e['reason']}")
            print("-" * 60)
        print()

    # ── Post-batch menu ────────────────────────────────────────────────────────
    delete_count = metrics.get("DELETE", 0)

    while not _stop.is_set():
        print("What would you like to do?")
        print("  R  Run another batch")
        if delete_count > 0:
            print(f"  T  Move {delete_count} marked email(s) to Trash")
        print("  A  Add a sender to contact list (A.# to specify a given message number)")
        print("  L  Correct a label (L.# to specify a given message number)")
        print("  x  Exit")
        try:
            choice = input("\n> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == "r":
            print()
            triage_and_label_emails()
            return

        elif choice == "t":
            if delete_count == 0:
                print("  No emails were marked for deletion in this batch.\n")
                continue
            try:
                confirm = input(f"  ⚠️  Move {len(delete_ids)} email(s) to Trash? This cannot be undone easily. [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = ""
            if confirm == "y":
                print(f"\n🗑️  Moving {len(delete_ids)} email(s) to Trash...")
                deleted = 0
                errors = 0
                for msg_id in delete_ids:
                    if _stop.is_set():
                        break
                    try:
                        call_with_timeout(
                            service.users().messages().trash(
                                userId="me", id=msg_id
                            ).execute
                        )
                        deleted += 1
                        print(f"  🗑️  Trashed {deleted}/{len(delete_ids)}", end="\r")
                    except Exception as e:
                        print(f"\n  ⚠️  Could not trash {msg_id}: {e}")
                        errors += 1
                print()
                print(f"\n✅ Done — {deleted} email(s) moved to Trash.")
                if errors:
                    print(f"⚠️  {errors} email(s) could not be trashed.\n")
                delete_ids.clear()
                delete_count = 0
            else:
                print("  Skipped — emails remain labelled but not trashed.\n")

        # Handle 'a' for Add sender
        if choice.startswith("a."):
            try:
                # Split 'a.N' and get N, then convert to 0-based index
                index_str = choice.split('.', 1)[1]
                sel_idx = int(index_str) - 1 # User enters 1-based index
                if not (0 <= sel_idx < len(grouped_entries)):
                    raise ValueError("Index out of bounds")
                
                # Perform the action directly
                e = grouped_entries[sel_idx]
                name, addr = parse_sender(e["sender"])
                print(f"\n  Sender : {name} <{addr}>")
                # For adding contact, we still need notes, so prompt for it.
                try:
                    notes = input("  Notes (optional, press Enter to skip): ").strip()
                except (EOFError, KeyboardInterrupt):
                    notes = ""
                save_contact(name, addr, notes)
                print()

            except (IndexError, ValueError):
                print("  Invalid selection format. Use 'A' to select from list or 'A.N' (e.g., A.3) for direct selection.\n")
                continue

        elif choice == "a": # If it was just 'a', proceed with interactive selection
            if not grouped_entries:
                print("  No senders available.\n")
                continue
            print("\n  Which email's sender would you like to add?")
            for i, e in enumerate(grouped_entries, start=1):
                name, addr = parse_sender(e["sender"])
                display = f"{name} <{addr}>" if name else addr
                print(f"  [{i}/{len(grouped_entries)}] {display}")
                print(f"        Subject: {e['subject']}")
            print("  (Enter number, or blank to cancel)")
            try:
                sel = input("\n  > ").strip()
            except (EOFError, KeyboardInterrupt):
                sel = ""
            if not sel:
                print("  Cancelled.\n")
                continue
            try:
                sel_idx = int(sel) - 1
                if not (0 <= sel_idx < len(grouped_entries)):
                    raise ValueError
            except ValueError:
                print("  Invalid selection.\n")
                continue
            e = grouped_entries[sel_idx]
            name, addr = parse_sender(e["sender"])
            print(f"\n  Sender : {name} <{addr}>")
            try:
                notes = input("  Notes (optional, press Enter to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                notes = ""
            save_contact(name, addr, notes)
            print()

        # Handle 'l' for Correct label
        elif choice.startswith("l."):
            try:
                index_str = choice.split('.', 1)[1]
                sel_idx = int(index_str) - 1
                if not (0 <= sel_idx < len(grouped_entries)):
                    raise ValueError("Index out of bounds")
                
                e = grouped_entries[sel_idx]
                old_decision = e["decision"]
                new_decision = "ATTENTION" if old_decision == "DELETE" else "DELETE"
                old_label_id = label_ids[old_decision]
                new_label_id = label_ids[new_decision]

                call_with_timeout(
                    service.users().messages().modify(
                        userId="me", id=e["id"],
                        body={"addLabelIds": [new_label_id], "removeLabelIds": [old_label_id]}
                    ).execute
                )
                if new_decision == "DELETE":
                    delete_ids.append(e["id"])
                    delete_count += 1
                elif e["id"] in delete_ids:
                    delete_ids.remove(e["id"])
                    delete_count -= 1

                print(f"\n  ✅ Flipped: {LABEL_NAMES[old_decision]} → {LABEL_NAMES[new_decision]}")
                print(f"  📝 Logged as correction for future learning.\n")
                e["decision"] = new_decision

            except (IndexError, ValueError):
                print("  Invalid selection format. Use 'L' to select from list or 'L.N' (e.g., L.3) for direct selection.\n")
                continue

        elif choice == "l": # If it was just 'l', proceed with interactive selection
            if not grouped_entries:
                print("  No labelled emails in this batch.\n")
                continue
            print("\n  Which email's label would you like to correct?")
            for i, e in enumerate(grouped_entries, start=1):
                _, addr = parse_sender(e["sender"])
                icon = "🗑️ " if e["decision"] == "DELETE" else ("👁️ " if e["decision"] == "ATTENTION" else "⚙️ ")
                print(f"  [{i}/{len(grouped_entries)}] {icon} {LABEL_NAMES[e['decision']]}")
                print(f"        From   : {addr}")
                print(f"        Subject: {e['subject']}")
            print("  (Enter number, or blank to cancel)")
            try:
                sel = input("\n  > ").strip()
            except (EOFError, KeyboardInterrupt):
                sel = ""
            if not sel:
                print("  Cancelled.\n")
                continue
            try:
                sel_idx = int(sel) - 1
                if not (0 <= sel_idx < len(grouped_entries)):
                    raise ValueError
            except ValueError:
                print("  Invalid selection.\n")
                continue

            e = grouped_entries[sel_idx]
            old_decision = e["decision"]
            new_decision = "ATTENTION" if old_decision == "DELETE" else "DELETE"
            old_label_id = label_ids[old_decision]
            new_label_id = label_ids[new_decision]

            try:
                call_with_timeout(
                    service.users().messages().modify(
                        userId="me", id=e["id"],
                        body={"addLabelIds": [new_label_id], "removeLabelIds": [old_label_id]}
                    ).execute
                )
                if new_decision == "DELETE":
                    delete_ids.append(e["id"])
                    delete_count += 1
                elif e["id"] in delete_ids:
                    delete_ids.remove(e["id"])
                    delete_count -= 1

                print(f"\n  ✅ Flipped: {LABEL_NAMES[old_decision]} → {LABEL_NAMES[new_decision]}")
                print(f"  📝 Logged as correction for future learning.\n")
                e["decision"] = new_decision
            except Exception as ex:
                print(f"  ⚠️  Failed to update label: {ex}\n")

        elif choice == "x" or choice == "":
            print("👋 Bye!")
            break

        else:
            print("  Unrecognised option. Please choose R, T, A, L, or x.\n")


if __name__ == "__main__":
    try:
        triage_and_label_emails()
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
