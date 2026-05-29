# Gmail Authentication Guide

## Quick Start

MailAgent authenticates via **OAuth 2.0** using credentials stored in your `.env` file.

---

## Setup

> Follow the official Google Python quickstart for full details:
> **https://developers.google.com/workspace/gmail/api/quickstart/python**

### Step 1: Create OAuth Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project:
   - Click "Create Project"
   - Name it (e.g., "MailAgent")
   - Click "Create"

3. Enable Gmail API:
   - Go to "APIs & Services" → "Library"
   - Search for "Gmail API"
   - Click "Enable"

4. Create OAuth Client ID:
   - Go to "APIs & Services" → "Credentials"
   - Click "Create Credentials" → "OAuth client ID"
   - Choose "Desktop app" as application type
   - Click "Create"
   - **Download the JSON file** → Save locally

### Step 2: Configure Environment

Edit `.env` with your OAuth credentials:

```env
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret-from-json-file
```

### Step 3: (Optional) Create Service Account

For headless operation, create a service account:

1. In Google Cloud Console:
   - Go to "IAM & Admin" → "Service Accounts"
   - Click "Create Service Account"
   - Name: `mailagent@your-project.iam.gserviceaccount.com`
   - Click "Done"

2. Create a JSON key:
   - Select your service account
   - Click "Keys" → "Add Key" → "Create new key"
   - Choose "JSON"
   - Download and save as `service_account.json`
   - Enable in `.env`: `GOOGLE_USE_SERVICE_ACCOUNT=true`

### Step 3b: Add yourself as a Test User

Since the app is in Testing mode, only explicitly approved accounts can log in.

1. In Google Cloud Console, go to **Google Auth Platform** → **Audience**
2. Scroll down to **Test users**
3. Click **+ Add users**
4. Enter your Gmail address
5. Click **Save**

Without this step, the browser login will show "Access blocked: app has not completed the Google verification process."

> You do not need to publish or verify the app — Testing mode with your own account added is sufficient for personal use.

### Step 4: Run MailAgent

```bash
python main.py
```

MailAgent will automatically:
- Load OAuth credentials from `.env`
- Use auto-refresh if token is missing/expired
- Store tokens securely in `.token_cache`

---

## Troubleshooting

### "OAuth credentials not configured"

```bash
# Check your .env file exists
ls -la .env

# Verify variables are set (note: don't commit to git)
grep -v "^#" .env | grep -v "^$"
```

### "No default credentials available"

```bash
# Install Google Auth
pip install google-auth google-auth-httplib2 google-auth-oauthlib

# Login with application default credentials
gcloud auth application-default login

# Then run your app
python main.py
```

### "Token expired or invalid"

```bash
# Delete old token file
rm .token_cache

# Regenerate token
python refresh_oauth_token.py
```

---

## Security Best Practices

1. **Never commit credentials** to git (`.gitignore` already covers):
   ```
   .env
   service_account.json
   .token_cache
   ```

2. **Use environment variables** only (no hardcoded secrets)

3. **Limit OAuth scopes** to what you need:
   - `https://www.googleapis.com/auth/gmail.readonly` (view only)
   - `https://www.googleapis.com/auth/gmail.send` (for sending)
   - `https://www.googleapis.com/auth/gmail.labels` (for labels)

---

**Need help?** Check the [Google OAuth documentation](https://developers.google.com/identity/protocols/oauth2)
