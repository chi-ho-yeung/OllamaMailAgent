# AGENTS.md — MailAgent

Orientation for LLM agents working in this repo. Read before making changes.

## What this project does

CLI tool that triages a Gmail inbox using a **local decision model** served by llama.cpp (`POST /v1/systemone`). One request per email: the model answers typed questions with probabilities, Python decides, the tool applies Gmail labels (archiving likely-deletes) and offers an interactive review menu.

**Decision models only.** Chat/text models are not supported: the server returns 501 on `/v1/systemone` for them and the app refuses to start. Do not reintroduce `/v1/chat/completions`, prompt-the-model-for-a-word flows, or JSON-parsing of model prose.

## File map

| File | Purpose |
|---|---|
| `mailAgent.py` | Orchestrator & CLI: fetches untagged messages, sanitizes content, calls the decision model once per email, applies labels, runs the review menu. Note capital A. |
| `system_one.py` | `/v1/systemone` client: `decide(state, questions)` → `answers` dict; `check_server()` probe; `DecisionError`. |
| `relevancy_prompt.py` | The `QUESTIONS` (record / bulk / actionable / personal), `email_signals()` (code-side facts: unsubscribe header, no-reply sender, Reply-To mismatch), `build_state()`, and `decide_action()` which maps probabilities to ATTENTION/DELETE. |
| `config.py` | Env config + tuning thresholds. No import-time side effects. |
| `refresh_oauth_token.py` | `get_gmail_service()` — Gmail OAuth2. |
| `secrets/` | Gitignored: `.env`, `credentials.json`, `token.json`, `contacts.yml`. Never read, print, or commit. |

## Call graph

```
mailAgent.triage_and_label_emails()
  ├─ get_gmail_service()
  ├─ system_one.check_server()            → probe: server up AND a decision model
  ├─ get_or_create_label(), fetch_untagged_emails()
  └─ per message:
       ├─ contacts.yml match → label directly, no model call
       ├─ clean_text() → body[:MAX_BODY_CHARS]
       ├─ relevancy_prompt.build_state()  → one plain-text state (age computed in code)
       ├─ system_one.decide(state, QUESTIONS)   → answers (probabilities)
       ├─ relevancy_prompt.decide_action(answers) → (decision, reason, details)
       └─ messages.modify(): apply label (archive if DELETE)
```

## Conventions

- **Model supplies probabilities; code decides.** Thresholds live in `config.py` (`HARD_KEEP_MIN`, `SOFT_KEEP_MIN`, `DELETE_MIN_PROB`, `DELETE_MIN_PROB_BULK_HEADER`). Hard keep-signals: record/personal; soft: actionable. DELETE = `bulk` above its (header-adjusted) threshold and no keep-signal; the user reviews every DELETE, so leaning toward delete is intended. Never add a compound "should I delete this?" question; keep questions single-concept and combine in `decide_action()`.
- **Do checkable things in code**, not in prompts (age, headers, domain comparison), and pass them as facts via `email_signals()`.
- **Decision models write no prose.** `summary` is always empty; `reason` is generated in code from the answers.
- **`ERROR` is Python-only**: server unreachable, 501, or malformed answer. Labelled `1-ProcessError`, stays in INBOX.
- **Labels are the source of truth**: messages carrying any label in `LABEL_NAMES` are skipped. `LABEL_NAMES` in `mailAgent.py` is canonical; never hardcode label strings elsewhere.
- **Safety**: DELETE only archives. Trashing is a separate, user-confirmed action. ATTENTION (including contact rules) stays in INBOX.
- **Cancellation**: Ctrl+C sets a `threading.Event` (`_stop`) checked between steps.
- **Adding a signal**: add a question to `QUESTIONS`, read it in `decide_action()`. Questions are batched in one request and the state is processed once, but each extra question adds noticeable time on this hardware (7 questions ~30 s/email vs 3 ~6 s), so only add one if it changes decisions.

## Running locally

- Serve a decision model: `llama serve -hf ggml-org/Kev-4B-GGUF` (needs a llama.cpp build with `/v1/systemone`; run `llama update`).
- `pip install -r requirements.txt`, then `python mailAgent.py` (first run opens a browser for OAuth).
- `python config.py` prints a config summary without touching Gmail.

## When making changes

- Triage behavior → `QUESTIONS` / `decide_action()` in `relevancy_prompt.py`, thresholds in `config.py`.
- Backend/protocol → `system_one.py`.
- Labels → `LABEL_NAMES` in `mailAgent.py`.
- Leave `secrets/` untouched.
