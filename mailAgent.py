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
from config import EMAIL_ACCOUNT, OLLAMA_MODEL, OLLAMA_HOST, ollama_client, MODEL_CONFIGS
from refresh_oauth_token import get_gmail_service
from bs4 import BeautifulSoup

BATCH_SIZE = 5  # Number of oldest untagged emails to process per run

LABEL_NAMES = {
    "DELETE":    "1-ToDelete",
    "ATTENTION": "1-NeedAttention",
}

# Global stop event — set by Ctrl+C handler
_stop = threading.Event()

def _sigint_handler(sig, frame):
    print("\n\n⚠️  Ctrl+C detected — stopping after current operation...")
    _stop.set()

signal.signal(signal.SIGINT, _sigint_handler)


def call_with_timeout(fn, *args, timeout=30, **kwargs):
    """Run fn(*args, **kwargs) in a thread. Raises TimeoutError or re-raises exceptions."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            raise TimeoutError(f"API call timed out after {timeout}s")


def clean_text(text_body):
    if not text_body:
        return ""
    soup = BeautifulSoup(text_body, "html.parser")
    return soup.get_text(separator="\n").strip()


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
                print(f"  📥 Found untagged #{len(pool)}", end="\r")
                if len(pool) >= batch_size:
                    break

        page_token = results.get("nextPageToken")
        if not page_token:
            break

    print()  # newline after the \r progress
    return pool


def triage_and_label_emails():
    print("=" * 60)
    print("EMAIL CLEANUP AGENT - TRIER MODE")
    print("=" * 60)
    print(f"Gmail Account: {EMAIL_ACCOUNT}")
    print(f"Ollama Model: {OLLAMA_MODEL} ({OLLAMA_HOST})")
    print("=" * 60 + "\n")

    # Connect to Gmail API
    try:
        print("Connecting to Gmail API...")
        service = get_gmail_service()
        print("✓ Gmail connected (OAuth 2.0)")
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return
    except Exception as e:
        print(f"❌ Failed to connect to Gmail: {e}")
        return

    # Check Ollama
    try:
        available = ollama_client.list()
        if not available.get("models"):
            print("⚠️  No Ollama models available")
            return
        print("✓ Model ready")
    except Exception as e:
        print(f"⚠️  Ollama unavailable: {e}")
        return

    # Ensure triage labels exist
    print("\nSetting up labels...")
    label_ids = {}
    for key, name in LABEL_NAMES.items():
        label_ids[key] = get_or_create_label(service, name)
        print(f"  ✓ Label ready: {name}")

    # Fetch oldest untagged emails
    print(f"\nFetching {BATCH_SIZE} untagged inbox emails...")
    try:
        messages = fetch_untagged_emails(service, label_ids, BATCH_SIZE)
    except Exception as e:
        print(f"❌ Failed to fetch emails: {e}")
        return

    if not messages:
        print("📭 Nothing to process — all inbox emails are already tagged.")
        return

    total_emails = len(messages)
    print(f"📬 Found {total_emails} emails to triage")
    print(f"🚀 Processing with {OLLAMA_MODEL}...\n")

    metrics = {key: 0 for key in LABEL_NAMES}
    metrics["ERROR_FALLBACK"] = 0
    ai_times = []
    delete_ids = []  # IDs labelled DELETE in this batch only


    for index, msg_ref in enumerate(messages, start=1):
        if _stop.is_set():
            print("\n⚠️  Stopped by user.")
            break

        print(f"\n[{index}/{total_emails}] Fetching email...")

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
            print(f"  ⚠️  Failed to fetch: {e}")
            metrics["ERROR_FALLBACK"] += 1
            continue

        # Extract headers
        try:
            subject_raw, encoding = decode_header(msg.get("Subject") or "No Subject")[0]
            subject = subject_raw.decode(encoding or "utf-8", errors="ignore") if isinstance(subject_raw, bytes) else subject_raw
        except Exception:
            subject = "No Subject"

        sender = msg.get("From") or "Unknown"
        date = msg.get("Date") or "Unknown"

        # Map Gmail category labels to human-readable hints
        CATEGORY_MAP = {
            "CATEGORY_PROMOTIONS": ("Promotions", "Lean DELETE — but use ATTENTION if there is a limited-time offer, expiring deal, or discount that may be genuinely useful."),
            "CATEGORY_SOCIAL":     ("Social",     "Lean DELETE — social media notifications rarely need action."),
            "CATEGORY_UPDATES":    ("Updates",    "Neutral — could go either way. Use ATTENTION for: appointments, bill due dates, payment confirmations, security alerts, account changes, shipping with action needed, or any deadline. Use DELETE for: generic digests, read receipts, routine status updates with no action required."),
            "CATEGORY_FORUMS":     ("Forums",     "Lean DELETE — forum digests are rarely urgent."),
            "CATEGORY_PERSONAL":   ("Personal",   "Lean ATTENTION — likely from a real person or a service tied to personal commitments."),
        }
        gmail_category = None
        category_hint = ""
        for label_id, (label_name, hint_text) in CATEGORY_MAP.items():
            if label_id in gmail_labels:
                gmail_category = label_name
                category_hint = f"Gmail category: '{label_name}'. {hint_text}"
                break

        print(f"  ✉  From    : {sender[:60]}")
        print(f"  📅 Date    : {date}")
        print(f"  📝 Subject : {str(subject)[:60]}")
        if gmail_category:
            print(f"  🏷  Category: {gmail_category}")
        # Extract body
        body = ""
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
                if ct == "text/plain":
                    body = clean_text(raw_body)
                    break
                elif ct == "text/html" and not html_fallback:
                    html_fallback = clean_text(raw_body)
        else:
            try:
                raw_body = msg.get_payload(decode=True).decode(errors="ignore")
                body = clean_text(raw_body)
            except Exception:
                body = ""

        if not body.strip():
            body = html_fallback
        body = body[:500]

        print(f"  🤖 Sending to LLM...")

        # Triage prompt
        valid_keys = " or ".join(f'"{k}"' for k in LABEL_NAMES)
        prompt = f"""You are an advanced AI email triage assistant. Analyze the email below and decide whether it needs attention or should be deleted.

