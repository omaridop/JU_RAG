# Local Cloudflare hosting

The public origin runs with Waitress on `127.0.0.1:5000`. Both
`kasitbot.app` and `www.kasitbot.app` point to this origin through the existing
Cloudflare tunnel. The PC must remain powered on and connected to the internet.

`scripts/serve_public.py` supervises the origin, reads the local `.env` for each
restart, and enforces secure cookies, proxy awareness, and debug-off settings.
It checks port 5000 before launching and uses a Windows lock to prevent duplicate
supervisors. Logs are in the ignored `logs/public-server.log` file.

A per-user Windows Startup entry, `JU Assistant.vbs`, starts the supervisor after
sign-in. This is a user-session startup arrangement, not a Windows service that
runs before login. The existing Cloudflare connector has its own supervisor.

## Updating credentials

Edit the ignored `.env` locally. Stop only the process listening on port 5000;
the supervisor starts a new process with the updated configuration. Never run a
second application process against the same Chroma directory. Do not paste
credentials into source files or commit them.

## Verification

- `https://kasitbot.app/healthz` must return HTTP 200.
- Check `/admin/login` and the chat page through HTTPS.
- Test `/api/chat` as well: a health check does not confirm model availability.
- Test `/api/chat/stream` through the public domain: it must produce delta and
  done events. Waitress must manage connection headers; do not add a
  `Connection` header to the Flask streaming response.

## Read aloud

Completed RAG, memory, and web answers have a bilingual Listen button. It uses
`elevenlabs/eleven-v4-turbo` and the selected ElevenLabs Voice ID
`4wf10lgibMnboGJGCLrP` through the existing OpenRouter key. Voice availability
was verified with a real MP3 request; accent quality still needs human review.
The original answer wording is retained, without Markdown or URL targets.
Before Arabic narration, DeepSeek (configurable via `TTS_DIACRITICS_MODEL`,
defaulting to `CHAT_MODEL`) adds natural pronunciation diacritics to the spoken
copy only. The displayed answer is unchanged. A strict comparison rejects any
changed words, numbers or punctuation; timeout, truncation or invalid output
falls back to the original text. English-only answers skip this step. Its token
cost is recorded separately as Speech diacritics. Cached replays skip both calls.
This may improve pronunciation, but is not a guarantee of a particular accent.

English replies use `TTS_VOICE_EN=XZXyuzPdidXaSa10PmVA`; Swedish replies use
`TTS_VOICE_SV=aSLKtNoVBZlxQEMsnGL2`. Selection follows the predominant language
of the saved answer, not the interface language. Arabic is detected locally;
other text is classified by DeepSeek (up to 10 seconds), with cost recorded as
Speech language detection. If classification fails, the request fails safely
instead of silently selecting the wrong voice. Mixed replies use one voice for
their predominant language. Unsupported languages currently select English.
Only Arabic-dominant replies receive the diacritics step. Cached replays skip
classification, and changing a configured voice invalidates cached audio.

### Arabic provider switch

The additional **Male Jordanian AI · Drama 3** option (`fish_male`) uses voice
`4cb8371162134edeb4915672ffc66de6`, configurable as `TTS_FISH_MALE_VOICE`.
It was enabled on 2026-10-22 at the user's request. The previous Fish voice and
ElevenLabs remain selectable. This user-supplied voice label is not an
independent verification of its accent; Arabic tashkeel and byte billing apply
to both Fish options, with separate audio cache keys.

In **Admin → Settings → Arabic voice**, select **ElevenLabs · Eleven v4 Turbo**
or **Fish Audio · Drama 3**, then save. This login/CSRF-protected, audited setting
(`tts_ar_provider`) is read live; no restart is needed. ElevenLabs remains the
default. English and Swedish are unaffected. Refresh an already-open chat to
discard browser-cached audio when comparing providers.

Fish uses `fish-audio/drama-3` and voice
`21b77f5a415a41ceab055b43fcd2c348`. Configure overrides through
`TTS_FISH_MODEL`, `TTS_FISH_VOICE`, and `TTS_FISH_USD_PER_BYTE`. The verified
2026-10-21 rate is $10 per million UTF-8 input bytes ($0.00001/byte), not per
character; Arabic letters and diacritics therefore count as multiple bytes.
Speech cost estimates use the selected provider's billing unit.

Arabic preparation infers register/dialect from vocabulary, grammar and sentence
context in the same DeepSeek call used for tashkeel. It preserves formal Arabic
as formal Arabic, retains dialect words, and uses neutral standard pronunciation
when uncertain. It does not infer dialect from the university name or voice.
Only vowel marks/whitespace may change; altered words and numbers are rejected.
Validated preparations are cached separately (128 entries, 30 minutes), so
switching providers can reuse them without another DeepSeek call. Audio caches
include the selected provider/model/voice. No provider is silently substituted
if generation fails. Dialect inference and pronunciation still need listening
review; the system cannot guarantee perfect accents.

Audio is generated only on a click. `/api/speech` accepts a saved `log_id`, not
arbitrary text, and checks session ownership. Replies are sent to OpenRouter
for narration. The UI labels the result as an AI voice and offers pause,
resume, stop, and replay without autoplaying new answers.

Configure `TTS_ENABLED`, `TTS_MODEL`, `TTS_VOICE`, and
`TTS_USD_PER_CHARACTER` in `.env`, then restart the origin. The default rate,
verified on 2026-10-21, is $0.00004 per input character ($0.04 per 1,000).
Speech costs are recorded as estimates with zero text tokens and included in
the monthly cost total/budget. Update the character rate when pricing changes;
the token-price calculator is not a speech calculator.

The process keeps a 32 MiB, 30-minute audio cache; each browser retains up to
five clips until a new chat or page exit. No audio files are persisted to disk.
One synthesis request runs at a time, with rate limiting and a 60-second
speech timeout (plus up to 20 seconds for diacritics). Answers over the model's 10,000-character limit are rejected
with an explicit message rather than silently truncated. The budget check is
best-effort alongside concurrent text requests, not a provider-side hard cap.

## Rollback

Before the switch, the domain pointed to the existing local application on
port 57875. That application and its data were retained. A copy of the original
tunnel configuration is stored outside this repository under the user's
`.cloudflared/backups/kasitbot-20261020-jurag/cloudflare-original.yml`.

Restore that backup to the dedicated attendance tunnel's `Data/cloudflare.yml`,
validate it with `cloudflared tunnel ingress validate`, and restart only that
tunnel's connector. Its existing supervisor will relaunch it. Do not restart
other Cloudflare connectors. Remove the JU Assistant Startup entry and stop its
supervisor and origin if JU_RAG should no longer run locally.
