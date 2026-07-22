# Gmail Authentication Setup

## OAuth 2.0

### Step 1: Create OAuth Credentials
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing one)
3. Enable "Gmail API"
4. Go to "Credentials" → "Create Credentials" → "OAuth client ID"
5. Choose "Desktop app" as application type
6. Download the JSON file (contains client ID and secret)

### Step 2: Save the credentials file
Save the downloaded JSON as `secrets/credentials.json` in the project root.
No `.env` entries are needed for the client ID/secret — they live inside this
file.

### Step 3: How it works
MailAgent uses `google-auth-oauthlib`'s `InstalledAppFlow` to:
- Read `client_id`/`client_secret` directly from `secrets/credentials.json`
- Run the browser consent flow on first use, producing `secrets/token.json`
- Automatically refresh `token.json` on subsequent runs

See `refresh_oauth_token.py` for the implementation.
