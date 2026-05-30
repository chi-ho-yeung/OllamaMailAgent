# MailAgent — Email Triage Agent

Automatically triages your Gmail inbox using a local LLM (Qwen 3.5 via Ollama).
Runs in batches, labels emails, and gives you the option to move marked emails to Trash.

---

## How It Works

```
1. Connect to Gmail API (OAuth 2.0)
2. Connect to local Ollama instance, verify model is loaded
3. Create triage labels in Gmail if they don't exist yet
4. Find the 5 oldest inbox emails not yet labelled by this agent
5. For each email:
     - Extract subject, sender, date, and clean body text
     - Ask the LLM to decide: DELETE or ATTENTION
     - Apply the matching Gmail label
6. Print a summary report
7. If any emails were labelled 1-ToDelete, prompt:
     "Do you want to move them to Trash? [y/N]"
     - y → moves all 1-ToDelete emails to Gmail Trash (recoverable for 30 days)
     - N / Enter → skips; emails stay labelled but untouched
```

Emails labelled `1-ToDelete` are **not moved automatically**. After each batch the
agent asks whether to send them to Trash — you stay in control every run.

---

## Triage Labels

Defined in `LABEL_NAMES` at the top of `mailagent.py`:

| Key         | Gmail Label      | Criteria |
|-------------|------------------|----------|
| `DELETE`    | `1-ToDelete`     | Newsletters, marketing, shipping alerts, social media, promotions |
| `ATTENTION` | `1-NeedAttention`| Personal emails, receipts, medical, tax, bank alerts, legal notices |

Labels are created automatically on first run. The `1-` prefix makes them sort
to the top of your Gmail label list.

To add or rename categories, edit only the `LABEL_NAMES` dict — everything else
(prompt, validation, metrics, report) derives from it automatically.

---

## Requirements

