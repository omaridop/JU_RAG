# Voice conversations

The waveform button opens a hands-free conversation with **أحمد الزعبي**, an
explicitly AI university helper. Arabic replies use a friendly Jordanian dialect,
normally one or two sentences. There are no visible transcripts in this surface.
Written chat keeps its existing persona and styles.

## Request flow

1. The student explicitly starts microphone capture once. An adaptive audio-level
   detector requires sustained activity, then sends after 850 ms of quiet. There
   is a 30-second maximum; silent recordings rotate locally without uploads.
2. `/api/voice/transcribe` forwards audio in memory to OpenRouter's
   `elevenlabs/scribe-v2`. No microphone audio is written to disk.
3. `/api/chat/stream` runs the existing retrieval and web-fallback pipeline with
   a voice-specific prompt and a maximum 320-token generation budget.
4. `/api/speech` reads the completed answer using `fish-audio/drama-3`, voice
   `4cb8371162134edeb4915672ffc66de6`. This voice is pinned for Ahmad; normal
   read-aloud buttons still follow admin settings. Voice mode asks for pronunciation
   marks during answer generation, skipping separate language/tashkeel calls.
5. The microphone is disabled while processing/playing the answer and automatically
   re-enabled afterward. The student can continue without tapping Send or Record.

The orb and waveform use microphone/playback analyser levels with a critically
damped spring; status, mute and end controls remain accessible with reduced motion.
The surface follows the existing green brand and Apple-style restraint and feedback.

This is **automatic half-duplex voice, not native realtime speech-to-speech**.
Audio waits for the final answer so a replaced web-fallback draft is never spoken.
Provider latency and searches still affect response time. Tap the orb to interrupt
playback or cancel processing and ask another question. Work already accepted
upstream may still be billed after cancellation.

## Safeguards and limitations

- Session-bound CSRF validation, upload-size cap, rate limits and budget checks.
- Existing chat/session limits remain active; transcripts are retained in chat logs.
- Browser permission is required, over HTTPS (or localhost).
- Mute, closing the dialog, leaving the page, or 60 seconds without speech release
  microphone tracks. Returning to a hidden tab requires explicit resume.
- No wake word or automatic voice barge-in while Ahmad speaks. Tap the orb to
  interrupt; this avoids the assistant transcribing its own speaker output.
- Audio-level detection is a lightweight heuristic, not a speech recognition model;
  loud continuous background sound may trigger it. Real microphone/noise conditions
  should be tested on target devices. The orb also allows manual send as a fallback.
- Scribe handles transcription, not voice generation.
- Short answers cannot guarantee perfect dialect pronunciation or full diacritics.
- Cost uses returned provider usage when available; otherwise an explicitly
  estimated 30-second transcription cost is recorded.

Offline frontend regression tests: `node --test tests/voice_client.test.cjs`.
These simulate audio and devices; they do not replace real microphone testing.

References: [OpenRouter STT](https://openrouter.ai/docs/guides/overview/multimodal/stt),
[OpenAI realtime conversation architecture](https://developers.openai.com/api/docs/guides/realtime-conversations).
