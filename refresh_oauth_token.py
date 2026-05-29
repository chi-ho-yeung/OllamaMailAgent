"""
Gmail OAuth 2.0 Authentication
Follows the official Google Python quickstart pattern exactly.
Requires credentials.json downloaded from Google Cloud Console.
"""
import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"


def get_gmail_service():
    """
    Authenticate and return an authorized Gmail API service instance.
    - First run: opens browser for login, saves token.json
    - Subsequent runs: loads token.json, auto-refreshes if expired
    """
    creds = None

    # Load existing token if available
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    # If no valid credentials, refresh or re-authorize
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    "credentials.json not found.\n"
                    "Download it from Google Cloud Console:\n"
                    "  Google Auth Platform → Clients → Download OAuth client JSON\n"
                    f"  Save as: {os.path.abspath(CREDENTIALS_FILE)}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token for next run
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
        print(f"💾 Token saved to {TOKEN_FILE}")

    return build("gmail", "v1", credentials=creds)
