"""RAG orchestration: limit check -> memory cache -> retrieval -> generation.

Exposes two entry points used by the web layer:
  * :func:`generate_answer` — blocking, returns a full result dict.
  * :func:`stream_answer`   — generator of SSE-friendly event dicts.

Both share :func:`_prepare`, which decides whether the turn is short-circuited
(limit reached or memory hit) or needs the chat model.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Iterator, Optional

from openai import APIError

from ..config import settings
from ..core import db, tokens
from ..core.llm import (LLMNotConfigured, ProviderResponseError,
                        first_choice_content, get_client)
from ..core.utils import detect_lang
from ..pipeline.embed import embed_query
from ..pipeline.vectorstore import get_store
from . import memory_cache, prompts, web_search

log = logging.getLogger(__name__)


def _reasoning_extra(effort: str) -> dict:
    """OpenRouter `reasoning` config for a given effort.

    Reasoning models spend hidden "thinking" tokens before answering — the main
    latency cost. Capping the effort (or disabling it) makes responses much
    faster. Returns the fragment to merge into ``extra_body``; an unknown value
    sends nothing (the model uses its own default).
    """
    e = (effort or "").strip().lower()
    if e in ("off", "none", "false", "disabled", "0"):
        return {"reasoning": {"enabled": False}}
    if e in ("low", "medium", "high"):
        return {"reasoning": {"effort": e}}
    return {}


# Computed once: the reasoning fragment merged into chat / condense calls.
_CHAT_REASONING = _reasoning_extra(settings.CHAT_REASONING_EFFORT)
_CONDENSE_REASONING = _reasoning_extra(settings.CONDENSE_REASONING_EFFORT)
_WEB_AGENT_REASONING = _reasoning_extra(settings.WEB_AGENT_REASONING_EFFORT)


def _chat_extra() -> dict:
    """extra_body for a plain chat-model answer call (fast: reasoning usually off)."""
    return {"usage": {"include": True}, **_CHAT_REASONING}


def _agent_extra() -> dict:
    """extra_body for the web-search agent — needs a little reasoning so the model
    actually decides to call the search tool."""
    return {"usage": {"include": True}, **_WEB_AGENT_REASONING}


# Forces a clean final answer when the agent has used up its search rounds (so it
# stops trying to search and doesn't leak raw tool-call syntax as the answer).
_FINAL_ANSWER_NUDGE = (
    "Stop searching now — do NOT call any tool. Using the search results above, "
    "write your final answer to the student in their language. If the results "
    "don't fully answer the question, summarise what you did find and point them "
    "to the relevant official JU page."
)

# DeepSeek can emit tool-call syntax as plain text when it wants to call a tool
# but none is offered (the «｜｜DSML｜｜...» blocks). Strip any such leak.
_TOOL_LEAK = re.compile(r"<[^\n>]*?(?:DSML|tool_calls|invoke name|parameter name)[^\n>]*?>",
                        re.IGNORECASE)


def _strip_tool_leak(text: str) -> str:
    text = (text or "").strip()
    if "DSML" in text or "tool_calls" in text or "｜" in text:
        text = _TOOL_LEAK.sub(" ", text).replace("｜", " ")
        text = re.sub(r"</?\s*(?:tool_calls|invoke|parameter)\b[^>]*>", " ", text, flags=re.I)
        text = re.sub(r"[ \t]+", " ", text).strip()
    return text


# The model PUNTS (defers to an office / says the info isn't in the context) with
# consistent phrasing — that's our reliable signal to escalate to a web search.
_PUNT_RE = re.compile(
    r"do(?:es)?\s*n['’]?t\s+(?:have|contain|include)"
    r"|not\s+contain|no\s+(?:information|details)\b|not\s+available\s+in"
    r"|isn['’]?t\s+(?:in|available|included)"
    r"|recommend\s+(?:checking|contacting|visiting)|please\s+contact"
    r"|contact\s+the\s+[\w\s]+?(?:unit|office|department|deanship)"
    r"|check\s+the\s+(?:official|university)"
    r"|لا\s+(?:تتوفر|يحتوي|توجد|أملك|تتضمّن|يتضمن|أجد)"
    r"|غير\s+(?:متوفرة|متاحة|موجودة)"
    r"|يُرجى\s+(?:التواصل|مراجعة)|تواصل\s+مع|راجع\s+(?:الموقع|البوابة|دائرة|المصادر)"
    r"|الموقع\s+الرسمي|البوابة\s+الإلكترونية",
    re.IGNORECASE)


def _looks_like_punt(text: str) -> bool:
    """True when the answer defers instead of answering (so we should search)."""
    return bool(_PUNT_RE.search(text or ""))


def _agent_messages(prep: dict) -> list:
    """Rebuild the messages with the web-search note so the agent will search."""
    system = (prompts.SYSTEM_PROMPT + prompts.style_note(prep["answer_style"])
              + prompts.WEB_TOOL_NOTE)
    return [
        {"role": "system", "content": system},
        *prep["history"],
        {"role": "user",
         "content": prompts.build_user_message(prep["question"], prep["context"], prep["lang"], prep.get("answer_style", "concise"))},
    ]


# Tool the chat model can call to look up live, official info on the JU website.
_WEB_TOOL = {
    "type": "function",
    "function": {
        "name": "search_ju_website",
        "description": (
            "Search the OFFICIAL University of Jordan website (ju.edu.jo and its "
            "subdomains) for current or official information that is NOT in the "
            "provided context — e.g. academic-calendar dates, news and events, "
            "admission/registration procedures, faculty or department pages, "
            "contacts, forms, or anything time-sensitive. Returns excerpts from "
            "official JU pages with their URLs. Prefer the provided context first; "
            "use this only when the context does not contain the answer."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description":
                          "A focused search query (English or Arabic) for the JU site."}
            },
            "required": ["query"],
        },
    },
}


def _web_enabled() -> bool:
    """Whether the web-search feature is AVAILABLE to students at all.

    This is the admin master switch (runtime setting overrides config). The
    student still opts in PER MESSAGE via a composer toggle.
    """
    raw = db.get_setting("web_search_enabled")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return settings.WEB_SEARCH_ENABLED


def _web_sources(web_sources: list[dict]) -> list[dict]:
    """Format fetched JU pages as source descriptors for the UI/logs."""
    return [{"label": s.get("title") or s.get("url"), "url": s.get("url"),
             "type": "web", "date": "", "page": None} for s in web_sources]

_ERR_EN = ("Sorry — I'm having trouble reaching the assistant service right now. "
           "Please try again in a moment.")
_ERR_AR = ("عذراً — أواجه صعوبة في الوصول إلى خدمة المساعد حالياً. "
           "يُرجى المحاولة مرة أخرى بعد قليل.")


def _err_message(lang: str) -> str:
    return _ERR_AR if lang == "ar" else _ERR_EN


def _sources_from_hits(hits: list[dict]) -> list[dict]:
    out = []
    for h in hits:
        m = h.get("metadata", {}) or {}
        out.append({
            "label": m.get("source_label") or m.get("filename") or "Knowledge base",
            "date": (m.get("doc_date") or "")[:10],
            "page": m.get("page_start"),
            "type": m.get("type", "chunk"),
            "title": m.get("title", ""),
            "similarity": h.get("similarity"),
            "recency": h.get("recency"),
        })
    return out


def _build_history(session_token: str) -> list[dict]:
    rows = db.recent_logs_for_session(session_token, settings.CHAT_HISTORY_TURNS)
    msgs: list[dict] = []
    for r in reversed(rows):  # chronological order
        if r.get("served_from") == "limit":
            continue
        msgs.append({"role": "user", "content": r["question"]})
        msgs.append({"role": "assistant", "content": r["answer"]})
    return msgs


def _log(session_token: str, question: str, answer: str, lang: str,
         sources: list[dict], served_from: str, model: str, latency_ms: int) -> int:
    return db.add_chat_log(
        session_token=session_token, question=question, answer=answer, lang=lang,
        sources_json=json.dumps(sources, ensure_ascii=False), served_from=served_from,
        model=model, latency_ms=latency_ms,
    )


_CONDENSE_SYSTEM = (
    "You rewrite a student's latest chat message into ONE self-contained search "
    "question for a University of Jordan knowledge base. Resolve pronouns and "
    "implicit references (the major, program, course, or fee type being "
    "discussed) using the conversation. When the student names a DIFFERENT "
    "attribute than the previous turn (another program, fee type, or major), "
    "REPLACE that attribute — do not keep both.\n"
    "University terminology: «التنافسي/التنافس» means the REGULAR program "
    "(«البرنامج العادي», admission via القبول الموحد); «الموازي» is the parallel "
    "program; «الدولي» is the international program.\n"
    "Example: after discussing the PARALLEL fee for AI, the follow-up "
    "«التنافس؟» becomes «كم رسم الساعة المعتمدة للبرنامج العادي (التنافسي) "
    "لتخصص الذكاء الاصطناعي؟».\n"
    "Keep the student's language (Arabic in -> Arabic out). Output ONLY the "
    "rewritten question — no explanations, no quotes. If the message is "
    "already self-contained, output it unchanged."
)


def _condense_question(question: str, history: list[dict]) -> str:
    """Make follow-ups standalone so RETRIEVAL sees the conversation context.

    The generator already receives the chat history, but the retriever only
    embeds one query — a bare follow-up like «والتنافسي؟» matches the wrong
    records without this rewrite. Best-effort: any failure falls back to the
    raw question.
    """
    if not history:
        return question
    convo = "\n".join(
        f"{'Student' if m['role'] == 'user' else 'Assistant'}: {m['content'][:400]}"
        for m in history[-6:]
    )
    try:
        resp = get_client().chat.completions.create(
            model=settings.CHAT_MODEL,
            messages=[
                {"role": "system", "content": _CONDENSE_SYSTEM},
                {"role": "user",
                 "content": f"Conversation so far:\n{convo}\n\n"
                            f"Student's latest message: {question}"},
            ],
            temperature=0.0,
            max_tokens=settings.CONDENSE_MAX_TOKENS,
            extra_body={"usage": {"include": True}, **_CONDENSE_REASONING},
        )
        _usage = getattr(resp, "usage", None)
        tokens.record(settings.CHAT_MODEL, "condense", _usage, cost_usd=tokens.usage_cost(_usage))
        rewritten = first_choice_content(resp).strip().strip('"“”«»')
        if 0 < len(rewritten) <= 400:
            if rewritten != question:
                log.info("Condensed follow-up %r -> %r", question, rewritten)
            return rewritten
    except Exception:  # noqa: BLE001 - condensation must never block answering
        log.warning("Query condensation failed; using the raw question.", exc_info=True)
    return question


def _monthly_budget() -> float:
    """Effective monthly USD ceiling (admin setting overrides config)."""
    raw = db.get_setting("monthly_budget_usd")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    return max(0.0, settings.MONTHLY_BUDGET_USD)


def _prepare(question: str, session_token: str, use_web: bool = False,
             answer_style: str = "concise") -> dict:
    """Decide how to answer. Returns a dict with a 'type' discriminator.

    ``use_web`` is the student's per-message opt-in; web search runs only when
    they enabled it AND the admin master switch allows it.
    ``answer_style`` ('concise' | 'detailed') is the student's per-message
    choice for how long/thorough the answer should be.
    """
    lang = detect_lang(question)
    web_available = _web_enabled()
    web_on = bool(use_web) and web_available

    # 1) Per-session limit (token control).
    limit = db.get_int_setting("chat_limit", settings.CHAT_LIMIT_PER_SESSION)
    if db.session_message_count(session_token) >= limit:
        return {"type": "limit", "lang": lang, "limit": limit}

    # 1b) Monthly budget ceiling (USD). 0 = unlimited.
    budget = _monthly_budget()
    if budget > 0 and db.token_month_cost() >= budget:
        return {"type": "budget", "lang": lang}

    # 2) Exact memory match — free, no embedding/LLM.
    mem = memory_cache.lookup_exact(question)
    if mem and answer_style != "voice":
        return {"type": "memory", "lang": mem["lang"] or lang, "mem": mem}

    # 3) Conversation memory: rewrite follow-ups into standalone queries so
    #    retrieval knows what «التنافس؟» refers to.
    history = _build_history(session_token)
    search_q = _condense_question(question, history)
    if search_q != question:
        mem = memory_cache.lookup_exact(search_q)
        if mem and answer_style != "voice":
            return {"type": "memory", "lang": mem["lang"] or lang, "mem": mem}

    # 4) Embed once, reuse for semantic-memory AND retrieval.
    query_embedding = embed_query(search_q)
    mem = memory_cache.lookup_semantic(search_q, query_embedding)
    if mem and answer_style != "voice":
        return {"type": "memory", "lang": mem["lang"] or lang, "mem": mem}

    # 5) Retrieve context and assemble the prompt. Deactivated documents are
    #    excluded — to the bot they are as good as deleted.
    hits = get_store().search(query_embedding, exclude_doc_ids=db.inactive_doc_ids())

    # Web search modes:
    #   web_on   = student toggled "Search site" -> answer with the search agent.
    #   web_auto = available + admin auto-search on -> answer fast from the KB,
    #              and if the model PUNTS (no answer in context), escalate to a
    #              real search. This is reliable because the model punts
    #              consistently, whereas it only *chooses* to search ~3/4 of the
    #              time. Cosine similarity can't tell "topically similar" from
    #              "actually answers", so we trigger on the punt, not a threshold.
    web_auto = (not web_on) and web_available and settings.WEB_SEARCH_AUTO

    context = prompts.build_context(hits)
    system = (prompts.SYSTEM_PROMPT + prompts.style_note(answer_style)
              + (prompts.WEB_TOOL_NOTE if web_on else ""))
    messages = [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": prompts.build_user_message(question, context, lang, answer_style)},
    ]
    return {"type": "rag", "lang": lang, "hits": hits, "messages": messages,
            "web_on": web_on, "web_auto": web_auto, "question": question,
            "history": history, "context": context, "answer_style": answer_style}


# --------------------------------------------------------------------------- #
#  Web-search agent (tool-calling loop)                                        #
# --------------------------------------------------------------------------- #
def _exec_tool_calls(messages: list, tcs: dict, web_sources: list,
                     content: str) -> Iterator[dict]:
    """Append the assistant tool-call turn, run each search, append results.

    Yields ``status`` events so the UI can show "searching the JU website…".
    """
    messages.append({
        "role": "assistant", "content": content,
        "tool_calls": [{
            "id": s["id"] or f"call_{i}", "type": "function",
            "function": {"name": s["name"], "arguments": s["args"] or "{}"},
        } for i, s in sorted(tcs.items())],
    })
    for i, s in sorted(tcs.items()):
        tool_id = s["id"] or f"call_{i}"
        if s["name"] != "search_ju_website":
            messages.append({"role": "tool", "tool_call_id": tool_id,
                             "content": "Unknown tool."})
            continue
        try:
            query = (json.loads(s["args"] or "{}").get("query") or "").strip()
        except json.JSONDecodeError:
            query = (s["args"] or "").strip()
        yield {"type": "status", "stage": "searching", "query": query}
        result, srcs = web_search.run_search(query)
        seen = {w["url"] for w in web_sources}
        for sc in srcs:
            if sc["url"] not in seen:
                web_sources.append(sc)
                seen.add(sc["url"])
        messages.append({"role": "tool", "tool_call_id": tool_id, "content": result})


def _voice_token_limit(messages):
    voice = any(m.get("role") == "system" and "VOICE MODE —" in m.get("content", "") for m in messages)
    return min(settings.CHAT_MAX_TOKENS, 320) if voice else settings.CHAT_MAX_TOKENS


def _agent_stream(messages: list, holder: dict) -> Iterator[dict]:
    """Streaming tool-calling agent. Yields delta/status/reset events; fills
    ``holder`` with the final ``answer`` and ``web_sources``."""
    web_sources: list[dict] = []
    final = ""
    max_iters = max(1, settings.WEB_SEARCH_MAX_ITERS)

    for iteration in range(max_iters + 1):
        offer_tools = iteration < max_iters  # last round: force a plain answer
        if not offer_tools:
            messages.append({"role": "user", "content": _FINAL_ANSWER_NUDGE})
        kwargs = dict(
            model=settings.CHAT_MODEL, messages=messages,
            temperature=settings.CHAT_TEMPERATURE, max_tokens=_voice_token_limit(messages),
            stream=True, stream_options={"include_usage": True},
            extra_body=_agent_extra(),
        )
        if offer_tools:
            kwargs["tools"] = [_WEB_TOOL]
            kwargs["tool_choice"] = "auto"

        content_parts: list[str] = []
        tcs: dict = {}
        usage = None
        stream = get_client().chat.completions.create(**kwargs)
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                content_parts.append(delta.content)
                # Never stream leaked tool-call syntax to the UI.
                if "DSML" not in delta.content and "｜" not in delta.content:
                    yield {"type": "delta", "text": delta.content}
            for tcd in (getattr(delta, "tool_calls", None) or []):
                slot = tcs.setdefault(tcd.index, {"id": "", "name": "", "args": ""})
                if tcd.id:
                    slot["id"] = tcd.id
                if tcd.function and tcd.function.name:
                    slot["name"] = tcd.function.name
                if tcd.function and tcd.function.arguments:
                    slot["args"] += tcd.function.arguments
        tokens.record(settings.CHAT_MODEL, "chat", usage, cost_usd=tokens.usage_cost(usage))

        if not tcs:
            final = _strip_tool_leak("".join(content_parts))
            break
        # The model is about to search — its streamed preamble is not the final
        # answer, so tell the UI to clear it, then run the searches.
        yield {"type": "reset"}
        yield from _exec_tool_calls(messages, tcs, web_sources, "".join(content_parts))

    holder["answer"] = final
    holder["web_sources"] = web_sources


def _agent_blocking(messages: list) -> tuple[str, list[dict]]:
    """Non-streaming tool-calling agent for the blocking endpoint."""
    web_sources: list[dict] = []
    max_iters = max(1, settings.WEB_SEARCH_MAX_ITERS)
    answer = ""
    for iteration in range(max_iters + 1):
        offer_tools = iteration < max_iters
        if not offer_tools:
            messages.append({"role": "user", "content": _FINAL_ANSWER_NUDGE})
        kwargs = dict(
            model=settings.CHAT_MODEL, messages=messages,
            temperature=settings.CHAT_TEMPERATURE, max_tokens=_voice_token_limit(messages),
            extra_body=_agent_extra(),
        )
        if offer_tools:
            kwargs["tools"] = [_WEB_TOOL]
            kwargs["tool_choice"] = "auto"
        resp = get_client().chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        usage = getattr(resp, "usage", None)
        tokens.record(settings.CHAT_MODEL, "chat", usage, cost_usd=tokens.usage_cost(usage))
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            answer = _strip_tool_leak(msg.content or "")
            break
        tcs = {i: {"id": tc.id, "name": tc.function.name, "args": tc.function.arguments}
               for i, tc in enumerate(tool_calls)}
        # Drain the status generator (no streaming here).
        for _ in _exec_tool_calls(messages, tcs, web_sources, msg.content or ""):
            pass
    return answer, web_sources


# --------------------------------------------------------------------------- #
#  Blocking generation                                                        #
# --------------------------------------------------------------------------- #
def generate_answer(question: str, session_token: str, use_web: bool = False,
                    answer_style: str = "concise") -> dict:
    t0 = time.time()
    lang = detect_lang(question)
    try:
        prep = _prepare(question, session_token, use_web, answer_style)
    except (LLMNotConfigured, APIError, ProviderResponseError) as exc:
        log.exception("Prepare failed")
        ans = _err_message(lang)
        _log(session_token, question, ans, lang, [], "error", "", int((time.time() - t0) * 1000))
        return {"answer": ans, "sources": [], "served_from": "error", "lang": lang}

    if prep["type"] == "limit":
        ans = prompts.limit_message(prep["lang"], prep["limit"])
        _log(session_token, question, ans, prep["lang"], [], "limit", "", 0)
        return {"answer": ans, "sources": [], "served_from": "limit", "lang": prep["lang"]}

    if prep["type"] == "budget":
        ans = prompts.budget_message(prep["lang"])
        _log(session_token, question, ans, prep["lang"], [], "budget", "", 0)
        return {"answer": ans, "sources": [], "served_from": "budget", "lang": prep["lang"]}

    if prep["type"] == "memory":
        ans, mem = prep["mem"]["answer"], prep["mem"]
        db.bump_session(session_token)
        log_id = _log(session_token, question, ans, prep["lang"], mem["sources"], "memory",
                      f"memory:{mem['match']}", int((time.time() - t0) * 1000))
        return {"answer": ans, "sources": mem["sources"], "served_from": "memory",
                "lang": prep["lang"], "match": mem["match"], "log_id": log_id}

    # RAG path. web_on (opt-in) -> search agent. Otherwise answer fast from the
    # KB and escalate to a search if the model punts (web_auto).
    web_sources: list[dict] = []
    try:
        if prep.get("web_on"):
            ans, web_sources = _agent_blocking(prep["messages"])
        else:
            resp = get_client().chat.completions.create(
                model=settings.CHAT_MODEL, messages=prep["messages"],
                temperature=settings.CHAT_TEMPERATURE, max_tokens=_voice_token_limit(prep["messages"]),
                extra_body=_chat_extra(),
            )
            _usage = getattr(resp, "usage", None)
            tokens.record(settings.CHAT_MODEL, "chat", _usage, cost_usd=tokens.usage_cost(_usage))
            ans = first_choice_content(resp).strip()
            if prep.get("web_auto") and _looks_like_punt(ans):
                log.info("Answer punted; escalating to web search.")
                ans, web_sources = _agent_blocking(_agent_messages(prep))
    except (LLMNotConfigured, APIError, ProviderResponseError) as exc:
        log.exception("Generation failed")
        ans = _err_message(prep["lang"])
        _log(session_token, question, ans, prep["lang"], [], "error", settings.CHAT_MODEL,
             int((time.time() - t0) * 1000))
        return {"answer": ans, "sources": [], "served_from": "error", "lang": prep["lang"]}

    sources = _sources_from_hits(prep["hits"]) + _web_sources(web_sources)
    served = "web" if web_sources else "rag"
    db.bump_session(session_token)
    log_id = _log(session_token, question, ans, prep["lang"], sources, served, settings.CHAT_MODEL,
                  int((time.time() - t0) * 1000))
    return {"answer": ans, "sources": sources, "served_from": served,
            "lang": prep["lang"], "log_id": log_id}


# --------------------------------------------------------------------------- #
#  Streaming generation (SSE events)                                          #
# --------------------------------------------------------------------------- #
def stream_answer(question: str, session_token: str, use_web: bool = False,
                  answer_style: str = "concise") -> Iterator[dict]:
    t0 = time.time()
    lang = detect_lang(question)
    try:
        prep = _prepare(question, session_token, use_web, answer_style)
    except (LLMNotConfigured, APIError, ProviderResponseError):
        log.exception("Prepare failed (stream)")
        ans = _err_message(lang)
        _log(session_token, question, ans, lang, [], "error", "", int((time.time() - t0) * 1000))
        yield {"type": "meta", "lang": lang, "served_from": "error"}
        yield {"type": "delta", "text": ans}
        yield {"type": "done", "served_from": "error", "sources": []}
        return

    lang = prep["lang"]
    yield {"type": "meta", "lang": lang, "served_from": prep["type"]}

    if prep["type"] == "limit":
        ans = prompts.limit_message(lang, prep["limit"])
        _log(session_token, question, ans, lang, [], "limit", "", 0)
        yield {"type": "delta", "text": ans}
        yield {"type": "done", "served_from": "limit", "sources": []}
        return

    if prep["type"] == "budget":
        ans = prompts.budget_message(lang)
        _log(session_token, question, ans, lang, [], "budget", "", 0)
        yield {"type": "delta", "text": ans}
        yield {"type": "done", "served_from": "budget", "sources": []}
        return

    if prep["type"] == "memory":
        ans, mem = prep["mem"]["answer"], prep["mem"]
        db.bump_session(session_token)
        log_id = _log(session_token, question, ans, lang, mem["sources"], "memory",
                      f"memory:{mem['match']}", int((time.time() - t0) * 1000))
        yield {"type": "delta", "text": ans}
        yield {"type": "done", "served_from": "memory", "sources": mem["sources"], "log_id": log_id}
        return

    # RAG streaming (with the web-search agent when enabled).
    kb_sources = _sources_from_hits(prep["hits"])
    holder: dict = {"answer": "", "web_sources": []}
    acc = []
    try:
        if prep.get("web_on"):
            for ev in _agent_stream(prep["messages"], holder):
                if ev.get("type") == "delta":
                    acc.append(ev["text"])
                elif ev.get("type") == "reset":
                    acc.clear()
                yield ev
        else:
            stream = get_client().chat.completions.create(
                model=settings.CHAT_MODEL, messages=prep["messages"],
                temperature=settings.CHAT_TEMPERATURE, max_tokens=_voice_token_limit(prep["messages"]),
                stream=True, stream_options={"include_usage": True},
                extra_body=_chat_extra(),
            )
            for chunk in stream:
                # The final usage-only chunk carries usage (with cost), no choices.
                if getattr(chunk, "usage", None):
                    tokens.record(settings.CHAT_MODEL, "chat", chunk.usage,
                                  cost_usd=tokens.usage_cost(chunk.usage))
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    acc.append(delta)
                    yield {"type": "delta", "text": delta}
            # If the fast answer punted, escalate to a real web search and
            # replace it (the reset clears the punt from the UI).
            if prep.get("web_auto") and _looks_like_punt("".join(acc)):
                log.info("Answer punted; escalating to web search (stream).")
                yield {"type": "reset"}
                acc.clear()
                for ev in _agent_stream(_agent_messages(prep), holder):
                    if ev.get("type") == "delta":
                        acc.append(ev["text"])
                    elif ev.get("type") == "reset":
                        acc.clear()
                    yield ev
    except (LLMNotConfigured, APIError, ProviderResponseError):
        log.exception("Streaming failed")
        if not acc:
            ans = _err_message(lang)
            _log(session_token, question, ans, lang, [], "error", settings.CHAT_MODEL,
                 int((time.time() - t0) * 1000))
            yield {"type": "delta", "text": ans}
            yield {"type": "done", "served_from": "error", "sources": []}
            return

    web_sources = holder.get("web_sources") or []
    sources = kb_sources + _web_sources(web_sources)
    served = "web" if web_sources else "rag"
    answer = (holder.get("answer") or "".join(acc)).strip()
    db.bump_session(session_token)
    log_id = _log(session_token, question, answer, lang, sources, served, settings.CHAT_MODEL,
                  int((time.time() - t0) * 1000))
    yield {"type": "done", "served_from": served, "sources": sources, "log_id": log_id}
