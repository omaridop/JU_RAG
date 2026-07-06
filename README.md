# Bilingual RAG Assistant

A production-minded, bilingual (Arabic / English) Retrieval-Augmented Generation
assistant built for a university-scale support use case. It ingests official
documents, turns them into a searchable bilingual knowledge base, and answers
students' questions **grounded only in that knowledge** — newest information
first, always cited — with an optional live web-search fallback scoped to the
institution's official domain.

The whole system runs on a single LLM provider key (OpenRouter) for chat,
document reading, and embeddings, plus one search key. It ships with a full
Flask web app: a public streaming chatbot and a complete admin panel (documents,
analytics, cost tracking, roles, and audit).

> **Note on data.** This repository publishes the **engineering only**. The
> knowledge base is built from private institutional documents, so the vector
> store, database, uploaded files, and API keys are intentionally excluded from
> version control (see [`.gitignore`](.gitignore)). A small synthetic
> [`data/qa/sample_qa.json`](data/qa/sample_qa.json) is included purely to
> document the ingestion format. Point it at your own documents to run it.

---

## Why it's interesting

Most RAG demos stop at "embed some PDFs, do a cosine lookup, stuff the top-k into
a prompt." The harder problems show up at real scale, and this project is built
around them:

- **Bilingual retrieval that actually retrieves.** Arabic and English users
  phrase the same question a dozen different ways. Instead of relying on the
  embedding model to bridge that gap, ingestion expands every fact into an
  exhaustive bilingual Q&A bank (≥ 4 English + ≥ 4 Arabic paraphrases per fact,
  each embedded as its own vector, plus the answer text). Any phrasing, in either
  language, lands on the right fact.
- **Recency-aware retrieval.** University rules change every semester. Every
  record carries an *effective date*. Retrieval over-fetches by similarity, then
  re-ranks with a time-decay boost, so last year's fee schedule never outranks
  this year's.
