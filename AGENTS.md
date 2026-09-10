# AGENTS.md — MailAgent

Orientation for LLM agents working in this repo. Read before making changes.

## What this project does

CLI tool that triages a Gmail inbox using a **local** LLM via Ollama in batches. It analyzes emails, applies Gmail triage labels, archives non-relevant messages, and presents reports and interactive actions to review or process emails.

## File map

| File | Layer / Purpose |
|---|---|
| `mailAgent.py` | Main orchestrator & CLI runtime: fetches untagged messages, sanitizes email content, coordinates the two-stage LLM triage pipeline, applies Gmail label modifications, and drives the user interaction loop. Note capital A. |
| `relevancy_prompt.py` | Prompt engineering & classification schemas: Stage 1 objective triage prompt (`build_stage1_prompt`), Stage 2 subjective judgment prompt (`build_stage2_prompt`), category hint mapping, and valid verdict definitions. |
| `config.py` | Environment & model configuration: loads `.env`, manages Ollama host/model settings, and defines per-model Ollama parameters (`MODEL_CONFIGS`/`DEFAULT_MODEL_CONFIG`). Import has side effects: verifies Ollama connectivity and prints status. |
| `refresh_oauth_token.py` | Authentication layer: `get_gmail_service()` handles Gmail OAuth2 token retrieval, local browser consent, and auto-refresh. |
| `requirements.txt` | Python dependencies (`beautifulsoup4`, `google-api-python-client`, `google-auth*`, `ollama`, `python-dotenv`, `pyyaml`). |
| `README.md` | Full documentation: architecture rationale, two-stage triage design, setup instructions, and model benchmarks. |
| `OAUTH_SETUP.md` | Google Cloud Console OAuth setup walkthrough. |
| `secrets/` | Gitignored directory: `.env`, `credentials.json`, `token.json`, `contacts.yml`. Never read, print, or commit. |

## Call graph

```
mailAgent.py: triage_and_label_emails()   [main entry, run via __main__]
  ├─ refresh_oauth_token.get_gmail_service()      → Gmail API client
  ├─ config.ollama_client.list()                   → verify Ollama is up
  ├─ get_or_create_label()                         → ensure triage labels exist
  ├─ fetch_untagged_emails()                       → fetch batch of untagged inbox messages
  └─ for each message:
       ├─ contacts check (secrets/contacts.yml)    → route known contacts without LLM
       ├─ clean_text() (BeautifulSoup)             → strip HTML to plain text / sanitize
       ├─ relevancy_prompt.get_category_hint()     → Gmail category → hint string
       ├─ relevancy_prompt.build_stage1_prompt()   → Stage 1: objective rule check (NOTICE, PROMO, MONEY, etc.)
       ├─ config.ollama_client.chat(...)           → local LLM call
       ├─ [if UNSURE] build_stage2_prompt()        → Stage 2: subjective judgment (ATTENTION/DELETE)
       ├─ parse JSON response                      → decision/summary/reason/score
       └─ service.users().messages().modify(...)   → apply Gmail label (archive if DELETE)
```

## Key conventions & architecture rules

- **Labels are the source of truth**: Messages with any triage label defined in `LABEL_NAMES` are treated as processed and skipped in future batches.
- **`LABEL_NAMES` in `mailAgent.py` is canonical**: Central dictionary defines triage label names across the application. Never hardcode label strings elsewhere.
- **Two-stage triage pipeline**:
  - **Stage 1** (`build_stage1_prompt`): Fast, objective classifier matching messages against explicit rules (e.g. `NOTICE`, `PROMO`, `MONEY`, `DEADLINE`) or marking them `UNSURE`.
  - **Stage 2** (`build_stage2_prompt`): Only invoked for ambiguous survivors (`UNSURE`) to weigh qualitative signals (sender identity vs `Reply-To`, personal address, sales tone).
- **LLM classification vs. Code decision-making**:
  - The LLM's role is strictly to classify messages, evaluate relevance, and extract signals (e.g. matched rules like `NOTICE`, `PROMO`, `MONEY` in Stage 1, or subjective signal assessment in Stage 2).
  - The Python orchestrator makes the final decision on what action to take (e.g. mapping classifications to `ATTENTION` or `DELETE`, applying labels, and archiving).
  - `ERROR` is Python-only, applied when the model is unreachable or returns invalid/unparseable JSON. Never prompt the LLM to emit `ERROR`.
- **Model configuration**:
  - Per-model options live in `config.MODEL_CONFIGS`, falling back to `DEFAULT_MODEL_CONFIG`.
  - Top-level parameters for Ollama (such as `format` and `think`) are extracted and passed as top-level kwargs to `ollama_client.chat()`.
- **Safety & archiving**:
  - Marking `DELETE` archives the email immediately (removes from `INBOX`). Trashing emails is a separate, user-confirmed action.
- **Cancellation**:
  - Ctrl+C handling uses a `threading.Event` (`_stop`) checked between processing steps rather than raw unhandled `KeyboardInterrupt` crashes.

## Running locally

- Requires Ollama running locally with a model pulled and completed OAuth setup (`secrets/.env`, `secrets/credentials.json`; `secrets/token.json` auto-generated).
- Install dependencies: `pip install -r requirements.txt` (or within a venv).
- Run: `python mailAgent.py`. First run opens a browser for OAuth consent.
- `python refresh_oauth_token.py` re-runs auth standalone if token is stale/deleted.
- `python config.py` prints a standalone config/auth summary without touching Gmail.

## When making changes

- **Prompt & triage behavior tweaks** → `relevancy_prompt.py`. Keep prompt JSON schemas and `mailAgent.py` response parsing in sync.
- **Triage label categories** → Update `LABEL_NAMES` in `mailAgent.py`.
- **Ollama model options** → Add or update `MODEL_CONFIGS` in `config.py`.
- **Sensitive data** → Leave `secrets/` untouched/unread unless specifically required.
