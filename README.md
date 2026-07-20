# MailAgent — Email Triage Agent

Automatically triages your Gmail inbox using a local LLM (Qwen 2.5 via Ollama).
Runs in batches, labels emails, and gives you the option to move marked emails to Trash.

---

## Project Goals

### Proof of Concept: Small Models, Modest Hardware

This project is a proof of concept for running useful AI agents on a mid-range laptop —
specifically a machine with **16 GB RAM and ~2 GB VRAM**. No cloud API, no GPU cluster.
Just a local Ollama instance and a small quantized model.

The core thesis: **LLMs are exceptionally good at reading, summarizing, and evaluating
the value of text.** Email is a perfect domain to test this — it is high-volume,
mostly text, and the cost of mis-classification is low but the productivity gain of
doing it well is high. The value of automation scales directly with inbox size: the
larger and messier your inbox, the more time this saves.

Small models (2–4B parameters) turn out to be well-suited for this task. Triage does
not require deep reasoning — it requires pattern recognition, tone detection, and
judgment about urgency. A 3B model running locally at ~10s per email is fast enough
to clear a backlog overnight and is more than capable of making correct calls on
newsletters, bills, appointments, and spam.

### Approach: Deterministic Function Calls vs. Agentic Tool Calling

This project deliberately takes a different architectural approach from
[ollama-assistant-cli](https://github.com/chi-ho-yeung/ollama-assistant-cli), a companion
project that uses the LLM as an agent — letting the model decide which tools to call,
in what order, and how to chain them together via LangGraph.

MailAgent does the opposite: **the LLM is called for one specific, bounded function** —
read this email and return a structured triage decision. The code controls the flow
entirely; the model never decides what happens next. This makes the app faster, more
predictable, and easier to debug on constrained hardware.

The two approaches represent a genuine trade-off:

| | MailAgent (this project) | ollama-assistant-cli |
|---|---|---|
| **LLM role** | Performs a specific function | Drives agentic tool calling |
| **Flow control** | Python code | LLM via LangGraph |
| **Predictability** | High — same inputs, same behavior | Lower — model decides next step |
| **Flexibility** | Low — one task, done well | High — open-ended tasks |
| **Speed** | Faster — one call per email | Slower — multi-step reasoning |

The hypothesis here is that for well-defined tasks with large volumes of data — like
email triage — a deterministic, function-call model outperforms an open-ended agent
both in speed and reliability on modest hardware.

---

A second goal is for the agent to **get better the longer it runs.** The current version
is stateless — each batch starts fresh with no memory of past decisions. Future
iterations should:

- Track decisions and outcomes (e.g. emails you manually un-trash or re-label)
- Build a personal profile of senders, domains, and topics you care about
- Use that history to fine-tune the triage prompt or bias decisions for your inbox
- Eventually flag patterns: *"You always delete emails from this sender"* or
  *"Emails with this subject pattern consistently need attention"*

The long-term vision is an agent that starts generic and converges toward your
specific habits and preferences — without ever sending your data to the cloud.

#### Label corrections as a learning signal

The post-batch menu includes a **Correct a label** option (option 4) that lets you
flip any email in the current batch from `1-NeedAttention` → `1-ToDelete` or vice
versa. Each flip is a ground-truth signal: the LLM got it wrong, and you know why.

Two failure modes are worth tracking separately:

| Flip direction | What it means | Future use |
|---|---|---|
| `NeedAttention` → `ToDelete` | LLM was too cautious — gave ATTENTION to something that didn't warrant it | Could tighten the DELETE criteria in the prompt, or lower the relevance threshold for certain sender types |
| `ToDelete` → `NeedAttention` | LLM was too aggressive — marked something important for deletion | Higher priority to fix; could add sender/domain to a soft-trust list, or add subject patterns to the ATTENTION criteria |

The code currently applies the label flip immediately in Gmail. The next step is to
persist each correction to `corrections.yml`. The planned format:

```yaml
corrections:
  - date: '2026-05-29'
    from: ToDelete
    to: NeedAttention
    sender: billing@acme.com
    subject: Your invoice is ready
    reason: ''   # optional note by user
```

Once enough corrections accumulate, they can be used to:
- Automatically add frequently-corrected senders to the trusted list
- Surface recurring patterns to the user ("You've corrected 5 emails from
  newsletters this week — consider adjusting the promotions hint")
- Eventually fine-tune the prompt with few-shot examples drawn from real corrections

---

## How It Works

```
1. Connect to Gmail API (OAuth 2.0)
2. Connect to local Ollama instance, verify model is loaded
3. Create triage labels in Gmail if they don't exist yet
4. Find the 10 oldest inbox emails not yet labelled by this agent
5. For each email:
     - Check if sender is in contacts.yml trusted list → label ATTENTION instantly, skip LLM
     - Otherwise extract subject, sender, date, body, and Gmail category hint
     - Ask the LLM to decide: DELETE or ATTENTION, with highlights and action
     - Apply the matching Gmail label
       · DELETE    → archived (removed from INBOX)
       · ATTENTION → label applied, stays in INBOX
       · ERROR     → label applied, stays in INBOX for manual review
6. Print a batch performance report
7. Post-batch menu:
     1  Run another batch
     2  Move marked emails to Trash (with confirmation)
     3  Add a sender to the trusted contact list
     4  Correct a label (flip DELETE ↔ ATTENTION)
     x  Exit
```

Emails labelled `1-ToDelete` are **not moved automatically**. Option 2 asks for
confirmation before trashing — you stay in control every run. Only DELETE emails
are archived out of the inbox; ATTENTION and ERROR emails remain visible.

---

## Triage Labels

Defined in `LABEL_NAMES` at the top of `mailagent.py`:

| Key         | Gmail Label       | Icon | Inbox | Criteria |
|-------------|-------------------|------|-------|----------|
| `DELETE`    | `1-ToDelete`      | 🗑️   | Archived | Newsletters, marketing, shipping alerts, social media, promotions |
| `ATTENTION` | `1-NeedAttention` | 👁️   | Kept  | Personal emails, receipts, medical, tax, bank alerts, legal notices |
| `ERROR`     | `1-ProcessError`  | ⚙️   | Kept  | LLM could not parse the email or returned an invalid decision — review manually |

Labels are created automatically on first run. The `1-` prefix makes them sort
to the top of your Gmail label list.

To add or rename categories, edit only the `LABEL_NAMES` dict — everything else
(prompt, validation, metrics, report) derives from it automatically.

---

## Requirements

- Python 3.x
- [Ollama](https://ollama.com/) running locally with your chosen model pulled (see [LLM Settings](#llm-settings))
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
OLLAMA_MODEL=qwen2.5:3b-instruct

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
| `OLLAMA_MODEL` | Model name (default: `qwen2.5:3b-instruct`, recommended) |
| `OLLAMA_HOST`  | Ollama server URL                               |

Batch size (emails per run) is set in `mailagent.py`:

```python
BATCH_SIZE = 10
```

---

## Usage

```bash
python mailagent.py
```

The agent processes each email and prints a per-email summary, then shows a
performance report and the post-batch menu:

```
[2/10] [Updates] From: "Amazon.com" <auto-confirm@amazon.com>
  📅 Tue, 19 May 2026 | 📝 Ordered: "FUMAX Shower Door Hooks 10..."
  🗑️ 1-ToDelete | ⏱ 26.9s | 📊 Rel: 1/5
  💬 Amazon shipping confirmation for a non-actionable, already-delivered order.
  💡 Routine delivery confirmation with no deadlines or follow-up action needed.
------------------------------------------------------------

[3/10] [Personal] From: billing@acme.com
  📅 Thu, 29 May 2026 | 📝 Your invoice #4821 is ready
  👁️ 1-NeedAttention | ⏱ 21.3s | 📊 Rel: 4/5
  💬 Invoice #4821 for $149.00 due June 5 with PDF attached.
  💡 Bill with a deadline requiring action — kept in inbox.
------------------------------------------------------------

========================================
      BATCH PERFORMANCE REPORT
========================================
Emails Processed : 10
  🗑️ 1-ToDelete          : 6
  👁️ 1-NeedAttention     : 3
  ⚙️ 1-ProcessError      : 1
Avg Inference    : 22.1s
Total Time       : 132.6s
========================================

What would you like to do?
  1  Run another batch
  2  Move 6 marked email(s) to Trash
  3  Add a sender to contact list
  4  Correct a label
  x  Exit

>
```

Press **Ctrl+C** at any time to stop cleanly between emails.

---

## LLM Settings

### Model Performance

`qwen2.5:3b-instruct` is the recommended default — it produces accurate triage
decisions with no thinking-mode overhead, making it both fast and predictable
on modest hardware. To switch models, update `OLLAMA_MODEL` in your `.env`.

| Model                  | Avg. inference time | Thinking mode | Notes                             |
|------------------------|--------------------:|:-------------:|-----------------------------------|
| `qwen2.5:3b-instruct` ✅ | ~10s               | None          | **Recommended default.** No thinking mode, very fast |
| `qwen3.5:2b`           | ~23s                | Suppressed    | Fast and accurate                  |
| `qwen3.5:4b`           | ~60–70s             | Suppressed    | More powerful hardware recommended |
| `phi4-mini`            | ~15s                | None          | Microsoft model, compact and capable |
| `granite4.1:3b`        | ~12s                | None          | IBM Granite, strong instruction following |
| `ministral-3:3b`       | ~10s                | None          | Mistral's 3B, fast and efficient   |
| `liquidai/lfm2.5-1.2b-instruct:latest` | ~5s | None          | Liquid AI's 1.2B, smallest option tried |

### Per-Model Configuration

Each model can have its own `options` block, or none at all — see `MODEL_CONFIGS`
(and the `DEFAULT_MODEL_CONFIG` fallback used for any model not listed there)
in `config.py`.

`temperature` and `top_p` values are Qwen's recommended defaults for non-thinking
mode. Adjust to tune creativity vs. consistency. Models without thinking mode
(`qwen2.5:3b-instruct`) need no `think` key at all.

To add a new model, add an entry to `MODEL_CONFIGS` with `format: "json"` and
`think: False` if the model supports thinking mode. Pull it with `ollama pull
<model>` and set `OLLAMA_MODEL` in `.env`.

### Suppressing Thinking Mode

Several supported models ship with an extended chain-of-thought reasoning mode that generates a large `<think>...</think>` block before every answer. However, small LLMs typically struggle with reasoning at this scale; the cognitive overhead often outweighs any quality gain. For Qwen3.5, leaving thinking enabled inflated a single email analysis from ~60s to nearly 400s without significant improvement in triage accuracy. Other models of this size typically do not support reasoning modes and are more performant as a result.

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
> is a no-op for that model. The `phi4-mini`, `granite4.1:3b`, and `ministral-3:3b`
> models support the flag and have it set in `MODEL_CONFIGS`.

---

## File Structure

```
mailagent/
├── mailagent.py          # Main agent
├── config.py             # Model and account settings
├── refresh_oauth_token.py# OAuth token management
├── requirements.txt      # Python dependencies
├── contacts.yml          # Trusted senders (auto-created, not committed)
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
