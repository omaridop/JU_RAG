# Voice conversations

The waveform button opens a tap-to-talk conversation with **أحمد الزعبي**, an
explicitly AI university helper. Arabic replies use a friendly Jordanian dialect,
normally one or two sentences. Written chat keeps its existing persona and styles.

## Request flow

1. The student explicitly starts microphone capture. Each recording ends on tap
   or after 30 seconds; microphone tracks are released before processing.
2. `/api/voice/transcribe` forwards audio in memory to OpenRouter's
   `elevenlabs/scribe-v2`. No microphone audio is written to disk.
3. `/api/chat/stream` runs the existing retrieval and web-fallback pipeline with
   a voice-specific prompt and a maximum 320-token generation budget.
4. `/api/speech` reads the completed answer using the existing admin-selected
   Arabic voice. Voice mode asks for pronunciation marks during answer generation
   instead of making a second diacritization request.

This is **turn-based voice, not native realtime speech-to-speech**. Text streams,
but audio waits for the final answer so a replaced web-fallback draft is never
spoken. Provider latency and searches still affect response time. Stop cancels
browser requests and playback; work already accepted upstream may still be billed.

## Safeguards and limitations

- Session-bound CSRF validation, upload-size cap, rate limits and budget checks.
- Existing chat/session limits remain active; transcripts are retained in chat logs.
- Browser permission is required, over HTTPS (or localhost).
- Closing the dialog or leaving the page releases microphone tracks and audio URLs.
- No always-on listening or automatic barge-in. Press Stop, then start a new turn.
- Scribe handles transcription, not voice generation. Existing ElevenLabs/Fish
  speech output settings still select the spoken voice.
- Short answers cannot guarantee perfect dialect pronunciation or full diacritics.
- Cost uses returned provider usage when available; otherwise an explicitly
  estimated 30-second transcription cost is recorded.

References: [OpenRouter STT](https://openrouter.ai/docs/guides/overview/multimodal/stt),
[OpenAI realtime conversation architecture](https://developers.openai.com/api/docs/guides/realtime-conversations).
