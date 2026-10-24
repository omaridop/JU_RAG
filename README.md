# UJ Assistant

A university support application combining Retrieval-Augmented Generation (RAG),
streaming chat, and a hands-free AI voice assistant. It turns documents into a
searchable Arabic/English knowledge base, retrieves relevant information with a
recency-aware ranking, and can fall back to searches of the institution's official
website. Written responses include source references for verification.

Chat, document reading, embeddings, speech recognition, and speech synthesis use
OpenRouter. Search providers such as Serper and Tavily use separate credentials.
The Flask application includes a student-facing interface and a bilingual admin
panel for documents, departments, analytics, usage costs, and access control.

> **Note on data.** This repository publishes the **engineering only**. The
> knowledge base is built from private institutional documents, so the vector
> store, database, uploaded files, and API keys are intentionally excluded from
> version control (see [`.gitignore`](.gitignore)). A small synthetic
> [`data/qa/sample_qa.json`](data/qa/sample_qa.json) is included purely to
> document the ingestion format. Point it at your own documents to run it.

---

## Design decisions

The implementation focuses on retrieval quality, changing source material,
operational controls, and accessible interaction:

- **Bilingual Q&A retrieval.** Arabic and English users
  phrase the same question a dozen different ways. Instead of relying on the
  embedding model to bridge that gap, ingestion expands every fact into an
  exhaustive bilingual Q&A bank (≥ 4 English + ≥ 4 Arabic paraphrases per fact,
  each embedded as its own vector, plus the answer text). This increases the
  range of question phrasings represented in the index.
- **Recency-aware retrieval.** University rules change every semester. Every
  record carries an *effective date*. Retrieval over-fetches by similarity, then
  re-ranks with a time-decay boost to favor recent material alongside relevance.
