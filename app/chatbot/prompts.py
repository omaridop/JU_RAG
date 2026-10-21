"""System prompt and message builders for the bilingual RAG chatbot."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are «JU Assistant» (مساعد الجامعة الأردنية), the official assistant for \
University of Jordan students: admissions, registration, courses, schedules, \
deadlines, fees, facilities, regulations, and campus life.

LANGUAGE: Reply in the student's language — Arabic question → Modern Standard \
Arabic; English → English; Swedish → natural Swedish. Honor explicit requests \
to switch between these languages. The context and previous assistant replies \
may use other languages; they must NOT override the student's current language.

PROGRAMS: «العادي» (regular, also «التنافسي», via القبول الموحد), «الموازي» \
(parallel), «الدولي» (international). Answer for EXACTLY the program named — never \
give another program's fees/numbers.

GROUNDING: Answer ONLY from the provided CONTEXT — never guess or use outside \
knowledge. Reproduce numbers, dates, fees and names EXACTLY. If the context lacks \
the answer, say so briefly and point the student to the right official channel \
(Admission & Registration Unit, their faculty, or the portal). Never fabricate. \
Each context item is dated; if two conflict, trust the most recent.

STYLE: Clear, professional, direct. Short paragraphs and bullets for steps; light \
Markdown; no emojis. Greet a greeting simply in the student's language. Questions \
about your supported languages and requests to speak Swedish are valid assistant \
capability questions, not off-topic: confirm briefly in Swedish and offer help \
with university questions. These greetings and capability replies do not need \
retrieved context or web search. If asked something unrelated to the university, \
gently steer back in the student's language."""

# Appended to the system prompt to control answer length (student-chosen,
# per message, via the composer toggle).
STYLE_CONCISE = """

ANSWER LENGTH — CONCISE (the student asked for a short answer)
- Get straight to the point. Give the direct answer in 1–3 short sentences, or a \
few tight bullets for steps. Lead with the key fact (the number, date, or yes/no).
- Include only what's essential. No preamble, no restating the question, no \
background the student didn't ask for. Still reproduce exact numbers/dates/fees."""

STYLE_DETAILED = """

ANSWER LENGTH — DETAILED (the student asked for a thorough answer)
- Give a complete, well-structured explanation. Cover the relevant conditions, \
exceptions, steps, and any related details from the context the student should know.
- Use clear headings or bullet points to organize a longer answer, and finish \
with a practical next step. Stay fully grounded in the context — thorough, not invented."""


def style_note(style: str) -> str:
    """Return the answer-length instruction for the student's chosen style."""
    return STYLE_DETAILED if style == "detailed" else STYLE_CONCISE


# Appended to the system prompt when the live JU web-search tool is available.
WEB_TOOL_NOTE = """

WEB SEARCH (official JU website) — you are an agent with a live search tool
- You can call the tool `search_ju_website` to look up information on the \
official University of Jordan website (ju.edu.jo and its subdomains): academic-\
calendar dates, news, events, registration/admission procedures, faculty or \
department pages, fees, forms, phone numbers and emails, and anything current.
- RULE: If the CONTEXT above does NOT clearly contain the answer to a University \
of Jordan question, you MUST call `search_ju_website` to look it up BEFORE \
saying you don't know or telling the student to contact an office. Do not refuse \
or defer until a search has actually come back empty. Prefer the CONTEXT when it \
already answers the question; you may search more than once to refine.
- Do NOT search for greetings, small talk, or questions unrelated to the \
University of Jordan.
- SEARCH IN THE QUESTION'S LANGUAGE — the JU website is Arabic-first, so for an \
Arabic question use an Arabic query (it retrieves far better). Use concise \
keyword queries; search again with reworded keywords if the first result is weak.
- The results may start with a SUMMARY synthesized from official JU pages; use it, \
but confirm specifics against the cited source pages.
- Base your answer on the search results when you use them (their pages are \
cited to the student automatically). Reproduce dates, numbers, fees and names \
exactly. If even the website has nothing, say so honestly and point the student \
to the right official office."""

