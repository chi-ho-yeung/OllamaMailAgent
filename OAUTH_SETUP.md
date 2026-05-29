# Gmail Authentication Setup

## OAuth 2.0

### Step 1: Create OAuth Credentials
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing one)
3. Enable "Gmail API"
4. Go to "Credentials" → "Create Credentials" → "OAuth client ID"
5. Choose "Desktop app" as application type
6. Download the JSON file (contains client ID and secret)

### Step 2: Update Configuration
Create/update `.env` file:

```env
# OAuth 2.0 Credentials
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret-from-json-file
GOOGLE_TOKEN=your-access-token  # Can be generated once and stored

# Optional: For automated token refresh
GOOGLE_TOKEN_FILE_PATH=.token_cache
```

### Step 3: How it works
MailAgent uses Google's `google-auth-library` / `oauthlib` to:
- Authenticate with `client_id` and `client_secret`
- Exchange for an `access_token`
- Automatically refresh the token when expired
