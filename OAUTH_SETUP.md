# Gmail Authentication Setup

This guide covers setting up Gmail OAuth 2.0 for MailAgent.

## Prerequisites

Before starting, ensure you've created the `secrets/` directory:

```bash
mkdir secrets
```

## OAuth 2.0 Setup

### Step 1: Create OAuth Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Search for and enable **"Gmail API"**
4. Go to **"Credentials"** → **"Create Credentials"** → **"OAuth client ID"**
5. Choose **"Desktop app"** as the application type
6. Download the JSON file (it contains your `client_id` and `client_secret`)

### Step 2: Save the Credentials

Save the downloaded JSON file as `secrets/credentials.json` in your project.

The file should look like this (with your actual values):

```json
{
  "client_id": "123456789-abc.apps.googleusercontent.com",
  "client_secret": "GOCSPX-xxxx",
  "project_id": "your-project-id",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token",
  "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
  "redirect_uris": ["http://localhost"]
}
```

> **Note:** No `.env` entries are needed for the client ID/secret — they live inside this file.

### Step 3: First Run

Run MailAgent for the first time:

```bash
python mailagent.py
```

The first time you run it, `google-auth-oauthlib`'s `InstalledAppFlow` will:

1. Open a browser window asking you to sign in to Google
2. Request permission to read/modify your emails (the scopes MailAgent needs)
3. After you consent, automatically create `secrets/token.json`

On subsequent runs, the token refreshes automatically — you shouldn't need to re-authenticate.

## Troubleshooting

### Token expires or is revoked

Simply delete `secrets/token.json` and run `python mailagent.py` again. It will re-prompt for consent.

### Need to re-run auth standalone

```bash
python refresh_oauth_token.py
```

This will delete any existing token and start a fresh OAuth flow.

### Scopes used by MailAgent

- `https://www.googleapis.com/auth/gmail.readonly` — Read emails
- `https://www.googleapis.com/auth/gmail.modify` — Add labels, archive (modify)

These are the minimal scopes needed for the tool to function.
