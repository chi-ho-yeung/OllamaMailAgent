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

### Two-Stage Triage: Right-Sizing the Task for the Model

Triage runs as **two separate LLM calls**, not one — a change made specifically to
get better results out of small models.

The original single-prompt design asked the model to do several things in one pass:
check five different objective rules (bill? deadline? notice? promo? real person?),
weigh soft signals like sender identity and tone, *and* commit to a final
verdict — all in one shot. In testing, this was where small models broke
down. A 3B model correctly reasoned that an email failed every rule, then talked
itself out of its own conclusion anyway because a promotional detail in the body kept
pulling it toward the wrong answer. The failure wasn't the model's language skill or
its knowledge of the rules — it was asking one pass to both apply several rules *and*
arbitrate between them under conflicting pressure, which turns out to be the weakest
link for models at this size.

The fix: split detection from judgment, into two narrower calls.

- **Stage 1** is a fast, cheap classifier. Its only job is to check an email against
  a short list of *objective, checkable* rules — a dollar amount tied to a bill, an
  unexpired deadline, a security/legal/tax notice, a still-valid promotion — and sort
  the email into `KEEP`, `DISCARD`, or `UNSURE`. No judgment calls, no weighing
  competing signals. Most emails resolve here.
- **Stage 2** only runs for the `UNSURE` survivors — the genuinely ambiguous minority.
  This is where the soft, harder-to-verify judgment call lives: how recent is this,
  does it address the user by name, does the sender's `Reply-To` undercut a
  personal-looking `From` name, does the body read like real correspondence or a
  sales pitch. Only here does the model get asked to weigh several fuzzy signals
  against each other — and only on emails where that's actually necessary.

In addition, a personal-*sounding* sender is deliberately excluded from stage 1's
KEEP list — sender identity can be spoofed via `Reply-To`, so it's held back for
stage 2's closer, cross-referenced look rather than getting a fast, unverified pass.

The underlying idea generalizes: **narrow, single-purpose classification is where
small models are reliable; multi-factor arbitration in a single pass is where they
aren't.** Two stages was the minimum split needed to fix the failure mode observed —
if a smaller model (this project was tested down to a 1.2B) starts showing the same
kind of "correct rule, wrong final answer" behavior even within a single stage, the
plan is to split further — e.g. giving stage 2's judgment signals (recency, sender identity,
tone) their own passes — rather than trying to fix it by writing a more emphatic prompt.

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

---

## How It Works

```
1. Connect to Gmail API (OAuth 2.0)
2. Connect to local Ollama instance, verify model is loaded
3. Create triage labels in Gmail if they don't exist yet
4. Find the 10 oldest inbox emails not yet labelled by this agent
5. For each email, print one compact progress line as it finishes:
     - Check if sender is in contacts.yml trusted list → label ATTENTION instantly, skip both LLM stages
     - Otherwise extract subject, sender, Reply-To, date, body, and Gmail category hint
     - STAGE 1 (always runs): ask the LLM to sort the email into KEEP / DISCARD /
       UNSURE against objective rules (money, deadline, notice, valid promo)
       · KEEP    → ATTENTION, reason generated from the matched rule
       · DISCARD → DELETE, reason generated in code
       · UNSURE  → falls through to STAGE 2
     - STAGE 2 (only for UNSURE survivors): ask the LLM to weigh softer signals —
       recency, addressed by name, sender identity vs. Reply-To, sales-pitch tone —
       and decide DELETE or ATTENTION with its own reasoning
     - Apply the matching Gmail label
       · DELETE    → archived (removed from INBOX)
       · ATTENTION → label applied, stays in INBOX
       · ERROR     → label applied, stays in INBOX for manual review
6. Print a batch performance report
7. Print full Detailed Results — every email processed, grouped
   1-NeedAttention → 1-ProcessError → 1-ToDelete, sorted by relevance score
   within each group (message numbers reflect this order, not processing order)
8. Post-batch menu:
     R  Run another batch
     T  Move marked emails to Trash (with confirmation)
     A  Add a sender to the trusted contact list (A.# to specify a given message number)
     x  Exit
```

