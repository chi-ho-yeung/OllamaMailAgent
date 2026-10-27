"""
MailAgent Configuration Manager
Environment-based configuration (llama.cpp decision-model backend).
Importing this module has no side effects beyond loading secrets/.env.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

SECRETS_DIR = Path(__file__).parent / "secrets"
load_dotenv(SECRETS_DIR / ".env")

# ============================================
# AUTHENTICATION
# Gmail credentials come from Google's credential store, loaded by refresh_oauth_token.py.
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT", "")
USER_NAME = os.getenv("USER_NAME", "")

# ============================================
# Ollama decision-model server
# Defaults to local Ollama installation.
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").strip().rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "d3:2b")
# Backward-compatible aliases (mailAgent.py imports these names).
LLAMA_HOST = OLLAMA_HOST
LLAMA_MODEL = OLLAMA_MODEL
LLAMA_API_KEY = os.getenv("LLAMA_API_KEY", "").strip()
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "90"))

# ============================================
# TRIAGE TUNING - confidence cutoffs differ per model; test on your own inbox.
# You review everything before trashing, so a wrong DELETE costs little and a missed one
# costs a manual cleanup: the defaults lean toward recommending delete.
#
# HARD keep-signals (record / personal) at or above this keep the email.
HARD_KEEP_MIN = float(os.getenv("HARD_KEEP_MIN", "0.50"))
# The SOFT keep-signal (actionable) is vague and fires on ordinary marketing mail,
# so it only keeps the email at this higher score.
SOFT_KEEP_MIN = float(os.getenv("SOFT_KEEP_MIN", "0.80"))
# "Sent in bulk / automatically" probability needed to recommend DELETE...
DELETE_MIN_PROB = float(os.getenv("DELETE_MIN_PROB", "0.50"))
# ...and the lower bar used when code sees an unsubscribe/bulk header or an automated sender.
DELETE_MIN_PROB_BULK_HEADER = float(os.getenv("DELETE_MIN_PROB_BULK_HEADER", "0.35"))
# Characters of body text sent to the model (decision models have small contexts).
MAX_BODY_CHARS = int(os.getenv("MAX_BODY_CHARS", "1500"))

if not EMAIL_ACCOUNT:
    print("⚠️  WARNING: EMAIL_ACCOUNT not set in secrets/.env")

if __name__ == "__main__":
    print("\nConfiguration Summary:")
    print(f"  Gmail account: {EMAIL_ACCOUNT or '(not set)'}")
    print(f"  Credentials file: {SECRETS_DIR / 'credentials.json'}")
    print(f"  Token file: {SECRETS_DIR / 'token.json'}")
    print(f"  llama.cpp host: {OLLAMA_HOST}")
    print(f"  API key: {'set' if LLAMA_API_KEY else '(none)'}")
    print(f"  Model: {OLLAMA_MODEL or '(server default)'}")
    print(f"  HARD_KEEP_MIN={HARD_KEEP_MIN}  SOFT_KEEP_MIN={SOFT_KEEP_MIN}  DELETE_MIN_PROB={DELETE_MIN_PROB}  DELETE_MIN_PROB_BULK_HEADER={DELETE_MIN_PROB_BULK_HEADER}")
