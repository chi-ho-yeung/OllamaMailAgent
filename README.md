# MailAgent — Email Triage Agent

Triages your Gmail inbox with a **local decision model** served by Ollama.
Runs in batches, labels emails, archives likely-deletes, and lets you move them to Trash after review.

---

## Project Goals

A proof of concept for useful AI agents on a mid-range laptop (16 GB RAM, ~2 GB VRAM): no cloud API, no GPU cluster, just a local Ollama server and a small decision model.

**Deterministic function call, not an agent.** The model is called for one bounded job — answer typed questions about this email — and Python controls all flow. That makes it fast, predictable and easy to debug on constrained hardware.

### Why a decision model, and why one stage

A decision model doesn't write text. You send one `state` (the email) plus typed questions
(`choice`, yes/no, `score`) to Ollama's `/v1/systemone` compatible endpoint, and it returns a probability for
every option in a single forward pass. This replaced an earlier chat-model design that needed two
LLM calls (an objective rule check, then a subjective judgment on the ambiguous leftovers) because small chat models
broke down when asked to apply rules *and* arbitrate between them in one prompt.

Decision models make that split unnecessary:

- **Several narrow questions, one request.** Questions are answered independently and the email is processed once, so
  "which rule matches?", "is it safe to delete?" and "is it a real person?" cost about one call.
- **Probabilities replace `UNSURE`.** Instead of escalating ambiguous mail to a second model, Python applies confidence
  thresholds. Anything not confidently deletable stays in your inbox.
- **Code arbitrates.** Weighing conflicting signals happens in `decide_action()`, where it is testable and tunable, not inside a prompt.

Only decision models are supported. A chat model loaded in Ollama makes the request return an error or 501 and the app refuses to start.

---

## How It Works

```
1. Connect to Gmail API (OAuth 2.0)
2. Probe Ollama: server up AND serving a decision model (also warms it up)
3. Create triage labels in Gmail if they don't exist yet
4. Find the 10 oldest inbox emails not yet labelled by this agent
5. For each email:
     - Sender in contacts.yml? → label directly, no model call
     - Code computes the checkable facts: email age, unsubscribe/bulk headers, no-reply sender,
       Reply-To on a different domain than From. They go into the state as plain sentences.
     - Build one state (From, Reply-To, date, age, Gmail category, facts, subject, body with URLs
       replaced by <domain/link>) and ask four narrow yes/no questions in ONE /v1/systemone request:
         record     hard keep: bill, payment, receipt, security alert, or official notice?
         bulk       sent automatically or in bulk, not written for this recipient?
         actionable soft keep: must the recipient pay, reply, attend, or keep it as a record?
         personal   hard keep: written by a real person specifically to the recipient?
       Each extra question adds noticeable time on a small laptop, so the list is kept short.
       "Safe to delete" is deliberately not one compound question: it is computed in decide_action().
     - decide_action() leans toward recommending delete (you review before trashing), checking keep-signals first:
         record p >= HARD_KEEP_MIN                                          -> ATTENTION
         personal p >= HARD_KEEP_MIN (and Reply-To not mismatched)          -> ATTENTION
         actionable p >= SOFT_KEEP_MIN                                      -> ATTENTION
         bulk p >= DELETE_MIN_PROB                                          -> DELETE (archived)
           (lower bar, DELETE_MIN_PROB_BULK_HEADER, when code sees an unsubscribe/bulk
            header or an automated sender such as noreply@ or editors-noreply@)
         otherwise                                                          -> ATTENTION
       Server down / 501 / malformed answer                                → ERROR (stays in INBOX)
6. Print Detailed Results (grouped ATTENTION → ERROR → DELETE) and a batch performance report
7. Post-batch menu: R run another batch · T trash marked emails (confirmed) · A add sender to contacts · M message details · x exit
```