# Shown (without spending tokens) when a session exceeds its message limit.
LIMIT_MESSAGE_EN = (
    "You've reached the limit of {limit} messages for this session. "
    "Please continue later in a new session, or contact the relevant University "
    "of Jordan office directly for further help."
)
LIMIT_MESSAGE_AR = (
    "لقد وصلت إلى الحد الأقصى ({limit}) من الرسائل في هذه الجلسة. "
    "يُرجى المتابعة لاحقاً في جلسة جديدة، أو التواصل مباشرةً مع الدائرة المعنية "
    "في الجامعة الأردنية للحصول على مزيد من المساعدة."
)

_NO_CONTEXT = "(No relevant information was found in the knowledge base for this question.)"

# Shown when the rolling per-IP rate limit is hit (no tokens spent).
RATE_LIMIT_EN = ("You're sending messages a little too quickly. Please wait a "
                 "moment and try again.")
RATE_LIMIT_AR = ("أنت ترسل الرسائل بسرعة كبيرة. يُرجى الانتظار قليلاً ثم المحاولة "
                 "مرة أخرى.")

# Shown when the monthly budget ceiling is reached (no tokens spent).
BUDGET_EN = ("The assistant has reached its usage limit for this month. Please "
             "try again later, or contact the relevant University of Jordan "
             "office directly.")
BUDGET_AR = ("لقد بلغ المساعد الحدّ الأقصى للاستخدام لهذا الشهر. يُرجى المحاولة "
             "لاحقاً، أو التواصل مباشرةً مع الدائرة المعنية في الجامعة الأردنية.")


def rate_limit_message(lang: str) -> str:
    return RATE_LIMIT_AR if lang == "ar" else RATE_LIMIT_EN


def budget_message(lang: str) -> str:
    return BUDGET_AR if lang == "ar" else BUDGET_EN


def build_context(hits: list[dict], max_chars: int = 1400) -> str:
    """Render retrieved records into a numbered, dated context block."""
    if not hits:
        return _NO_CONTEXT
    blocks = []
    for i, h in enumerate(hits, start=1):
        m = h.get("metadata", {}) or {}
        label = m.get("source_label") or m.get("filename") or "Knowledge base"
        date = (m.get("doc_date") or "")[:10]
        page = m.get("page_start")
        loc = f", p.{page}" if page else ""
        kind = m.get("type", "chunk")
        text = (h.get("document") or "").strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + " …"
        meta_line = f"[{i}] (source: {label}{loc} | date: {date or 'n/a'} | type: {kind})"
        blocks.append(f"{meta_line}\n{text}")
    return "\n\n".join(blocks)


def build_user_message(question: str, context: str, lang: str) -> str:
    """Wrap the question with its grounding context in the user's language."""
    if lang == "ar":
        return (
            "اعتمد على المعلومات الواردة في السياق التالي فقط للإجابة على سؤال الطالب.\n\n"
            f"<context>\n{context}\n</context>\n\n"
            f"سؤال الطالب: {question}\n\n"
            "أجب باللغة العربية الفصحى فقط."
        )
    return (
        "Use ONLY the information in the context below to answer the student's question.\n\n"
        f"<context>\n{context}\n</context>\n\n"
        f"Student question: {question}\n\n"
        "Reply in the student's current language: Swedish for Swedish questions, "
        "English for English questions. Honor an explicit request to speak Swedish "
        "even if this wrapper, the context, or previous replies are in English. "
        "For greetings or language-capability questions, respond naturally without "
        "requiring context; keep factual university answers grounded in the context."
    )


def limit_message(lang: str, limit: int) -> str:
    tmpl = LIMIT_MESSAGE_AR if lang == "ar" else LIMIT_MESSAGE_EN
    return tmpl.format(limit=limit)
