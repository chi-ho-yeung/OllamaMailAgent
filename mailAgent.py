"""
================================================================================
EMAIL CLEANUP AGENT - TRIER MODE (llama.cpp decision-model backend)
================================================================================
GOALS:
1. SAFE TRIAGE: ask a local decision model (llama.cpp /v1/systemone) whether
   each email is safe to delete, in ONE request per email; Python decides.
2. PERFORMANCE METRICS: benchmark local inference speed
ENVIRONMENT:
- Runtime: Python 3.x with Gmail API & bs4
- Engine: llama-server serving a decision model (not a chat model)
================================================================================
"""

import os
import yaml
import sys
import time
import json
import re
from urllib.parse import urlparse
import base64
import email
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from email.header import decode_header
from config import EMAIL_ACCOUNT, USER_NAME, LLAMA_HOST, LLAMA_MODEL, MAX_BODY_CHARS
from system_one import decide, check_server, DecisionError
from refresh_oauth_token import get_gmail_service
from relevancy_prompt import (
    QUESTIONS,
    get_category_hint,
    build_state,
    email_signals,
    decide_action,
)
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime
from datetime import datetime

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
    print(f"[{index}/{total}] {entry['date']} {cat_str}{entry['sender'].replace('\\n', ' ').replace('\\t', ' ').strip()[:max_sender_len]} | 📝 {entry['subject'][:40]} | ⏱ {elapsed_str}")


def call_with_timeout(fn, *args, timeout=30, **kwargs):
    """Run fn(*args, **kwargs) in a thread. Raises TimeoutError or re-raises exceptions.

    The executor is shut down with wait=False: using it as a context manager
    would block on exit until the hung call finished, defeating the timeout.
    """
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            raise TimeoutError(f"API call timed out after {timeout}s")
    finally:
        executor.shutdown(wait=False)


def _decode_mime_header(raw_header):
    """Fully decode an RFC 2047 MIME-encoded header into plain text."""
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


_INVISIBLE_CHARS_RE = re.compile(
    "[\u034f\u00ad\u200b\u200c\u200d\u2060\ufeff\u180e]"
)
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_HTML_SOURCE_NOISE_RE = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)?\(https?:\/\/[^\s)]*\)\s*$"
    r"|^\s*\]\s*\(https?:\/\/[^\)\s]*\)\s*$"
    r"|^\s*\[[^\]]*\]\s*$"
    r"|^\s*https?:\/\/\S+\s*$"
)


def clean_text(text_body):
    if not text_body:
        return ""
    soup = BeautifulSoup(text_body, "html.parser")
    for tag in soup(["style", "script"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = _INVISIBLE_CHARS_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text)

    cleaned_lines = []
    prev_line = None
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if _HTML_SOURCE_NOISE_RE.match(line):
            continue
        if line == prev_line:
            continue
        cleaned_lines.append(line)
        prev_line = line

    return "\n".join(cleaned_lines)


_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)


def _url_to_placeholder(match):
    """https://www.linkedin.com/comm/x?y=z -> <linkedin.com/link> (keeps the domain, drops the noise)."""
    url = match.group(0)
    trailing = ""
    while url and url[-1] in ".,;:!?":
        trailing = url[-1] + trailing
        url = url[:-1]
    host = urlparse(url if "//" in url else "//" + url).hostname or ""
    host = host.lower().removeprefix("www.")
    return (f"<{host}/link>" if host else "<link>") + trailing


