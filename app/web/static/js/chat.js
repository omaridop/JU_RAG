/* University of Jordan — chat (Claude-style). User right, bot left, per-message dir. */
(function () {
  "use strict";

  const I18N = {
    en: {
      brand: "University of Jordan", brandSub: "Smart Student Assistant",
      newChat: "New chat", reg: "Registration",
      welcomeTitle: "How can I help you today?",
      welcomeSub: "Ask about admissions, registration, courses, dates, fees and campus life at the University of Jordan.",
      placeholder: "Message the assistant…",
      disclaimer: "Answers may contain errors; please verify with official university sources.",
      errorMsg: "Something went wrong. Please try again.",
      fbUp: "Helpful answer", fbDown: "Not helpful",
      searching: "Searching the University of Jordan website…",
      sources: "Sources",
      webBtn: "Search site", webTitle: "Search the official JU website for this message",
      styleCap: "Answer", styleConcise: "Concise", styleDetailed: "Detailed",
      styleTitle: "Detailed answer — on for a full answer, off for a short one",
      suggest: ["How do I register for courses?", "What are the admission requirements?",
                "When is the course withdrawal deadline?", "How do I reset my portal password?"]
    },
    ar: {
      brand: "الجامعة الأردنية", brandSub: "المساعد الذكي للطلبة",
      newChat: "محادثة جديدة", reg: "بوابة التسجيل",
      welcomeTitle: "كيف يمكنني مساعدتك اليوم؟",
      welcomeSub: "اسألني عن القبول، التسجيل، المواد، المواعيد، الرسوم، والحياة الجامعية في الجامعة الأردنية.",
      placeholder: "اكتب رسالتك…",
      disclaimer: "قد تحتوي الإجابات على أخطاء؛ يُرجى التحقق من المصادر الرسمية للجامعة.",
      errorMsg: "حدث خطأ ما. يُرجى المحاولة مرة أخرى.",
      fbUp: "إجابة مفيدة", fbDown: "إجابة غير مفيدة",
      searching: "جاري البحث في موقع الجامعة الأردنية…",
      sources: "المصادر",
      webBtn: "بحث في الموقع", webTitle: "ابحث في موقع الجامعة الرسمي لهذه الرسالة",
      styleCap: "الإجابة", styleConcise: "موجز", styleDetailed: "مفصّل",
      styleTitle: "إجابة مفصّلة — فعّلها لإجابة كاملة، أو أوقفها لإجابة موجزة",
      suggest: ["كيف أسجّل في المواد؟", "ما هي شروط القبول؟",
                "متى آخر موعد للانسحاب من المواد؟", "كيف أعيد تعيين كلمة مرور البوابة؟"]
    }
  };
  const AR = /[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]/;
  const dirOf = (s) => (AR.test(s || "") ? "rtl" : "ltr");
  const THUMB_UP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/></svg>';
  const THUMB_DOWN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/></svg>';
  const LINK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>';

  let lang = localStorage.getItem("ju_lang") || "ar";
  let busy = false;

  const $ = (s) => document.querySelector(s);
  const app = $("#app"), messages = $("#messages"), conversation = $("#conversation");
  const input = $("#input"), form = $("#form"), sendBtn = $("#send");
  const webToggle = $("#webToggle");
  const styleToggle = $("#styleToggle");
  let webSearch = localStorage.getItem("ju_web") === "1";
  let answerStyle = localStorage.getItem("ju_style") === "detailed" ? "detailed" : "concise";
  const t = () => I18N[lang];

  function applyWeb() {
    if (!webToggle) return;
    webToggle.classList.toggle("on", webSearch);
    webToggle.setAttribute("aria-pressed", webSearch ? "true" : "false");
  }

  function applyStyle() {
    if (!styleToggle) return;
    const detailed = answerStyle === "detailed";
    styleToggle.classList.toggle("on", detailed);
    styleToggle.setAttribute("aria-pressed", detailed ? "true" : "false");
  }

  function applyLang(next) {
    lang = next; localStorage.setItem("ju_lang", lang);
    document.documentElement.lang = lang;
    document.documentElement.dir = (lang === "ar") ? "rtl" : "ltr";
    document.querySelectorAll("[data-lang]").forEach(b => b.classList.toggle("active", b.dataset.lang === lang));
    document.querySelectorAll("[data-i18n]").forEach(n => { const k = n.getAttribute("data-i18n"); if (t()[k] != null) n.textContent = t()[k]; });
    document.querySelectorAll("[data-i18n-ph]").forEach(n => { const k = n.getAttribute("data-i18n-ph"); if (t()[k] != null) n.setAttribute("placeholder", t()[k]); });
    document.querySelectorAll("[data-i18n-title]").forEach(n => { const k = n.getAttribute("data-i18n-title"); if (t()[k] != null) n.setAttribute("title", t()[k]); });
    applyStyle();
    renderSuggest();
  }

  function renderSuggest() {
    const box = $("#suggest"); box.innerHTML = "";
    t().suggest.forEach(s => {
      const b = document.createElement("button"); b.type = "button"; b.textContent = s;
      b.addEventListener("click", () => send(s));
      box.appendChild(b);
    });
  }

  function mdToHtml(text) {
    try { if (window.marked && window.DOMPurify) return DOMPurify.sanitize(marked.parse(text, { breaks: true })); }
    catch (e) {}
    const d = document.createElement("div"); d.textContent = text; return d.innerHTML.replace(/\n/g, "<br>");
  }

  const scrollDown = () => { conversation.scrollTop = conversation.scrollHeight; };

  function addMessage(role) {
    app.classList.remove("is-empty");
    const m = document.createElement("div"); m.className = "message " + role;
    const bubble = document.createElement("div"); bubble.className = "bubble";
    const body = document.createElement("div"); body.className = "body";
    bubble.appendChild(body); m.appendChild(bubble); messages.appendChild(m);
    scrollDown();
    return { bubble, body };
  }

  function setBody(el, text, asHtml) {
    const d = dirOf(text);
    el.dir = d; el.style.textAlign = (d === "rtl") ? "right" : "left";
    if (asHtml) el.innerHTML = mdToHtml(text); else el.textContent = text;
  }

  function showSearching(body, query) {
    body.dir = (lang === "ar") ? "rtl" : "ltr";
    body.innerHTML =
      '<div class="searching"><span class="spinner"></span>' +
      '<span>' + escapeHtml(t().searching) +
      (query ? ' <em>“' + escapeHtml(query) + '”</em>' : '') + '</span></div>';
  }

  function escapeHtml(s) {
    const d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML;
  }

  function renderSources(bubble, sources) {
    const web = (sources || []).filter(s => s && s.url);
    if (!web.length) return;
    const seen = new Set(), box = document.createElement("div");
    box.className = "src-list";
    box.innerHTML = '<span class="src-title">' + escapeHtml(t().sources) + '</span>';
    web.forEach(s => {
      if (seen.has(s.url)) return; seen.add(s.url);
      const a = document.createElement("a");
      a.className = "src-link"; a.href = s.url; a.target = "_blank"; a.rel = "noopener";
      let host = ""; try { host = new URL(s.url).hostname.replace(/^www\./, ""); } catch (e) {}
      a.innerHTML = '<span class="src-favicon">' + LINK_ICON + '</span>' +
        '<span class="src-text"><b>' + escapeHtml(s.label || host) + '</b>' +
        '<small dir="ltr">' + escapeHtml(host) + '</small></span>';
      box.appendChild(a);
    });
    bubble.appendChild(box);
  }

  function addFeedback(bubble, logId) {
    if (!logId) return;
    const bar = document.createElement("div"); bar.className = "feedback";
    const up = document.createElement("button"); up.type = "button"; up.className = "fb-btn up"; up.title = t().fbUp; up.innerHTML = THUMB_UP;
    const down = document.createElement("button"); down.type = "button"; down.className = "fb-btn down"; down.title = t().fbDown; down.innerHTML = THUMB_DOWN;
    function vote(v, btn) {
      fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ log_id: logId, value: v }) }).catch(() => {});
      up.classList.remove("on"); down.classList.remove("on"); btn.classList.add("on");
    }
    up.addEventListener("click", () => vote(1, up));
    down.addEventListener("click", () => vote(-1, down));
    bar.appendChild(up); bar.appendChild(down); bubble.appendChild(bar);
  }

  function updateSendState() { sendBtn.disabled = busy || input.value.trim() === ""; }
  function autoGrow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; }

  async function send(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    busy = true;
    const u = addMessage("user"); setBody(u.body, text, false);
    input.value = ""; autoGrow(); updateSendState();
    const a = addMessage("assistant");
    a.body.innerHTML = '<span class="dots"><i></i><i></i><i></i></span>';

    let buf = "", first = true;
    try {
      const res = await fetch("/api/chat/stream", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, web_search: webSearch, answer_style: answerStyle })
      });
      const reader = res.body.getReader(); const dec = new TextDecoder(); let acc = "";
      while (true) {
        const { value, done } = await reader.read(); if (done) break;
        acc += dec.decode(value, { stream: true });
        let i;
        while ((i = acc.indexOf("\n\n")) >= 0) {
          const raw = acc.slice(0, i).replace(/^data: ?/, "").trim(); acc = acc.slice(i + 2);
          if (!raw) continue;
          let ev; try { ev = JSON.parse(raw); } catch (e) { continue; }
          if (ev.type === "status") { showSearching(a.body, ev.query); first = true; scrollDown(); }
          else if (ev.type === "reset") { buf = ""; first = true; }
          else if (ev.type === "delta") { if (first) { a.body.innerHTML = ""; first = false; } buf += ev.text; setBody(a.body, buf, true); scrollDown(); }
          else if (ev.type === "done") { if (first) { setBody(a.body, buf || "…", true); first = false; } renderSources(a.bubble, ev.sources); addFeedback(a.bubble, ev.log_id); scrollDown(); }
          else if (ev.type === "error") { setBody(a.body, ev.text || t().errorMsg, true); first = false; }
        }
      }
    } catch (e) {
      setBody(a.body, t().errorMsg, true);
    } finally {
      busy = false; updateSendState(); input.focus();
    }
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
  input.addEventListener("input", () => { autoGrow(); updateSendState(); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); } });
  document.querySelectorAll("[data-lang]").forEach(b => b.addEventListener("click", () => applyLang(b.dataset.lang)));
  if (webToggle) webToggle.addEventListener("click", () => {
    webSearch = !webSearch; localStorage.setItem("ju_web", webSearch ? "1" : "0");
    applyWeb(); input.focus();
  });
  if (styleToggle) styleToggle.addEventListener("click", () => {
    answerStyle = answerStyle === "detailed" ? "concise" : "detailed";
    localStorage.setItem("ju_style", answerStyle);
    applyStyle(); input.focus();
  });
  $("#newChat").addEventListener("click", async () => {
    try { await fetch("/api/reset", { method: "POST" }); } catch (e) {}
    messages.innerHTML = ""; app.classList.add("is-empty"); input.value = ""; autoGrow(); updateSendState(); input.focus();
  });

  applyLang(lang); applyWeb(); autoGrow(); updateSendState(); input.focus();
})();
