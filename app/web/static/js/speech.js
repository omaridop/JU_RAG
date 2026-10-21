/* On-demand narration. One player at a time; no autoplay on new replies. */
(function () {
  "use strict";
  const labels = {
    en: { ready: "Listen", playing: "Pause", paused: "Resume", ended: "Replay", loading: "Preparing audio…", stop: "Stop reading", ai: "AI voice",
      error: "Audio is unavailable. Try again.", busy: "Audio is busy. Please try again shortly.", budget: "The audio budget has been reached.", length: "This answer is too long to read aloud.", unavailable: "Audio is unavailable. Try again.", not_found: "This reply is no longer available. Start a new chat.", disabled: "Read aloud is currently unavailable." },
    ar: { ready: "استمع", playing: "إيقاف مؤقت", paused: "متابعة", ended: "إعادة الاستماع", loading: "جارٍ تجهيز الصوت…", stop: "إيقاف القراءة", ai: "صوت مولّد بالذكاء الاصطناعي",
      error: "تعذّر تشغيل الصوت. حاول مرة أخرى.", busy: "الخدمة مشغولة، حاول بعد قليل.", budget: "تم الوصول إلى سقف ميزانية الصوت.", length: "هذه الإجابة أطول من الحد المتاح للقراءة الصوتية.", unavailable: "تعذّر تشغيل الصوت. حاول مرة أخرى.", not_found: "هذه الإجابة لم تعد متاحة. ابدأ محادثة جديدة.", disabled: "القراءة الصوتية غير متاحة حالياً." }
  };
  const icons = {
    speaker: '<path d="M11 5 6 9H3v6h3l5 4V5Z"/><path d="M15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>',
    pause: '<path d="M8 5v14M16 5v14"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>'
  };
  const svg = name => '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' + icons[name] + '</svg>';
  const text = () => labels[document.documentElement.lang === "ar" ? "ar" : "en"];
  const entries = new Set();
  let active = null;

  function render(item) {
    const t = text();
    item.button.innerHTML = svg(item.state === "playing" ? "pause" : "speaker");
    const caption = document.createElement("span");
    caption.textContent = t[item.state] || t.ready;
    item.button.append(caption);
    item.button.setAttribute("aria-label", caption.textContent + " — " + item.preview);
    item.button.disabled = item.state === "loading";
    item.stop.hidden = !["loading", "playing", "paused"].includes(item.state);
    item.stop.setAttribute("aria-label", t.stop);
    item.stop.title = t.stop;
    item.note.textContent = t.ai;
    item.status.textContent = item.error ? (t[item.error] || t.error) : (item.state === "loading" ? t.loading : "");
    item.status.classList.toggle("is-error", !!item.error);
  }

  function stop(item) {
    if (!item) return;
    item.attempt = (item.attempt || 0) + 1;
    item.controller?.abort(); item.controller = null;
    if (item.audio) { item.audio.pause(); item.audio.currentTime = 0; }
    item.state = "ready"; item.error = null;
    if (active === item) active = null;
    render(item);
  }

  async function play(item) {
    if (item.state === "playing") { item.audio.pause(); return; }
    if (active && active !== item) stop(active);
    active = item; item.error = null;
    const attempt = item.attempt = (item.attempt || 0) + 1;
    try {
      if (!item.audio) {
        item.state = "loading"; render(item);
        const controller = new AbortController(); item.controller = controller;
        const timeout = setTimeout(() => controller.abort(), 105000);
        let blob;
        try {
          const res = await fetch("/api/speech", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ log_id: item.logId }), signal: controller.signal });
          if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.error || "error");
          }
          if (!res.headers.get("Content-Type")?.startsWith("audio/")) throw new Error("error");
          blob = await res.blob();
        } finally { clearTimeout(timeout); }
        if (active !== item || item.controller !== controller) return;
        item.controller = null;
        // Bound retained browser audio to five replies; the server also caches replays.
        const retained = [...entries].filter(other => other.url && other !== item);
        while (retained.length >= 5) {
          const old = retained.shift(); old.audio.pause(); old.audio.removeAttribute("src"); old.audio.load();
          URL.revokeObjectURL(old.url); old.audio = null; old.url = null; old.state = "ready"; render(old);
        }
        item.url = URL.createObjectURL(blob);
        const audio = new Audio(item.url); item.audio = audio;
        audio.addEventListener("play", () => { item.state = "playing"; render(item); });
        audio.addEventListener("pause", () => { if (item.state === "playing") { item.state = "paused"; render(item); } });
        audio.addEventListener("ended", () => { item.state = "ended"; if (active === item) active = null; render(item); });
        audio.addEventListener("error", () => { item.state = "ready"; item.error = "error"; render(item); });
      }
      if (item.state === "ended") item.audio.currentTime = 0;
      await item.audio.play();
    } catch (error) {
      if (active !== item || item.attempt !== attempt) return;
      item.controller = null; item.state = "ready"; item.error = error.message; render(item);
    }
  }

  function add(bubble, logId) {
    if (!logId || bubble.querySelector(".speech-controls")) return;
    const box = document.createElement("div"); box.className = "speech-controls";
    const button = document.createElement("button"); button.type = "button"; button.className = "speech-button";
    const stopButton = document.createElement("button"); stopButton.type = "button"; stopButton.className = "speech-stop";
    stopButton.innerHTML = svg("stop");
    const note = document.createElement("small"); note.className = "speech-note";
    const status = document.createElement("span"); status.className = "speech-status"; status.setAttribute("role", "status"); status.setAttribute("aria-atomic", "true");
    const item = { button, stop: stopButton, note, status, logId, state: "ready", preview: bubble.querySelector(".body").textContent.trim().slice(0, 60) };
    entries.add(item);
    button.addEventListener("click", () => play(item));
    stopButton.addEventListener("click", () => { stop(item); button.focus(); });
    box.append(button, stopButton, note, status); bubble.append(box); render(item);
  }

  function clear() {
    stop(active);
    entries.forEach(item => {
      if (item.audio) { item.audio.pause(); item.audio.removeAttribute("src"); item.audio.load(); }
      if (item.url) URL.revokeObjectURL(item.url);
    });
    entries.clear();
  }
  window.JUSpeech = { add, clear, refresh: () => entries.forEach(render) };
  window.addEventListener("pagehide", clear);
})();
