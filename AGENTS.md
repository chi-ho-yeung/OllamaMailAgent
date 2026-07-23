# AGENTS.md — MailAgent

Quick orientation for LLM agents (e.g. Ornith) working in this repo. Read this
before making changes.

## What this project does

A CLI tool that triages a Gmail inbox using a **local** LLM via Ollama. It
pulls the oldest untagged inbox emails in batches of 10, asks the LLM for a
DELETE/ATTENTION decision per email, applies a Gmail label, then shows a
report + interactive menu (run another batch / trash marked emails / add
trusted sender / correct a label).

Core design principle: **the LLM does one bounded job** (classify this one
email) — it never drives control flow, chains tools, or decides what happens
next. All flow control is plain Python. Don't refactor this toward an
agentic/tool-calling pattern; that's an explicit non-goal (see README.md
"Approach" section for the rationale and the comparison project).

**The model's decision (DELETE/ATTENTION) is final — code must not
second-guess it using other fields the model returns.** `relevance_score`,
`sender_type`, `action_required`, etc. are for display/reporting only. Do not
add logic like "override to ATTENTION if relevance_score >= 4" or "force
DELETE if sender_type == 'newsletter' even though decision was ATTENTION" —
that would just be re-implementing triage in Python and defeats the point of
asking the model. The only decisions that bypass the model entirely are
pre-filters applied *before* the LLM is ever called, on facts the model never
gets asked about — currently just the trusted-sender check in
`load_contacts()`/`secrets/contacts.yml` (auto-ATTENTION, LLM skipped). Any
new pre-filter must work the same way: decide before the prompt is built, not
after the model responds. If you're asked to add a new "safety net" that
overrides the model's decision after the fact, push back — that's the pattern
this project deliberately avoids.

## File map

| File | Purpose |
|---|---|
| `mailAgent.py` | Entry point and main loop. Gmail fetch/label/trash, body extraction, Ollama call, post-batch menu. **Note capital A** — the file is `mailAgent.py`, not `mailagent.py`. |
| `relevancy_prompt.py` | All prompt-construction logic: the triage prompt template, Gmail-category hints, financial-detail detection (`BILL_KEYWORDS` for owed money vs. `ACCOUNT_ACTIVITY_KEYWORDS` for money that already moved — see gotchas below), valid decision list. Edit this file to change triage behavior/wording. |
| `config.py` | Loads `secrets/.env`, exposes `EMAIL_ACCOUNT`, `OLLAMA_MODEL`, `OLLAMA_HOST`, `MODEL_CONFIGS`/`DEFAULT_MODEL_CONFIG` (per-model Ollama options). No OAuth client-id/secret handling lives here — that's entirely `secrets/credentials.json` + `secrets/token.json` via `refresh_oauth_token.py`. Runs an Ollama connectivity check and prints status on import — importing this module has side effects (prints to stdout). |
| `refresh_oauth_token.py` | `get_gmail_service()` — loads/refreshes/creates the OAuth token, returns an authorized Gmail API client. Run standalone to (re)authenticate. |
| `requirements.txt` | Python deps: `beautifulsoup4`, `google-api-python-client`, `google-auth*`, `ollama`, `python-dotenv`, `pyyaml`. |
| `README.md` | Full project docs: goals, architecture rationale, label table, setup steps, usage example output, model benchmark table. |
| `OAUTH_SETUP.md` | Gmail OAuth setup guide (Google Cloud Console steps). |
| `secrets/` | **Gitignored.** Holds `.env`, `credentials.json`, `token.json`, `contacts.yml`. Never read/print/commit these; treat as opaque local state. |
| `config/labels.json` | Not present by default — user-created, gitignored (see `.gitignore`: `config/`). Maps label keys to Gmail label names. |

## How the pieces connect (call graph)

```
mailAgent.py: triage_and_label_emails()   [main entry, run via __main__]
  ├─ refresh_oauth_token.get_gmail_service()      → Gmail API client
  ├─ config.ollama_client.list()                   → verify Ollama is up
  ├─ get_or_create_label() × 3                     → ensure 1-ToDelete / 1-NeedAttention / 1-ProcessError exist
  ├─ fetch_untagged_emails()                       → oldest BATCH_SIZE=10 inbox msgs w/ no triage label
  └─ for each message:
       ├─ load_contacts() / trusted-sender short-circuit → ATTENTION w/o calling LLM
       ├─ clean_text() (BeautifulSoup)              → strip HTML to plain text
       ├─ relevancy_prompt.get_category_hint()       → Gmail category → hint string
       ├─ relevancy_prompt.get_financial_hint()      → detect $amount + bill OR account-activity keyword
       ├─ relevancy_prompt.build_triage_prompt()     → full prompt string
       ├─ config.ollama_client.chat(...)             → local LLM call (think=False, format=json)
       ├─ parse JSON response → decision/summary/reason/etc.
       └─ service.users().messages().modify(...)     → apply Gmail label
  └─ post-batch menu (1/2/3/4/x) → loops back into triage_and_label_emails() on "1"
```