Be ruthless but practical. Prioritize actionability, personal relevance, and important commitments over generic updates.

{category_hint}

Evaluate the email across three dimensions:
1. SENDER TYPE: Is it a real person, an automated system, a newsletter, a service notification, or spam?
2. URGENCY & ACTION: Does it require a reply, an action, or has a deadline? Or is it purely informational/promotional?
3. RELEVANCE: Is it directly tied to personal life, finances, health, legal matters, or active commitments?

DECISION CRITERIA:
- Use "ATTENTION" for: personal emails, bills or payment due, appointments or reminders, security alerts, account changes, receipts, medical, tax, legal notices, shipping requiring action, anything with a deadline or requiring a reply.
- Use "DELETE" for: newsletters, marketing with no expiring offer, social media digests, routine shipping notifications already delivered, automated digests with no action required, read receipts, generic status updates.

[EMAIL CONTENT START]
From: {sender}
Date: {date}
Subject: {subject}
Body: {body.strip()}
[EMAIL CONTENT END]

Respond ONLY with this JSON — no markdown, no explanation, no thinking tags:
{{"decision": {valid_keys}, "summary": "1 sentence: what this email is about", "sender_type": "e.g. Real Person / Newsletter / Automated Notification / Spam", "action_required": "e.g. None / Reply needed / Review by Friday", "relevance_score": <1-5>, "reason": "2-3 sentences: what the email is, why you scored it this way, and what tipped the decision."}}"""

        # Run Ollama
        ai_start = time.time()
        try:
            chat_kwargs = {
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "think": False,
            }
            model_options = MODEL_CONFIGS.get(OLLAMA_MODEL)
            if model_options is not None:
                chat_kwargs["options"] = model_options
            response = ollama_client.chat(**chat_kwargs)
            # Handle both dict and object (ChatResponse) returns from ollama library
            if isinstance(response, dict):
                msg_obj = response.get("message", {})
                response_text = msg_obj.get("content", "") if isinstance(msg_obj, dict) else str(msg_obj)
            elif hasattr(response, "message"):
                # ollama >= 0.2 returns a ChatResponse object: response.message.content
                msg_obj = response.message
                response_text = msg_obj.content if hasattr(msg_obj, "content") else str(msg_obj)
            else:
                response_text = str(response)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"⚠️  Ollama error: {e}")
            metrics["ERROR_FALLBACK"] += 1
            continue

        elapsed = time.time() - ai_start
        ai_times.append(elapsed)

        # Parse decision — strip <think>...</think> blocks Qwen3 thinking mode emits
        valid_decisions = list(LABEL_NAMES.keys())
        default_decision = valid_decisions[-1]  # "ATTENTION"
        try:
            clean_response = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL).strip()
            clean_response = re.sub(r"^```(?:json)?", "", clean_response).strip()
            clean_response = re.sub(r"```$", "", clean_response).strip()
            result = json.loads(clean_response)
            decision = result.get("decision", default_decision).upper()
            summary = result.get("summary", "")
            reason = result.get("reason", "No reason provided.")
            sender_type = result.get("sender_type", "Unknown")
            action_required = result.get("action_required", "None")
            relevance_score = result.get("relevance_score", "?")
        except Exception:
            decision = default_decision
            summary = ""
            reason = f"Could not parse model response, defaulting to Attention. Raw: {response_text[:120]!r}"
            sender_type = action_required = "Unknown"
            relevance_score = "?"
            metrics["ERROR_FALLBACK"] += 1

        if decision not in valid_decisions:
            decision = default_decision
            reason = "Invalid decision, corrected to Attention."
            metrics["ERROR_FALLBACK"] += 1

        metrics[decision] += 1

        # Apply label via Gmail API
        try:
            call_with_timeout(
                service.users().messages().modify(
                    userId="me", id=msg_ref["id"],
                    body={"addLabelIds": [label_ids[decision]], "removeLabelIds": []}
                ).execute
            )
            status_msg = f"🏷️  Labelled → {LABEL_NAMES[decision]}"
            if decision == "DELETE":
                delete_ids.append(msg_ref["id"])
        except (TimeoutError, Exception) as e:
            print(f"⚠️  Label failed for {msg_ref['id']}: {e}")
            metrics["ERROR_FALLBACK"] += 1
            status_msg = "⚠️  Label failed"

        print(f"  ⏱  Inference   : {elapsed:.2f}s")
        print(f"  {status_msg}")
        if summary:
            print(f"  💬 Summary     : {summary}")
        print(f"  👤 Sender Type : {sender_type}")
        print(f"  ⚡ Action      : {action_required}")
        print(f"  📊 Relevance   : {relevance_score}/5")
        print(f"  💡 Reason      : {reason}")
        print("-" * 60)


    # Summary
    total_processed = sum(metrics[k] for k in LABEL_NAMES)
    avg_ai_time = sum(ai_times) / len(ai_times) if ai_times else 0

    print("\n" + "=" * 40)
    print("      BATCH PERFORMANCE REPORT")
    print("=" * 40)
    print(f"Emails Processed : {total_processed}")
    for key, name in LABEL_NAMES.items():
        print(f"  {name:<20}: {metrics[key]}")
    if metrics["ERROR_FALLBACK"]:
        print(f"⚠️  Errors         : {metrics['ERROR_FALLBACK']}")
    print(f"Avg Inference    : {avg_ai_time:.2f}s")
    print(f"Total Time       : {sum(ai_times):.2f}s")
    print("=" * 40 + "\n")

    # ── Ask user whether to delete the emails just marked as ToDelete ──────────
    delete_count = metrics.get("DELETE", 0)
    if delete_count > 0 and not _stop.is_set():
        print(f"🗑️  {delete_count} email(s) were just marked as '{LABEL_NAMES['DELETE']}'.")
        try:
            answer = input("   Do you want to move them to Trash? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""

        if answer == "y":
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

            print()  # newline after \r progress
            print(f"\n✅ Done — {deleted} email(s) moved to Trash.")
            if errors:
                print(f"⚠️  {errors} email(s) could not be trashed.")
        else:
            print("   Skipped — emails remain labelled but not deleted.")


if __name__ == "__main__":
    try:
        triage_and_label_emails()
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