def strip_urls(text):
    """Replace URLs with <domain/link> (tracking links are noise and eat the model's context)."""
    if not text:
        return ""
    text = _URL_RE.sub(_url_to_placeholder, text)
    lines = (_MULTI_SPACE_RE.sub(" ", ln).strip() for ln in text.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def load_contacts():
    if not os.path.exists(CONTACTS_FILE):
        return {}
    try:
        with open(CONTACTS_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"  ⚠️  Could not read contacts file: {e}")
        return {}

    contacts = {}
    default_label = LABEL_NAMES["ATTENTION"]

    raw_items = data
    if isinstance(data, dict):
        if "trusted" in data:
            raw_items = data["trusted"]
        elif "contacts" in data:
            raw_items = data["contacts"]

    if isinstance(raw_items, list):
        for item in raw_items:
            if isinstance(item, str):
                addr = item.strip().lower()
                if addr:
                    contacts[addr] = default_label
            elif isinstance(item, dict):
                if "email" in item:
                    addr = str(item["email"]).strip().lower()
                    lbl = str(item.get("label", default_label)).strip() or default_label
                    if addr:
                        contacts[addr] = lbl
                else:
                    for k, v in item.items():
                        addr = str(k).strip().lower()
                        lbl = str(v).strip() if v else default_label
                        if addr:
                            contacts[addr] = lbl
    elif isinstance(raw_items, dict):
        for k, v in raw_items.items():
            addr = str(k).strip().lower()
            lbl = str(v).strip() if v else default_label
            if addr:
                contacts[addr] = lbl

    return contacts


def save_contact(name, email_addr, target_label=None, notes=""):
    email_addr = email_addr.strip().lower()
    target_label = (target_label or LABEL_NAMES["ATTENTION"]).strip()

    data = {}
    if os.path.exists(CONTACTS_FILE):
        try:
            with open(CONTACTS_FILE, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            data = {}

    if not isinstance(data, dict):
        data = {"trusted": []}

    trusted = data.setdefault("trusted", [])
    entry_data = {"email": email_addr, "label": target_label}
    if notes:
        entry_data["notes"] = notes

    if isinstance(trusted, list):
        updated = False
        for i, item in enumerate(trusted):
            if isinstance(item, str) and item.strip().lower() == email_addr:
                trusted[i] = entry_data
                updated = True
                break
            elif isinstance(item, dict):
                if item.get("email", "").strip().lower() == email_addr:
                    item["label"] = target_label
                    if notes:
                        item["notes"] = notes
                    updated = True
                    break
                elif email_addr in [k.strip().lower() for k in item.keys() if k not in ("email", "label", "notes")]:
                    trusted[i] = entry_data
                    updated = True
                    break
        if not updated:
            trusted.append(entry_data)
    elif isinstance(trusted, dict):
        trusted[email_addr] = target_label
    else:
        data["trusted"] = [entry_data]

    with open(CONTACTS_FILE, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    display = f"{name} <{email_addr}>" if name else email_addr
    print(f"  ✅ Saved contact: {display} → {target_label}")


def parse_sender(raw_from):
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


def get_gmail_link(msg_id):
    return f"https://mail.google.com/mail/u/0/#all/{msg_id}"


def list_gmail_labels(service, exclude_names=None):
    try:
        result = call_with_timeout(service.users().labels().list(userId="me").execute)
    except Exception as e:
        print(f"  ⚠️  Could not fetch labels: {e}")
        return []
    exclude_names = exclude_names or set()
    labels = [
        l for l in result.get("labels", [])
        if l.get("type") == "user" and l["name"] not in exclude_names
    ]
    labels.sort(key=lambda l: l["name"].lower())
    return [(l["id"], l["name"]) for l in labels]


def show_message_detail(service, e, label_ids, delete_ids):
    while True:
        name, addr = parse_sender(e["sender"])
        current_label_name = e.get("custom_label_name") or LABEL_NAMES.get(e["decision"], e["decision"])

        print("\n" + "=" * 60)
        print("      MESSAGE DETAIL")
        print("=" * 60)
        print(f"From          : {name} <{addr}>" if name else f"From          : {addr}")
        print(f"Subject       : {e['subject']}")
        print(f"Date          : {e['date']}")
        if e.get("category"):
            print(f"Gmail category: {e['category']}")
        print(f"Current label : {current_label_name}")
        if e.get("summary"):
            print(f"Summary       : {e['summary']}")
        if e.get("reason"):
            print(f"Reason        : {e['reason']}")
        print(f"Gmail link    : {get_gmail_link(e['id'])}")
        print("-" * 60)
        body_preview = (e.get("body_preview") or "").strip()
        if body_preview:
            print(body_preview)
        else:
            print("(No message body extracted.)")
        print("-" * 60)

        old_decision = e["decision"]
        toggle_target = "ATTENTION" if old_decision == "DELETE" else "DELETE"
        print("\nWhat would you like to do with this message?")
        print(f"  L  Correct label (flip {current_label_name} → {LABEL_NAMES[toggle_target]})")
        print("  C  Choose a different Gmail label")
        print("  B  Back to list")
        try:
            sub_choice = input("\n  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            sub_choice = "b"

        if sub_choice == "l":
            new_decision = toggle_target
            old_label_id = e.get("custom_label_id") or label_ids.get(old_decision)
            new_label_id = label_ids[new_decision]
            remove_ids = [old_label_id] if old_label_id else []
            if new_decision == "DELETE":
                remove_ids.append("INBOX")
            try:
                call_with_timeout(
                    service.users().messages().modify(
                        userId="me", id=e["id"],
                        body={"addLabelIds": [new_label_id], "removeLabelIds": remove_ids}
                    ).execute
                )
                if new_decision == "DELETE":
                    if e["id"] not in delete_ids:
                        delete_ids.append(e["id"])
                elif e["id"] in delete_ids:
                    delete_ids.remove(e["id"])
                e["decision"] = new_decision
                e["custom_label_id"] = None
                e["custom_label_name"] = None
                print(f"\n  ✅ Flipped: {current_label_name} → {LABEL_NAMES[new_decision]}\n")
            except Exception as ex:
                print(f"  ⚠️  Failed to update label: {ex}\n")

        elif sub_choice == "c":
            labels = list_gmail_labels(service, exclude_names=set(LABEL_NAMES.values()))
            print("\n  Choose a label to apply:")
            for i, (lid, lname) in enumerate(labels, start=1):
                print(f"  [{i}] {lname}")
            new_custom_idx = len(labels) + 1
            print(f"  [{new_custom_idx}] Enter a new label name")
            print("  (Enter number, or blank to cancel)")
            try:
                sel = input("\n  > ").strip()
            except (EOFError, KeyboardInterrupt):
                sel = ""
            if not sel:
                print("  Cancelled.\n")
                continue

            new_label_id = None
            new_label_name = None

            if sel == str(new_custom_idx):
                try:
                    custom_name = input("  Enter new label name: ").strip()
                    if custom_name:
                        new_label_name = custom_name
                        new_label_id = get_or_create_label(service, new_label_name)
                    else:
                        print("  Cancelled.\n")
                        continue
                except (EOFError, KeyboardInterrupt):
                    print("  Cancelled.\n")
                    continue
            else:
                try:
                    sel_idx = int(sel) - 1
                    if 0 <= sel_idx < len(labels):
                        new_label_id, new_label_name = labels[sel_idx]
                    else:
                        raise ValueError
                except ValueError:
                    print("  Invalid selection.\n")
                    continue

            old_label_id = e.get("custom_label_id") or label_ids.get(e["decision"])
            remove_ids = [old_label_id] if old_label_id else []
            remove_ids.append("INBOX")
            try:
                call_with_timeout(
                    service.users().messages().modify(
                        userId="me", id=e["id"],
                        body={"addLabelIds": [new_label_id], "removeLabelIds": remove_ids}
                    ).execute
                )
                if e["id"] in delete_ids:
                    delete_ids.remove(e["id"])
                e["custom_label_id"] = new_label_id
                e["custom_label_name"] = new_label_name
                e["decision"] = new_label_name
                print(f"\n  ✅ Moved to label: {new_label_name}\n")
            except Exception as ex:
                print(f"  ⚠️  Failed to apply label: {ex}\n")

        elif sub_choice == "b" or sub_choice == "":
            return

        else:
            print("  Unrecognised option. Please choose L, C, or B.\n")
        break


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

    # Check llama.cpp is up and serving a decision model (also warms it up)
    try:
        print("Checking decision model...")
        check_server()
        print(f"✓ Decision model ready ({LLAMA_MODEL or 'server default'} @ {LLAMA_HOST})")
    except DecisionError as e:
        print(f"⚠️  {e}")
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

    metrics = {key: 0 for key in LABEL_NAMES}
    metrics["ERROR_FALLBACK"] = 0
    ai_times = []
    delete_ids = []
    batch_senders = []
    contacts = load_contacts()

    for index, msg_ref in enumerate(messages, start=1):
        if _stop.is_set():
            print("\n⚠️  Stopped by user.")
            break

        try:
            msg_data = call_with_timeout(
                service.users().messages().get(
                    userId="me", id=msg_ref["id"], format="raw"
                ).execute
            )
            gmail_labels = msg_data.get("labelIds", [])
            raw = base64.urlsafe_b64decode(msg_data["raw"].encode("utf-8"))
            msg = email.message_from_bytes(raw)
        except (TimeoutError, Exception) as e:
            print(f"\n[{index}/{total_emails}] ⚠️  Failed to fetch: {e}")
            metrics["ERROR_FALLBACK"] += 1
            continue

        subject = _decode_mime_header(msg.get("Subject")) or "No Subject"
        sender = _decode_mime_header(msg.get("From")) or "Unknown"
        reply_to = _decode_mime_header(msg.get("Reply-To"))
        raw_date = msg.get("Date") or "Unknown"
        
        try:
            dt = parsedate_to_datetime(raw_date)
            date = dt.strftime("%m/%d/%y")
            days_old = (datetime.now(dt.tzinfo) - dt).days
        except Exception:
            date = re.sub(r'\d{2}:\d{2}:\d{2}.*', '', raw_date).strip()
            days_old = None

        gmail_category, category_hint = get_category_hint(gmail_labels)

        _, sender_addr = parse_sender(sender)
        sender_contact_label = contacts.get(sender_addr.lower())

        entry = {
            "id": msg_ref["id"],
            "sender": sender,
            "subject": str(subject)[:60],
            "date": date,
            "category": gmail_category or "",
            "decision": None,
            "elapsed": None,
            "summary": "",
            "reason": "",
            "trusted": bool(sender_contact_label),
            "body_preview": "",
            "custom_label_id": None,
            "custom_label_name": None,
        }
        batch_senders.append(entry)

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

        body = strip_urls(body)[:MAX_BODY_CHARS]  # model sees text only, no tracking links
        entry["body_preview"] = body  # detail view shows exactly what the model saw

        if sender_contact_label:
            if sender_contact_label.upper() == "ATTENTION" or sender_contact_label == LABEL_NAMES["ATTENTION"]:
                target_label_id = label_ids["ATTENTION"]
                assigned_label_name = LABEL_NAMES["ATTENTION"]
                decision = "ATTENTION"
                custom_id = None
                custom_name = None
            elif sender_contact_label.upper() == "DELETE" or sender_contact_label == LABEL_NAMES["DELETE"]:
                target_label_id = label_ids["DELETE"]
                assigned_label_name = LABEL_NAMES["DELETE"]
                decision = "DELETE"
                custom_id = None
                custom_name = None
            else:
                target_label_id = get_or_create_label(service, sender_contact_label)
                assigned_label_name = sender_contact_label
                decision = sender_contact_label
                custom_id = target_label_id
                custom_name = sender_contact_label

            entry["reason"] = f"Contact rule — auto-moved to '{assigned_label_name}', skipped LLM."
            entry["decision"] = decision
            entry["custom_label_id"] = custom_id
            entry["custom_label_name"] = custom_name
            entry["trusted"] = True

            try:
                call_with_timeout(
                    service.users().messages().modify(
                        userId="me", id=msg_ref["id"],
                        body={"addLabelIds": [target_label_id], "removeLabelIds": [] if decision == "ATTENTION" else ["INBOX"]}
                    ).execute
                )
            except (TimeoutError, Exception) as e:
                entry["reason"] += f" | ⚠️ Label failed: {e}"
                metrics["ERROR_FALLBACK"] += 1

            metrics[decision] = metrics.get(decision, 0) + 1
            if decision == "DELETE" or assigned_label_name == LABEL_NAMES["DELETE"]:
                delete_ids.append(msg_ref["id"])
            print_progress(index, total_emails, entry)
            continue

        # ── Single-stage decision-model triage ─────────────────────────────
        facts, signals = email_signals(
            sender,
            reply_to=reply_to,
            list_unsubscribe=bool(msg.get("List-Unsubscribe")),
            precedence=msg.get("Precedence") or "",
        )
        state = build_state(
            sender=sender,
            date=date,
            subject=subject,
            body=body,
            category_hint=category_hint,
            days_old=days_old,
            user_name=USER_NAME,
            reply_to=reply_to,
            facts=facts,
        )

        ai_start = time.time()
        try:
            answers = decide(state, QUESTIONS)
            decision, reason, _ = decide_action(answers, signals)
        except KeyboardInterrupt:
            raise
        except (DecisionError, ValueError) as e:
            decision = "ERROR"
            reason = f"Decision model error: {e}"
            metrics["ERROR_FALLBACK"] += 1

        elapsed = time.time() - ai_start
        ai_times.append(elapsed)
        metrics[decision] = metrics.get(decision, 0) + 1

        entry["elapsed"] = elapsed
        entry["summary"] = ""  # decision models write no prose
        entry["reason"] = reason

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

    GROUP_ICONS = {"ATTENTION": "👁️ ", "ERROR": "⚙️ ", "DELETE": "🗑️ "}

    def _get_group_order(entries):
        custom_groups = sorted(list(set(
            e["decision"] for e in entries
            if e.get("decision") and e["decision"] not in ("ATTENTION", "ERROR", "DELETE")
        )))
        return custom_groups + ["ATTENTION", "ERROR", "DELETE"]

    grouped_entries = []

    def print_detailed_results():
        nonlocal grouped_entries
        grouped_entries = []
        group_order = _get_group_order(batch_senders)
        for key in group_order:
            group_items = [e for e in batch_senders if e.get("decision") == key]
            grouped_entries.extend(group_items)

        if not grouped_entries:
            return
        print("=" * 40)
        print("      DETAILED RESULTS")
        print("=" * 40)
        current_group = None
        for i, e in enumerate(grouped_entries, start=1):
            if e["decision"] != current_group:
                current_group = e["decision"]
                header_name = e.get("custom_label_name") or LABEL_NAMES.get(current_group, current_group)
                header = f" {header_name} "
                print(f"\n{header:─^40}")
            cat_str = f"[{e['category']}] " if e.get("category") else ""
            label_display = e.get("custom_label_name") or LABEL_NAMES.get(e["decision"], e["decision"])
            icon = GROUP_ICONS.get(e["decision"], "📁 ")
            print(f"[{i}/{len(grouped_entries)}] {e['date']} {cat_str}{e['sender'].replace('\\n', ' ').replace('\\t', ' ').strip()[:60]}")
            print(f"  📝 {e['subject']}")
            elapsed_str = f"{e['elapsed']:.1f}s" if e.get("elapsed") is not None else "skip"
            print(f"  {icon}{label_display} | ⏱ {elapsed_str}")
            if e.get("summary"):
                print(f"  💬 {e['summary']}")
            if e.get("reason"):
                print(f"  💡 {e['reason']}")
            print("-" * 60)
        print()

    print_detailed_results()

    total_processed = len(batch_senders)
    avg_time = sum(ai_times) / len(ai_times) if ai_times else 0

    print("\n" + "=" * 40)
    print("      BATCH PERFORMANCE REPORT")
    print("=" * 40)
    print(f"Emails Processed : {total_processed}")
    group_order = _get_group_order(batch_senders)
    for key in group_order:
        cnt = sum(1 for e in batch_senders if e.get("decision") == key)
        if cnt > 0 or key in LABEL_NAMES:
            display_name = LABEL_NAMES.get(key, key)
            icon = GROUP_ICONS.get(key, "📁 ")
            print(f"  {icon}{display_name:<20}: {cnt}")
    if metrics.get("ERROR_FALLBACK"):
        print(f"⚠️  Errors         : {metrics['ERROR_FALLBACK']}")
    print(f"Avg Inference    : {avg_time:.2f}s  (1 decision call per email)")
    print(f"Total Time       : {sum(ai_times):.2f}s")
    print("=" * 40 + "\n")

    delete_count = len(delete_ids)

    def _prompt_add_contact(e):
        name, addr = parse_sender(e["sender"])
        display = f"{name} <{addr}>" if name else addr
        print(f"\n  Sender : {display}")

        labels = list_gmail_labels(service, exclude_names=set(LABEL_NAMES.values()))
        print("\n  Choose a target label for this contact:")
        print(f"  [1] {LABEL_NAMES['ATTENTION']} (Default)")
        print(f"  [2] {LABEL_NAMES['DELETE']}")
        offset = 2
        for idx, (lid, lname) in enumerate(labels, start=offset + 1):
            print(f"  [{idx}] {lname}")
        custom_input_idx = len(labels) + offset + 1
        print(f"  [{custom_input_idx}] Enter a new label name")
        print("  (Enter number, or blank for default 1-NeedAttention)")

        try:
            choice_lbl = input("\n  > ").strip()
        except (EOFError, KeyboardInterrupt):
            choice_lbl = ""

        target_label = LABEL_NAMES["ATTENTION"]
        if choice_lbl == "1" or choice_lbl == "":
            target_label = LABEL_NAMES["ATTENTION"]
        elif choice_lbl == "2":
            target_label = LABEL_NAMES["DELETE"]
        elif choice_lbl == str(custom_input_idx):
            try:
                custom_lbl = input("  Enter new label name: ").strip()
                if custom_lbl:
                    target_label = custom_lbl
            except (EOFError, KeyboardInterrupt):
                pass
        else:
            try:
                sel_lbl_idx = int(choice_lbl) - (offset + 1)
                if 0 <= sel_lbl_idx < len(labels):
                    target_label = labels[sel_lbl_idx][1]
                else:
                    print("  Invalid selection, using default 1-NeedAttention.")
            except ValueError:
                if choice_lbl:
                    target_label = choice_lbl

        try:
            notes = input("  Notes (optional, press Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            notes = ""

        save_contact(name, addr, target_label=target_label, notes=notes)
        contacts[addr.lower()] = target_label
        print()

    while not _stop.is_set():
        print("What would you like to do?")
        print("  R  Run another batch")
        if delete_count > 0:
            print(f"  T  Move {delete_count} marked email(s) to Trash")
        print("  A  Add a sender to contact list (A.# to specify a given message number)")
        print("  M  View message details (M.# to specify a given message number)")
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

        elif choice.startswith("a."):
            try:
                index_str = choice.split('.', 1)[1]
                sel_idx = int(index_str) - 1
                if not (0 <= sel_idx < len(grouped_entries)):
                    raise ValueError("Index out of bounds")
                _prompt_add_contact(grouped_entries[sel_idx])
            except (IndexError, ValueError):
                print("  Invalid selection format. Use 'A' to select from list or 'A.N' (e.g., A.3) for direct selection.\n")
                continue

        elif choice == "a":
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
            _prompt_add_contact(grouped_entries[sel_idx])

        elif choice.startswith("m."):
            try:
                index_str = choice.split('.', 1)[1]
                sel_idx = int(index_str) - 1
                if not (0 <= sel_idx < len(grouped_entries)):
                    raise ValueError("Index out of bounds")
            except (IndexError, ValueError):
                print("  Invalid selection format. Use 'M' to select from list or 'M.N' (e.g., M.3) for direct selection.\n")
                continue

            show_message_detail(service, grouped_entries[sel_idx], label_ids, delete_ids)
            delete_count = len(delete_ids)
            print_detailed_results()

        elif choice == "m":
            if not grouped_entries:
                print("  No messages available in this batch.\n")
                continue
            print("\n  Which message would you like to view?")
            for i, e in enumerate(grouped_entries, start=1):
                _, addr = parse_sender(e["sender"])
                icon = GROUP_ICONS.get(e["decision"], "📁 ")
                label_display = e.get("custom_label_name") or LABEL_NAMES.get(e["decision"], e["decision"])
                print(f"  [{i}/{len(grouped_entries)}] {icon}{label_display}")
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

            show_message_detail(service, grouped_entries[sel_idx], label_ids, delete_ids)
            delete_count = len(delete_ids)
            print_detailed_results()

        elif choice == "x" or choice == "":
            print("👋 Bye!")
            break

        else:
            print("  Unrecognised option. Please choose R, T, A, M, or x.\n")


if __name__ == "__main__":
    try:
        triage_and_label_emails()
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
