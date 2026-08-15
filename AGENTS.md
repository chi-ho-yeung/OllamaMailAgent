# AGENTS.md — MailAgent

Orientation for LLM agents (e.g. Ornith) working in this repo. Read before making changes.

## What this project does

CLI tool that triages a Gmail inbox using a **local** LLM via Ollama. Pulls the
oldest untagged inbox emails in batches of 10, asks the LLM for a
DELETE/ATTENTION decision per email, applies a Gmail label, then shows a
report + interactive menu (run another batch / trash marked emails / add
trusted sender / correct a label).

**Non-goal: don't refactor toward agentic/tool-calling.** The LLM does one
bounded job (classify this email); Python owns all control flow. See
README.md "Approach" for rationale.

**The model's DELETE/ATTENTION decision is final.** Other returned fields
(`relevance_score`, `sender_type`, `action_required`, etc.) are display-only —
never write logic that overrides the decision based on them. The only
pre-model override is the trusted-sender check in `load_contacts()` /
`secrets/contacts.yml` (auto-ATTENTION, LLM skipped entirely, decided before
the prompt is built). New pre-filters must follow that same pattern. Push
back on any request to add a post-hoc override.

## File map

| File | Purpose |
|---|---|
| `mailAgent.py` | Entry point + main loop: Gmail fetch/label/trash, body extraction, Ollama call, post-batch menu. Note capital A. |
| `relevancy_prompt.py` | Prompt construction: triage template, Gmail-category hints, financial-detail keyword sets, valid decision list. Edit here to change triage behavior/wording. |
| `config.py` | Loads `secrets/.env`; exposes `EMAIL_ACCOUNT`, `OLLAMA_MODEL`, `OLLAMA_HOST`, `MODEL_CONFIGS`/`DEFAULT_MODEL_CONFIG`. No OAuth client-id/secret here (see `refresh_oauth_token.py`). Import has side effects: runs an Ollama connectivity check and prints status. |
| `refresh_oauth_token.py` | `get_gmail_service()` — loads/refreshes/creates OAuth token, returns authorized Gmail client. Run standalone to (re)authenticate. |
| `requirements.txt` | `beautifulsoup4`, `google-api-python-client`, `google-auth*`, `ollama`, `python-dotenv`, `pyyaml`. |
| `README.md` | Full docs: architecture rationale, label table, setup, usage example, model benchmarks. |
| `OAUTH_SETUP.md` | Gmail OAuth setup (Google Cloud Console steps). |
| `secrets/` | Gitignored: `.env`, `credentials.json`, `token.json`, `contacts.yml`. Never read/print/commit. |
| `config/labels.json` | Not present by default; user-created, gitignored. Maps label keys → Gmail label names. |

## Call graph

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
  ├─ relevancy_prompt.build_stage1_prompt()      → first-pass triage prompt
  ├─ relevancy_prompt.build_stage2_prompt()      → ambiguous-case judgment prompt
  ├─ config.ollama_client.chat(...)             → local LLM call (think=False, format=json)
  ├─ parse JSON response → decision/summary/reason/etc.
  └─ service.users().messages().modify(...)     → apply Gmail label
  └─ post-batch menu (R/T/A/L/x) → loops back into triage_and_label_emails() on "R"
```

## Key conventions / gotchas

- Labels are the source of truth for "already processed" — an email with any
  `LABEL_NAMES` value is skipped from future batches. Remove the label in
  Gmail to reprocess.
- `LABEL_NAMES` in `mailAgent.py` is canonical — never hardcode label strings
  elsewhere.
- Only `DELETE`/`ATTENTION` are valid LLM outputs (`VALID_DECISIONS`).
  `ERROR` is Python-only, applied when the model is unreachable/returns
  bad JSON/invalid value — never prompt the LLM to emit it.
- `think=False` must be a top-level kwarg to `ollama_client.chat()`, not
  inside `options={}` (silently no-ops there). Full rationale: README
  "Suppressing Thinking Mode."
- Per-model Ollama options live in `config.MODEL_CONFIGS`, keyed by exact
  model string; unlisted models fall back to `DEFAULT_MODEL_CONFIG`. Add new
  models there, not inline in `mailAgent.py`.
- Body extraction prefers plaintext, falls back to HTML when plaintext is too
  short or missing a dollar figure the HTML has (`_has_dollar_amount` in
  `mailAgent.py`). Truncated to 1500 chars before prompting.
- Trusted senders (`secrets/contacts.yml`, `load_contacts()`) always resolve
  to ATTENTION, bypassing the LLM.
- Financial signals are handled by the MONEY rule in Stage 1 (detects dollar amounts tied to bills or account activity) — this is now a prompt-based rule rather than a pre-prompt Python filter.
- DELETE archives immediately (removes from INBOX); actual Trash only via
  menu option 2 + confirmation. Don't collapse this two-step safety behavior.
- Ctrl+C uses `threading.Event` (`_stop`), checked between emails/menu loops
  — not a raw `KeyboardInterrupt` catch.
- Menu option 4 (corrections) only flips the label live today. Persisting to
  `corrections.yml` for future learning is planned but unimplemented — see
  README § "get better the longer it runs" + `TODO` in `mailAgent.py` if
  asked to build it.

## Running / testing locally

- Requires Ollama running locally with a model pulled (default
  `qwen2.5:3b-instruct`) and completed OAuth setup (`secrets/.env`,
  `secrets/credentials.json`; `secrets/token.json` auto-generated).
- `pip install -r requirements.txt --break-system-packages` (or a venv).
- Run: `python mailAgent.py`. First run opens a browser for OAuth consent.
- `python refresh_oauth_token.py` re-runs auth standalone if token is
  stale/deleted.
- `python config.py` prints a standalone config/auth summary without
  touching Gmail.
- No automated tests. Verify by running against a real/test inbox and
  checking printed output.

## When making changes

- Prompt/behavior tweaks → `relevancy_prompt.py`. Keep `build_triage_prompt`'s
  JSON response shape in sync with the parsing in `mailAgent.py`
  (`result.get(...)` calls).
- New label categories → update `LABEL_NAMES` in `mailAgent.py`; everything
  else derives from it automatically. No parallel hardcoded label logic.
- New Ollama model support → add to `MODEL_CONFIGS` in `config.py`.
- Leave `secrets/` untouched/unread unless the task specifically requires it.