- **A web-search fallback triggered by answer quality, not similarity.** Cosine
  similarity can't tell "topically related" from "actually answers the
  question." So when the knowledge base can't answer, the model *says so* in a
  detectable way ("this isn't in the provided material / contact the office / لا
  تتوفر"), and a punt-detector escalates the turn to a live search of the
  official site, then replaces the non-answer in the stream. Detection is
  heuristic, not a guarantee that every incomplete answer will be caught.
- **Voice without a separate knowledge pipeline.** Transcribed questions use the
  same retrieval and web-fallback path as text chat. Speech is generated only
  after the final answer is available, avoiding narration of a discarded draft.
- **Usage accounting.** Provider-reported costs are recorded where available;
  speech costs use explicit estimates when needed. Usage is attributed by model
  and operation, with monthly budget checks across chat and audio requests.

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
| **Reading documents** | A vision-capable model reads PDF pages natively, with page batching and concurrency controls. Curated Q&A JSON bypasses the reader model. |
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
  is less likely to produce an incorrect cache hit. Voice mode bypasses direct
  cached answers so replies can follow its short, conversational persona.
- **Streaming UI** — Server-Sent Events; the web fallback emits a reset event to
  clear a punt before streaming the real answer.

---

## Voice assistant

The waveform button opens a dedicated audio-only conversation with **أحمد الزعبي
(Ahmad Al-Zoubi)**, explicitly presented as an AI university helper. Arabic replies
use a friendly Jordanian style, usually one or two sentences. The voice interface
does not display question or answer transcripts.

### How a conversation works

1. Start once and grant microphone permission.
2. Speak normally. A browser audio-level detector sends the question after
   approximately 850 ms of quiet following sustained speech.
3. ElevenLabs Scribe v2 transcribes the recording through OpenRouter.
4. The RAG pipeline produces a brief answer, with a 320-token generation ceiling.
5. Fish Audio Drama 3 reads the answer aloud. Listening resumes automatically.

```text
Microphone -> turn detection -> Scribe v2 -> RAG / web fallback
     ^                                            |
     +--- resume listening <- playback <- Drama 3 -+
```

The green orb and waveform react to actual microphone and playback levels.
Spring-smoothed motion, keyboard-accessible controls, and reduced-motion support
keep the interface responsive without requiring a visible transcript. Tap the
orb to interrupt a reply or cancel a pending question; mute and end-call remain
available throughout the conversation.

**This is automatic half-duplex voice, not native realtime speech-to-speech.**
The microphone is disabled while the answer is processing or playing. There is
no wake word or spoken barge-in during playback; interruption is by tap. Separate
transcription, answer, and synthesis requests introduce latency, and web searches
can add more. Audio-level turn detection is a lightweight heuristic and should be
tested with the microphones and background noise of the intended environment.

### Read-aloud for written answers

Saved text answers also have a read-aloud control. This is separate from the
hands-free assistant:

- Arabic narration can use ElevenLabs or either configured Fish voice, selected
  in admin settings. English and Swedish have their own ElevenLabs voices.
- A separate AI preparation step adds context-aware Arabic pronunciation marks
  (tashkeel). Its output is checked for wording changes, with a fallback to the
  original text if validation fails.
- Hands-free mode instead asks the answer model to add useful pronunciation marks
  while generating the reply, avoiding an additional preparation request.
- Ahmad's conversational voice is pinned to Fish Drama 3 voice
  `4cb8371162134edeb4915672ffc66de6`, independently of read-aloud settings.

### Audio privacy and safeguards

Microphone recordings are forwarded in memory to OpenRouter and the transcription
provider; the app does not write them to disk. Text transcripts are retained in
chat logs, and generated narration may be cached temporarily in server memory.
Provider-side retention is governed by the providers' policies.

Audio requests have session checks, upload-size limits, rate limiting, and budget
checks; the transcription endpoint also validates a CSRF token. Recording is
bounded to 30 seconds per turn. Silence is rotated locally rather than uploaded.
Mute, end-call, leaving the tab, or 60 seconds without speech releases the
microphone. Cancelling a browser request cannot undo costs already incurred upstream.

See [voice implementation notes](docs/voice-mode.md) for the request flow and
testing limitations.

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
  budget cap. Audio operations are included, with estimates where applicable.
- **Speech settings** — select the Arabic read-aloud provider and voice without
  restarting the app. Hands-free Ahmad retains his dedicated Fish voice.
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
- **Testing** — an offline `pytest` suite (no provider calls) covering
  language detection, recency ranking, dedupe, the memory guard, Q&A prep, token
  cost accounting, CSRF, headers, rate limiting, speech routing, transcription,
  and `/healthz`. Node tests simulate voice turn-taking, interruption, microphone
  cleanup, silence handling, and cancellation races.
- **Data safety** — ingestion is incremental and content-addressed (SHA-256), so
  the store is added to, never blindly rebuilt.

---

## Tech stack

Python 3.13 · Flask · OpenAI SDK (pointed at OpenRouter) · Chroma (local,
persistent vector DB) · PyMuPDF · python-docx · SQLite · httpx · tenacity ·
waitress · pytest. The voice client uses MediaRecorder and the Web Audio API in
plain JavaScript; its regression tests use Node's built-in test runner.

| Concern | Default model |
|---------|---------------|
| Chat (answering) | DeepSeek; selected through `CHAT_MODEL` |
| Document reading | `google/gemini-2.5-flash` |
| Embeddings | `google/gemini-embedding-2` |
| Speech recognition (STT) | `elevenlabs/scribe-v2` |
| Hands-free speech (TTS) | `fish-audio/drama-3` with Ahmad's pinned voice |
| Read-aloud speech (TTS) | `elevenlabs/eleven-v4-turbo` or configured Fish voices |
| Arabic narration preparation | `TTS_DIACRITICS_MODEL` |

Chat, document, embedding, and read-aloud settings are configured through
[`.env.example`](.env.example). The hands-free transcription model and Ahmad's
speech model/voice are pinned in `transcription.py` and `speech.py`; they are not
overridden by the read-aloud controls. Provider access and model availability must
be checked against the OpenRouter account used to run the app.

---

## Quick start

Windows PowerShell:

```powershell
# 1. Create a virtualenv and install dependencies
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. Configure (never commit the result)
Copy-Item .env.example .env
#   edit .env: set OPENROUTER_API_KEY and a strong ADMIN_PASSWORD

# 3. Verify the key + models work
.\.venv\Scripts\python.exe scripts/verify_api.py

# 4. (Optional) bulk-ingest your own files
#     PDFs/DOCX in data/uploads, curated Q&A JSON in data/qa
# .\.venv\Scripts\python.exe scripts/ingest_cli.py

# 5. Run
.\.venv\Scripts\python.exe run.py
```

On macOS/Linux, use `.venv/bin/python` instead, and copy the configuration with
`cp .env.example .env`.

Open **http://127.0.0.1:5000** for the chat and **/admin** for the panel. The
first admin is seeded from `ADMIN_USERNAME` / `ADMIN_PASSWORD` as the super
admin — set a strong password before the first run. Get an OpenRouter key at
<https://openrouter.ai/keys>.

Do not run CLI ingestion against the same Chroma directory while the app is
running; the store is single-process.

### Enable voice

Keep `TTS_ENABLED=true` and configure `OPENROUTER_API_KEY` with access to Scribe v2
and Fish Drama 3. No separate ElevenLabs or Fish API key is used by this integration.
Microphone access requires HTTPS or localhost and explicit browser permission.
Open the waveform button, start the conversation, and allow microphone access.
Test input sensitivity and playback on the target device before relying on it.

### Tests

Use a disposable checkout or isolated data paths for backend tests: the Flask
tests create an app and must not share a live SQLite/Chroma directory. For example,
run the following Python code with the project's virtual environment:

```python
import tempfile
from pathlib import Path
from app.config import settings

root = Path(tempfile.mkdtemp(prefix="uj-tests-"))
for name, value in {
    "DATA_DIR": root,
    "DB_PATH": root / "app.db",
    "CHROMA_DIR": root / "chroma",
    "UPLOAD_DIR": root / "uploads",
    "PROCESSED_DIR": root / "processed",
    "QA_DIR": root / "qa",
}.items():
    setattr(settings, name, value)
settings.ADMIN_USERNAME = "test-admin"
settings.ADMIN_PASSWORD = "test-only-password"
import pytest
raise SystemExit(pytest.main(["-q"]))
```

```bash
node --test tests/voice_client.test.cjs
```

The checked-in suites currently contain 85 Python tests and 10 voice-client tests.
Synthetic audio/device tests do not replace real microphone testing.

---

## Configuration

Every setting is documented in [`.env.example`](.env.example). A few that shape
behavior:

| Key | Meaning |
|-----|---------|
| `OPENROUTER_API_KEY` | **Required.** Chat, reading, embeddings, STT and TTS use it. |
| `CHAT_MODEL` / `CHUNK_MODEL` / `EMBED_MODEL` | Swap any model without touching code. |
| `TOP_K` / `CANDIDATE_K` | Context size vs. over-fetch pool for re-ranking. |
| `RECENCY_WEIGHT` / `RECENCY_HALFLIFE_DAYS` | How strongly newer content is favored. |
| `MEMORY_SIM_THRESHOLD` | Similarity required to reuse a cached answer. |
| `WEB_SEARCH_ENABLED` / `WEB_SEARCH_DOMAIN` | Make web search available; restrict it to configured domains. The admin `web_search_enabled` switch must also be on. |
| `CHAT_REASONING_EFFORT` / `CONDENSE_REASONING_EFFORT` / `WEB_AGENT_REASONING_EFFORT` | Separate reasoning budgets for answering, follow-ups, and search tools. |
| `TTS_ENABLED` | Enable audio endpoints for read-aloud and hands-free conversations. |
| `TTS_MODEL` / `TTS_VOICE*` / `TTS_FISH_*` | Read-aloud models, language-specific voices, and billing estimates; not Ahmad's pinned voice. |
| `TTS_DIACRITICS_MODEL` | Model used to prepare Arabic narration and classify non-Arabic read-aloud text. |
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
│  ├─ chatbot/             # RAG, prompts, memory, web search, speech, transcription
│  └─ web/                 # Flask factory, security, rate limiting, routes, templates, static
├─ scripts/                # verify_api, ingest_cli, backfill_token_usage
├─ docs/                   # PDF-to-Q&A prompt, voice behavior, local hosting notes
├─ tests/                  # Python backend tests + Node voice-controller tests
└─ data/                   # runtime: uploads / processed / chroma / qa  (gitignored)
```

---

## License

Released under the [MIT License](LICENSE).

Built by Hani Muhannad Alshammari.
