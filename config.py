"""
MailAgent Configuration Manager
Environment-based configuration for security
"""
import os
import json
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables from secrets/.env (if exists)
SECRETS_DIR = Path(__file__).parent / "secrets"
load_dotenv(SECRETS_DIR / ".env")

# ============================================
# AUTHENTICATION
# Credentials come from Google's credential store, loaded by refresh_oauth_token.py.
# The credentials.json + token.json pattern is the only one we need:
#   1. First run: OAuth consent → creates both .json files under secrets/
#   2. Subsequent runs: refresh_oauth_token.py loads/refreshes them automatically
# No client-id/client-secret env vars are needed — they're embedded in credentials.json.

# Gmail account address (display/logging only)
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT", "")

# Check if Ollama is running
import ollama as ollama_client
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct")
OLLAMA_CLIENT = ollama_client

# Fallback options for any model not listed in MODEL_CONFIGS below. 4096
# tokens is comfortably more than the triage prompt needs (instructions +
# the 1500-char truncated email body come to well under 1500 tokens), so
# there's no need to give every model its own entry just to set this.
DEFAULT_MODEL_CONFIG = {
    "num_ctx": 4096,
    "format": "json",
}

# Per-model Ollama options. Set to None to use Ollama defaults for that model.
# Only add an entry here if a model needs something different from
# DEFAULT_MODEL_CONFIG (e.g. a bigger context window, tuned temperature/top_p,
# or think=False for models that support "thinking" mode).
# Note: think=False is passed as a top-level ollama.chat() param, not here.
MODEL_CONFIGS = {
    "qwen3.5:4b": { "format": "json",
        "num_ctx": 16384,
        "temperature": 0.5,
        "top_p": 0.8,
    },
    "qwen3.5:2b": { "format": "json",
        "num_ctx": 16384,
        "temperature": 0.7,
        "top_p": 0.8,
    },
}

try:
    available = ollama_client.list()
    has_models = len(available.get('models', [])) > 0
    if has_models:
        print(f"✅ Ollama is running")
    else:
        print(f"⚠️  Warning: No Ollama models detected")
except Exception as e:
    print(f"⚠️  Could not connect to Ollama: {e}")

# Label folder path
LABELS_PATH = os.getenv("LABELS_PATH", "config/labels.json")

# Server Settings
IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

# Print configuration summary
if EMAIL_ACCOUNT:
    print(f"✅ Gmail account: {EMAIL_ACCOUNT}")
else:
    print("⚠️  WARNING: EMAIL_ACCOUNT not set in secrets/.env")
print("  Auth: secrets/credentials.json + secrets/token.json (see refresh_oauth_token.py)")

# ============================================
# End of Authentication Configuration
# ============================================

if __name__ == "__main__":
    print("\nConfiguration Summary:")
    print(f"  Gmail account: {EMAIL_ACCOUNT or '(not set)'}")
    print(f"  Credentials file: {SECRETS_DIR / 'credentials.json'}")
    print(f"  Token file: {SECRETS_DIR / 'token.json'}")