- Python 3.x
- [Ollama](https://ollama.com/) running locally with `qwen3.5:2b` pulled (recommended — see [LLM Settings](#llm-settings))
- Gmail API credentials (see [README_AUTH.md](README_AUTH.md))

Install Python dependencies:

```bash
pip install -r requirements.txt
```

---

## Setup

Several files required to run MailAgent are excluded from the repository because
they contain credentials or runtime state. You must create them locally before
running the agent.

### 1. `.env` — credentials and settings

Create a `.env` file in the project root:

```env
# Gmail address to process
EMAIL_ACCOUNT=you@gmail.com

# OAuth 2.0 credentials (from Google Cloud Console — see README_AUTH.md)
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret

# Optional: override Ollama defaults
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen3.5:2b

# Optional: path to labels config (default: config/labels.json)
LABELS_PATH=config/labels.json
```

See [README_AUTH.md](README_AUTH.md) for how to obtain `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` from the Google Cloud Console.

### 2. `credentials.json` — Google OAuth client config

Download this file from the Google Cloud Console after creating an OAuth 2.0
client ID (Desktop app type). Save it as `credentials.json` in the project root.

Full instructions: [README_AUTH.md](README_AUTH.md) → Step 1.

### 3. `token.json` and `.token_cache` — OAuth tokens

These are generated automatically the first time you run the agent or
`refresh_oauth_token.py`. You do not need to create them manually:

```bash
python refresh_oauth_token.py
```

A browser window will open for you to log in and grant Gmail access. The token
is saved locally and refreshed automatically on subsequent runs.

### 4. `config/labels.json` — Gmail label definitions

Create a `config/` directory and add a `labels.json` file that defines the Gmail
labels the agent will create and apply. Minimal example matching the defaults:

```json
{
  "DELETE": "1-ToDelete",
  "ATTENTION": "1-NeedAttention"
}
```

The keys must match the `LABEL_NAMES` dict in `mailagent.py`. Labels are created
in your Gmail account automatically on first run if they don't already exist.

---

## Configuration

All settings live in `config.py` and `.env`:

| Setting        | Description                                     |
|----------------|-------------------------------------------------|
| `EMAIL_ACCOUNT`| Gmail address to process                        |
| `OLLAMA_MODEL` | Model name (default: `qwen3.5:2b`, recommended) |
| `OLLAMA_HOST`  | Ollama server URL                               |

Batch size (emails per run) is set in `mailagent.py`:

```python
BATCH_SIZE = 5
```

---

## Usage

```bash
python mailagent.py
```

The agent will print progress for each email and a summary report at the end:

```
...
========================================
      BATCH PERFORMANCE REPORT
========================================
Emails Processed : 5
  1-ToDelete          : 3
  1-NeedAttention     : 2
Avg Inference    : 1.45s
Total Time       : 7.23s
========================================

🗑️  3 email(s) were just marked as '1-ToDelete'.
   Do you want to move them to Trash? [y/N]: y

🗑️  Moving emails to Trash...
  🗑️  Trashed 3

✅ Done — 3 email(s) moved to Trash.
```

Press **Ctrl+C** at any time to stop cleanly between emails.

---

## LLM Settings

### Model Performance

`qwen3.5:2b` is the recommended default — it produces accurate triage decisions and
runs about 3× faster than the 4b variant, making it a good fit for most laptops.
If you have a more powerful machine, `qwen3.5:4b` is also fully supported and may
produce more nuanced reasoning on borderline emails. To switch models, just update
`OLLAMA_MODEL` in your `.env`.

| Model                  | Avg. inference time | Notes                             |
|------------------------|--------------------:|-----------------------------------|
| `qwen3.5:2b` ✅        | ~23s                | **Recommended default.** Fast and accurate |
| `qwen3.5:4b`           | ~60–70s             | More powerful hardware recommended|
| `qwen2.5:3b-instruct`  | ~10s                | No thinking mode, fast            |

### Per-Model Configuration

Each model can have its own `options` block (or none at all) defined in `config.py`:

```python
MODEL_CONFIGS = {
    "qwen2.5:3b-instruct": None,        # use Ollama defaults
    "qwen3.5:4b": {
        "temperature": 0.5,
        "top_p": 0.8,
    },
    "qwen3.5:2b": {
        "temperature": 0.7,
        "top_p": 0.8,
    },
}
```

`temperature` and `top_p` values are Qwen's recommended defaults for non-thinking
mode. Adjust to tune creativity vs. consistency.

### Disabling Thinking Mode (Qwen3)

Qwen3 models ship with an extended chain-of-thought reasoning mode that generates
a large `<think>...</think>` block before every answer. Left enabled, this inflated
a single email analysis from ~60s to nearly **400s**.

Three things are needed to fully suppress it — getting any one of them wrong leaves
thinking partially or fully active:

| # | What | Where | Why it matters |
|---|------|-------|----------------|
| 1 | `think=False` | Top-level `ollama.chat()` kwarg | The correct place to pass this flag. Putting it inside `options={}` is silently ignored by the Ollama library. |
| 2 | `options={"temperature": 0.7, "top_p": 0.8}` | `ollama.chat(options=...)` | Qwen's own docs recommend these values when thinking is off; without them the model can become overly conservative or erratic. |
| 3 | Strip `<think>...</think>` from the response | Response parsing in `mailagent.py` | A safety net — occasionally a stray thinking block slips through; stripping it prevents JSON parse failures. |

```python
# The call that makes it work
response = ollama.chat(
    model=OLLAMA_MODEL,
    messages=[{"role": "user", "content": prompt}],
    think=False,                          # ← top-level, NOT inside options
    options={"temperature": 0.7, "top_p": 0.8},
)

# Safety net in parsing
clean = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL).strip()
```

> **Note:** `qwen2.5:3b-instruct` does not have a thinking mode — `think=False`
> is a no-op for that model and `MODEL_CONFIGS` sets no options override.

---

## File Structure

```
mailagent/
├── mailagent.py          # Main agent
├── config.py             # Model and account settings
├── refresh_oauth_token.py# OAuth token management
├── requirements.txt      # Python dependencies
├── .env                  # Credentials (not committed — see Setup)
├── credentials.json      # Google OAuth client config (not committed — see Setup)
├── token.json            # OAuth token (auto-managed, not committed)
├── README.md             # This file
├── README_AUTH.md        # Gmail OAuth setup guide
└── OAUTH_SETUP.md        # Additional OAuth notes
```

---

## Authentication

See [README_AUTH.md](README_AUTH.md) for the full Gmail OAuth 2.0 setup guide.