See [Two-Stage Triage: Right-Sizing the Task for the Model](#two-stage-triage-right-sizing-the-task-for-the-model)
above for why triage is split into two LLM calls instead of one, and what
determines whether an email needs both.

Emails labelled `1-ToDelete` are **not moved automatically**. Option 2 asks for
confirmation before trashing — you stay in control every run. Only DELETE emails
are archived out of the inbox; ATTENTION and ERROR emails remain visible.

---

## Triage Labels

Defined in `LABEL_NAMES` at the top of `mailagent.py`:

| Key         | Gmail Label       | Icon | Inbox | Meaning |
|-------------|-------------------|------|-------|----------|
| `DELETE`    | `1-ToDelete`      | 🗑️   | Archived | Newsletters, marketing, shipping alerts, social media, expired promotions — see the stage table below for the exact rules |
| `ATTENTION` | `1-NeedAttention` | 👁️   | Kept  | Bills, deadlines, notices, valid promos, genuine personal correspondence — see the stage table below for the exact rules |
| `ERROR`     | `1-ProcessError`  | ⚙️   | Kept  | Either stage's LLM call failed, or the response couldn't be parsed / returned an invalid value — review manually |

Labels are created automatically on first run. The `1-` prefix makes them sort
to the top of your Gmail label list.

To add or rename categories, edit only the `LABEL_NAMES` dict — everything else
(prompt, validation, metrics, report) derives from it automatically.

### Stage 1 rules and stage 2 signals

The actual triage criteria live in `relevancy_prompt.py`, in the two prompt builders.
Quick reference:

| Stage | Checks | Outcome |
|-------|--------|---------|
| 1 — `build_stage1_prompt` | MONEY (bill/payment or account activity with a dollar amount), DEADLINE (unexpired appointment or reply needed), NOTICE (security/account/receipt/tax/legal), PROMO (unexpired offer) | `KEEP` → ATTENTION, `DISCARD` (generic marketing/newsletter/digest/expired promo/automated status) → DELETE, `UNSURE` → escalates to stage 2 |
| 2 — `build_stage2_prompt` | Gmail category lean, recency, addressed by name (`USER_NAME`), PERSON (real name in `From` vs. a `Reply-To` that points to a business/support/no-reply alias), sales-pitch tone | Judgment call → ATTENTION or DELETE |

Both prompt builders return `matched_rule`/`reason` text so every decision in the
Detailed Results report traces back to which rule or signal drove it.

**Email age is computed in code, not by the LLM.** `mailAgent.py` calculates
`days_old` from the parsed `Date` header and passes it into `build_stage1_prompt`
as a plain-language hint ("This email is N days old."). Smaller/cheaper models are
inconsistent at date arithmetic — subtracting a header timestamp from "today" — so
rather than trusting the LLM to work that out from raw dates, the app does the
subtraction itself and hands over the answer.

---

## Requirements

- Python 3.x
- [Ollama](https://ollama.com/) running locally with your chosen model pulled (see [LLM Settings](#llm-settings))
- Gmail API credentials (see [OAUTH_SETUP.md](OAUTH_SETUP.md))

Install Python dependencies:

```bash
pip install -r requirements.txt
---

## Configuration

All settings live in `config.py` and `.env`:

| Setting        | Description                                     |
|----------------|-------------------------------------------------|
| `EMAIL_ACCOUNT`| Gmail address to process                        |
| `USER_NAME`    | Your name — stage 2 checks whether an ambiguous email addresses you personally as a genuine-correspondence signal. Optional; that signal is skipped if unset. |
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

The agent prints one compact progress line per email as it's processed, then a
full Detailed Results breakdown, a performance report, and the post-batch menu:

```
[1/10] 07/29/26 [Promotions] "Citi Double Cash® Card" <citicards@e... | 📝 Grow your portfolio your way | ⏱ 4.5s
[2/10] 05/19/26 [Updates] "Amazon.com" <auto-confirm@amazon.com> | 📝 Ordered: "FUMAX Shower Door Hooks 10..." | ⏱ 26.9s
[3/10] 05/29/26 [Personal] billing@acme.com | 📝 Your invoice #4821 is ready | ⏱ 21.3s
...

========================================
      DETAILED RESULTS
========================================

──────────── 1-NeedAttention ────────────
[1/10] 05/29/26 [Personal] billing@acme.com
  📝 Your invoice #4821 is ready
  👁️ 1-NeedAttention | ⏱ 21.3s | 📊 Rel: 4/5
  💬 Invoice #4821 for $149.00 due June 5 with PDF attached.
  💡 Bill with a deadline requiring action — kept in inbox.
------------------------------------------------------------

──────────── 1-ProcessError ────────────
[4/10] 07/27/26 some-sender@example.com
  📝 A subject the model couldn't parse
  ⚙️ 1-ProcessError | ⏱ 18.2s | 📊 Rel: ?/5
  💡 Could not parse model response.
------------------------------------------------------------

──────────── 1-ToDelete ────────────
[5/10] 05/19/26 [Updates] "Amazon.com" <auto-confirm@amazon.com>
  📝 Ordered: "FUMAX Shower Door Hooks 10..."
  🗑️ 1-ToDelete | ⏱ 26.9s | 📊 Rel: 1/5
  💬 Amazon shipping confirmation for a non-actionable, already-delivered order.
  💡 Routine delivery confirmation with no deadlines or follow-up action needed.
------------------------------------------------------------
...

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
  R  Run another batch
  T  Move 6 marked email(s) to Trash
  A  Add a sender to contact list
  x  Exit

>
```

Message numbers in the Detailed Results section (and in menu options 3 and 4)
reflect this grouped/sorted display order — `1-NeedAttention` first, then
`1-ProcessError`, then `1-ToDelete`, sorted by relevance score within each
group — not the order emails were originally processed.

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
├── mailagent.py              # Main agent
├── config.py                 # Model and account settings
├── refresh_oauth_token.py   # OAuth token management
├── relevancy_prompt.py       # Triage prompt logic
├── requirements.txt          # Python dependencies
├── README.md                 # This file
├── OAUTH_SETUP.md            # Gmail OAuth setup guide
├── secrets/                  # Gitignored — all credentials here
│   ├── .env                  # EMAIL_ACCOUNT, OLLAMA_MODEL, OLLAMA_HOST
│   ├── credentials.json     # Google OAuth client ID/secret
│   ├── token.json           # Auto-generated OAuth token
│   └── contacts.yml          # Trusted senders (auto-created)
└── config/                   # Gitignored — user config
    └── labels.json          # Optional label mappings
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt --break-system-packages
```

### 2. Create secrets directory

```bash
mkdir secrets
```

### 3. Configure environment

Create `secrets/.env` with:

```
EMAIL_ACCOUNT=your-email@gmail.com
USER_NAME=Your Name
OLLAMA_MODEL=qwen2.5:3b-instruct
OLLAMA_HOST=http://localhost:11434
```

### 4. Set up Gmail OAuth

See [OAUTH_SETUP.md](OAUTH_SETUP.md) for the full Gmail OAuth 2.0 setup guide.

### 5. Run

```bash
python mailagent.py
```

On first run, it will open a browser for OAuth consent and create `secrets/token.json` automatically.