DELETE emails are only **archived**; trashing is a separate, confirmed action (option T). Every result's reason includes the
probabilities (`[record=0.02, bulk=0.61, actionable=0.03, personal=0.10]`) so you can see why and tune the thresholds.
Decision models write no prose, so there is no per-email summary.

### Manual Review & Correction

Option M opens a message: **L** flips between `1-ToDelete` and `1-NeedAttention`, **C** moves it to any Gmail label
(or a new one) and archives it. Option A adds the sender to `secrets/contacts.yml` so future mail skips the model.

---

## Triage Labels

Defined in `LABEL_NAMES` at the top of `mailAgent.py`:

| Key | Gmail Label | Inbox | Meaning |
|---|---|---|---|
| `DELETE` | `1-ToDelete` | Archived | Confidently safe to delete |
| `ATTENTION` | `1-NeedAttention` | Kept | Matches a keep-rule, looks personal, or the model wasn't confident enough to delete |
| `ERROR` | `1-ProcessError` | Kept | Server unreachable, not a decision model, or unparseable answer — review manually |

Labels are created automatically on first run.

---

## Requirements

- Python 3.x and `pip install -r requirements.txt`
- [Ollama](https://ollama.com/) installed and running
- A decision model (e.g., `d3:2b` or `d3:4b`)
- Gmail API credentials ([OAUTH_SETUP.md](OAUTH_SETUP.md))

### Serving a model

```bash
ollama pull d3:2b
```

Decision models like `d3:2b` are optimized for these typed questions. Context is small, so `MAX_BODY_CHARS` (default 1500) caps the body sent.
If the server rejects long inputs, lower `MAX_BODY_CHARS`.

---

## Configuration

`secrets/.env`:

```
EMAIL_ACCOUNT=your-email@gmail.com
USER_NAME=Your Name                # optional: lets the model see who the recipient is
OLLAMA_HOST=http://127.0.0.1:11434   # Ollama server URL
OLLAMA_MODEL=d3:2b                 # The decision model to use
```

Tuning (all optional, in `.env`):

| Setting | Default | Meaning |
|---|---|---|
| `HARD_KEEP_MIN` | `0.50` | record / personal probability at or above which the email is kept |
| `SOFT_KEEP_MIN` | `0.80` | actionable probability at or above which the email is kept (higher bar: it is vague and fires on ordinary marketing) |
| `DELETE_MIN_PROB` | `0.50` | "Sent in bulk/automatically" probability needed to recommend delete |
| `DELETE_MIN_PROB_BULK_HEADER` | `0.35` | Same, but used when the email has an unsubscribe/bulk header or an automated sender |
| `MAX_BODY_CHARS` | `1500` | Body characters sent to the model |
| `REQUEST_TIMEOUT` | `90` | Seconds per request |

**Calibrate per model.** Probabilities aren't comparable across models. Run a batch, read the probabilities in Detailed Results, and adjust `DELETE_MIN_PROB` and `SOFT_KEEP_MIN` on your own inbox. Defaults lean toward recommending delete because every delete is reviewed before trashing.
Email age is computed in code and passed in the state, because small models are unreliable at date arithmetic.

Batch size is `BATCH_SIZE = 10` in `mailAgent.py`.

---

## Usage

```bash
python mailAgent.py
```

Prints one progress line per email, then Detailed Results, a performance report and the menu. Ctrl+C stops cleanly between emails.

## File Structure

```
mailagent/
├── mailAgent.py          # Orchestrator & CLI
├── system_one.py         # /v1/systemone client (via Ollama)
├── relevancy_prompt.py   # Questions, state builder, decide_action()
├── config.py             # Env config + thresholds
├── refresh_oauth_token.py
├── requirements.txt
├── OAUTH_SETUP.md
└── secrets/              # Gitignored: .env, credentials.json, token.json, contacts.yml
```

## Roadmap

The current version is stateless. Future iterations could track corrections (emails you un-delete or re-label), build a
per-sender profile, and surface patterns ("you always delete this sender") — converging on your habits without sending data to the cloud.