## Key conventions / gotchas

- **Labels are the single source of truth** for "already processed." An email
  is skipped from future batches once it has any of the three
  `LABEL_NAMES` values applied. To reprocess an email, remove the label in Gmail.
- **`LABEL_NAMES` dict in `mailAgent.py` is the canonical label config** — the
  README says to derive everything from it. Don't hardcode label strings
  elsewhere.
- **Only `DELETE` and `ATTENTION` are valid LLM outputs** (`VALID_DECISIONS` in
  `relevancy_prompt.py`). `ERROR` is applied only in Python when the model is
  unreachable, returns bad JSON, or returns something outside `VALID_DECISIONS`
  — never ask the LLM to emit ERROR.
- **`think=False` must be a top-level kwarg to `ollama_client.chat()`**, not
  inside `options={}` — putting it in `options` silently no-ops. See the
  "Suppressing Thinking Mode" section in README.md for the full 3-part
  explanation (flag + temperature/top_p + regex strip of `<think>` as a safety net).
- **Per-model Ollama options live in `config.MODEL_CONFIGS`**, keyed by exact
  model string (e.g. `"qwen3.5:4b"`); anything not listed falls back to
  `DEFAULT_MODEL_CONFIG`. Add new models there, not inline in `mailAgent.py`.
- **Body extraction prefers plaintext but falls back to HTML** when the
  plaintext part is too short or lacks a dollar figure the HTML has (see
  `_has_dollar_amount` logic in `mailAgent.py`) — bank/invoice emails often
  ship a near-empty plaintext alternative with the real numbers only in HTML.
  Body is truncated to 1500 chars before prompting.
- **Trusted senders bypass the LLM entirely** (`secrets/contacts.yml`,
  loaded via `load_contacts()`), always resulting in ATTENTION.
- **A dollar amount alone isn't enough to trigger a financial hint — it must
  pair with a keyword.** `get_financial_hint()` checks two separate keyword
  sets: `BILL_KEYWORDS` (balance/due date/minimum payment — something owed)
  and `ACCOUNT_ACTIVITY_KEYWORDS` (transfer/deposit/withdrawal/Zelle/Venmo —
  money that already moved). Both force ATTENTION even with zero action
  required — completed account activity is ATTENTION-worthy purely because
  it's information about a real account event, not because it needs a reply.
  If you add new financial phrasing to detect, decide which bucket it
  belongs to (owed vs. already-happened) rather than lumping into one list.
- **DELETE removes the email from INBOX (archives it) immediately** when the
  label is applied — it is not yet trashed. Actual deletion (Trash) only
  happens via menu option 2, with a confirmation prompt. Don't change this
  two-step safety behavior without being asked.
- **Ctrl+C is handled via a `threading.Event` (`_stop`)**, not a raw
  `KeyboardInterrupt` catch everywhere — checked between emails/menu loops so
  the app stops cleanly rather than mid-operation.
- Corrections (menu option 4) currently only flip the Gmail label live; there
  is a documented but **not-yet-implemented** plan to persist flips to
  `corrections.yml` for future learning (see README.md § "get better the
  longer it runs" and the `TODO` comment in `mailAgent.py`). If asked to
  implement this, that section of the README is the spec to follow.

## Running / testing locally

- Requires Ollama running locally with a model pulled (default
  `qwen2.5:3b-instruct`) and a completed OAuth setup (`secrets/.env`,
  `secrets/credentials.json`; `secrets/token.json` is auto-generated).
- `pip install -r requirements.txt --break-system-packages` (or a venv).
- Run: `python mailAgent.py`. First run opens a browser for OAuth consent.
- `python refresh_oauth_token.py` re-runs auth standalone if the token is
  stale/deleted.
- `python config.py` prints a standalone config/auth summary (useful for
  sanity-checking `.env` without touching Gmail).
- There are no automated tests in this repo currently. Verify changes by
  running against a real (or test) Gmail inbox and checking printed output.

## When making changes

- Prompt/behavior tweaks → `relevancy_prompt.py`. Keep `build_triage_prompt`'s
  required JSON response shape in sync with the parsing code in `mailAgent.py`
  (`result.get(...)` calls) if you add/remove fields.
- New label categories → update `LABEL_NAMES` in `mailAgent.py`; everything
  else (label creation, report, menu) derives from that dict automatically —
  do not add parallel hardcoded label logic.
- New Ollama model support → add an entry to `MODEL_CONFIGS` in `config.py`.
- Keep `secrets/` untouched/unread unless the task specifically requires
  inspecting auth config — it's gitignored for a reason.