- **A web-search fallback triggered by answer quality, not similarity.** Cosine
  similarity can't tell "topically related" from "actually answers the
  question." So when the knowledge base can't answer, the model *says so* in a
  detectable way ("this isn't in the provided material / contact the office / لا
  تتوفر"), and a punt-detector escalates the turn to a live search of the
  official site, then replaces the non-answer in the stream. This turned out to
  be far more reliable than gating on a similarity threshold.
- **Real cost accounting.** Per-call token cost is captured from the provider
  and attributed by model, operation, and document, with a monthly budget cap
  that auto-pauses chat.

---

## Architecture

```
             ┌──────────── INGEST (incremental, dated) ────────────┐
  PDF / DOCX ─▶│  reader model reads the file natively               │
  TXT / typed ─▶│  → writes bilingual Q&A (≥4 EN + ≥4 AR per fact)    │──▶  Chroma
  Q&A JSON    ─▶│  → embed every paraphrase + answer → upsert         │    (vectors)
             └─────────────────────────────────────────────────────┘        │
                                                                             │
  student ─▶ question ─▶ rate-limit ─▶ budget cap ─▶ memory cache? ──────────┘
                          (instant free hit)   │
                                               ▼
                     condense follow-up into a standalone query
                                               │
                                               ▼
                     recency-aware retrieval (deactivated docs excluded)
                                               │
                                    ┌──────────┴───────────┐
                                    ▼                      ▼
                          grounded, cited answer     answer punts?
                                                          │
                                                          ▼
                                          live search of official domain,
                                          replace the answer in the stream
```

### Pipeline stages

| Stage | Responsibility |
|-------|----------------|
| **Reading documents** | A vision-capable model reads PDF pages natively (OCR, tables, Arabic script) instead of relying on brittle text extraction; page-batched and concurrent so large files never truncate. |
| **Q&A expansion** | Each fact becomes a bilingual paraphrase set with a grounded answer and source page — the core retrieval trick. |
| **Embeddings** | Every paraphrase and answer is embedded independently into a persistent Chroma store. |
| **Retrieval** | Over-fetch by similarity → re-rank with a recency boost → dedupe → drop deactivated documents. |
| **Answering** | A reasoning chat model answers strictly from retrieved context, in the question's language, with citations and dates. Reasoning effort is tuned per task for latency. |
| **Web fallback** | Pluggable providers (Serper → Tavily → provider web plugin → DuckDuckGo), domain-scoped, used only when the KB punts or the student opts in. |

### Serving path highlights

- **Follow-up memory** — follow-ups are rewritten into standalone queries so
  context (which major, which fee, which program) carries across turns.
- **Promoted-answer cache** — admin-approved answers are served instantly
  without spending tokens, protected by a lexical guard so a one-word change
  can't mis-serve a cached answer.
- **Streaming UI** — Server-Sent Events; the web fallback emits a reset event to
  clear a punt before streaming the real answer.

---

## The admin panel

A complete bilingual (AR/EN) back office at `/admin`:

- **Documents** — upload PDF / Word / text / curated Q&A JSON with an effective
  date; activate/deactivate any document (deactivated = excluded from retrieval,
  recoverable); scoped per department.
- **Roles & audit** — a super admin manages admins and departments and sees a
  full audit trail (who / what / IP); department admins see only their own
  documents, enforced server-side.
- **Cost tracking** — real per-call cost from the provider, broken down by model
  / operation / document, with a live calculator, editable prices, and a monthly
  budget cap.
- **Analytics** — satisfaction (👍 / 👎), top questions, **knowledge gaps**
  (questions answered with no strong source → what to ingest next), AR/EN split,
  questions per day, and share served from cache.

---

## Engineering notes

- **Security** — CSRF protection, a strict Content-Security-Policy and security
  headers, hardened session cookies, per-IP rate limiting, role-gated routes,
  and password hashing.
- **Operability** — `/healthz`, rotating file logs, `ProxyFix` for real client
  IPs behind a reverse proxy, and cache-busted static assets.
- **Testing** — an offline `pytest` suite (no API key or network) covering
  language detection, recency ranking, dedupe, the memory guard, Q&A prep, token
  cost accounting, CSRF, headers, rate limiting, and `/healthz`.
- **Data safety** — ingestion is incremental and content-addressed (SHA-256), so
  the store is added to, never blindly rebuilt.

---

## Tech stack

Python 3.13 · Flask · OpenAI SDK (pointed at OpenRouter) · Chroma (local,
persistent vector DB) · PyMuPDF · python-docx · SQLite · httpx · tenacity ·
waitress · pytest.

| Concern | Default model |
|---------|---------------|
| Chat (answering) | a fast reasoning model |
| Document reading | a vision-capable model (native PDF) |
| Embeddings | a multilingual embedding model |

Every model is configurable in [`.env`](.env.example) — the code is not tied to
any single vendor's model IDs.

---

## Quick start

```bash
# 1. Create a virtualenv and install dependencies
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS / Linux

# 2. Configure (never commit the result)
cp .env.example .env
#   edit .env: set OPENROUTER_API_KEY and a strong ADMIN_PASSWORD

# 3. Verify the key + models work
python scripts/verify_api.py

# 4. (Optional) bulk-ingest your own files
#     PDFs/DOCX in data/uploads, curated Q&A JSON in data/qa
python scripts/ingest_cli.py

# 5. Run
python run.py
```

Open **http://127.0.0.1:5000** for the chat and **/admin** for the panel. The
first admin is seeded from `ADMIN_USERNAME` / `ADMIN_PASSWORD` as the super
admin — set a strong password before the first run. Get an OpenRouter key at
<https://openrouter.ai/keys>.

### Tests

```bash
python -m pytest -q
```

---

## Configuration

Every setting is documented in [`.env.example`](.env.example). A few that shape
behavior:

| Key | Meaning |
|-----|---------|
| `OPENROUTER_API_KEY` | **Required.** Chat, reading, and embeddings all run through it. |
| `CHAT_MODEL` / `CHUNK_MODEL` / `EMBED_MODEL` | Swap any model without touching code. |
| `TOP_K` / `CANDIDATE_K` | Context size vs. over-fetch pool for re-ranking. |
| `RECENCY_WEIGHT` / `RECENCY_HALFLIFE_DAYS` | How strongly newer content is favored. |
| `MEMORY_SIM_THRESHOLD` | Similarity required to reuse a cached answer. |
| `WEB_SEARCH_ENABLED` / `WEB_SEARCH_DOMAIN` | Enable the fallback; restrict it to one domain. |
| `CHAT_LIMIT_PER_SESSION` / `RATE_LIMIT_*` | Per-session and per-IP ceilings. |
| `MONTHLY_BUDGET_USD` | Spend cap that auto-pauses chat (also editable in admin). |
| `DEBUG` / `COOKIE_SECURE` / `TRUST_PROXY` | Flip appropriately for production. |

---

## Deployment

Run under a WSGI server behind a TLS-terminating reverse proxy:

```bash
waitress-serve --listen=0.0.0.0:8000 run:app
```

For production: generate a random `SECRET_KEY`, set `DEBUG=false`,
`COOKIE_SECURE=true`, and `TRUST_PROXY=true` (only behind your proxy), set a
`MONTHLY_BUDGET_USD` ceiling, keep `data/` on persistent storage and back it up,
and monitor `GET /healthz`.

---

## Project structure

```
├─ run.py                  # entry point
├─ requirements.txt        # pinned dependencies
├─ .env.example            # copy to .env and fill in
├─ app/
│  ├─ config.py            # env-driven settings
│  ├─ core/                # db (SQLite), auth + roles, LLM client, token accounting
│  ├─ pipeline/            # extract, read, chunk, Q&A expansion, embed, vectorstore, ingest
│  ├─ chatbot/             # prompts, memory cache, the RAG orchestrator, web search
│  └─ web/                 # Flask factory, security, rate limiting, routes, templates, static
├─ scripts/                # verify_api, ingest_cli, backfill_token_usage
├─ tests/                  # offline pytest suite
└─ data/                   # runtime: uploads / processed / chroma / qa  (gitignored)
```

---

## License

Released under the [MIT License](LICENSE).

*Built as a complete, production-minded reference implementation for bilingual,
recency-aware RAG.*
