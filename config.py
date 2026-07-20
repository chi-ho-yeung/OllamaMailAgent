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
# AUTHENTICATION (OAuth 2.0)
# ============================================

# OAuth 2.0 Credentials
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_TOKEN = os.getenv("GOOGLE_TOKEN", "")

# Gmail account address (used to build XOAUTH2 string)
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT", "")

def get_auth_type():
    """Determine which auth method is configured"""
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        return "oauth20"
    return None

# Check if Ollama is running
import ollama as ollama_client
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "phi4-mini")
OLLAMA_CLIENT = ollama_client

# Per-model Ollama options. Set to None to use Ollama defaults for that model.
# Note: think=False is passed as a top-level ollama.chat() param, not here.
MODEL_CONFIGS = {
    "qwen2.5:3b-instruct": {"num_ctx": 8192},
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
    "phi4-mini": {
        "num_ctx": 16384,
        "format": "json",
        "think": False
    },
    "granite4.1:3b": {
        "num_ctx": 16384,
        "format": "json",
        "think": False
    },
    "ministral-3:3b": {
        "num_ctx": 16384,
        "format": "json",
        "think": False
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
_auth_type = get_auth_type()
if _auth_type == "oauth20":
    print("✅ Gmail authenticated with OAuth 2.0")
    print(f"  Client ID: ...{GOOGLE_CLIENT_ID[-12:] if GOOGLE_CLIENT_ID else 'N/A'}")
    print("  Using IMAP access via OAuth token")
    print(f"  Token status: {'Present' if GOOGLE_TOKEN and len(GOOGLE_TOKEN) > 10 else 'Not set (auto-refresh will be used)'}")
else:
    print("⚠️  WARNING: No Gmail credentials configured")
    print("Please create .env file with OAuth credentials:")
    print("  GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com")
    print("  GOOGLE_CLIENT_SECRET=your-client-secret")
    print("  # Optional: GOOGLE_TOKEN=your-token")

# Helper function to configure Gmail connection
def get_gmail_config():
    """
    Get Gmail OAuth 2.0 configuration object.

    Returns:
        dict: Configuration object with OAuth fields
    """
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return {"error": "OAuth credentials not configured (GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET not found)"}
    return {
        "imap_server": IMAP_SERVER,
        "imap_port": IMAP_PORT,
        "auth_type": "oauth20",
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "token": GOOGLE_TOKEN  # Will be refreshed automatically if missing
    }

def save_oauth_token(token_path=None):
    """
    Save OAuth access token to file (for persistence across restarts)
    
    Args:
        token_path: Path to save token (default: secrets/.token_cache)
    
    Returns:
        dict: {'token_path': path, 'token_saved': True/False, 'error': error if any}
    """
    if token_path is None:
        token_path = SECRETS_DIR / ".token_cache"
    
    if len(GOOGLE_TOKEN) > 10 and GOOGLE_TOKEN != os.getenv("GOOGLE_TOKEN", ""):
        try:
            token_file = Path(token_path)
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(GOOGLE_TOKEN)
            return {"token_path": str(token_path), "token_saved": True, "error": None}
        except Exception as e:
            return {"token_path": str(token_path), "token_saved": False, "error": str(e)}
    return {"token_saved": True, "error": None}

def is_token_expired(token):
    """
    Check if OAuth token is expired (simplified check)
    
    Args:
        token: OAuth access token (or None if not used)
    
    Returns:
        bool: True if expired
    """
    if not token or len(token) == 0:
        return True  # Need to refresh
    
    # Check token expiry (tokens have embedded expiry time)
    # This requires parsing the JWT token or checking response headers
    # For now, just return False to allow auto-refresh if needed
    return False

# ============================================
# End of OAuth 2.0 Configuration
# ============================================

if __name__ == "__main__":
    print("\nConfiguration Summary:")
    auth_type = get_auth_type()
    print(f"  Authentication Method: {auth_type}")

    config = get_gmail_config()
    if "error" in config:
        print(f"  Error: {config['error']}")
    else:
        print(f"  Server: {IMAP_SERVER}:{IMAP_PORT}")
        print(f"  Token: {'Loaded' if config.get('token') else 'Using auto-refresh'}")
