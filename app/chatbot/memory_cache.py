"""Admin-curated answer cache ("memory").

When an admin promotes a chat answer (or adds one manually), the question is
stored two ways:
  * normalized text in SQLite  -> instant, FREE exact-match lookup
  * an embedding in Chroma     -> semantic lookup for paraphrased questions

A memory hit is answered WITHOUT calling the chat model, saving tokens.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from ..config import settings
from ..core import db
from ..core.utils import detect_lang, new_uuid, normalize_text
from ..pipeline.embed import embed_query
from ..pipeline.vectorstore import get_store

log = logging.getLogger(__name__)


def _result(row: dict, match: str, similarity: float) -> dict:
    db.increment_memory_hits(row["id"])
    try:
        sources = json.loads(row.get("sources_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        sources = []
    return {
        "answer": row["answer"],
        "lang": row.get("lang") or detect_lang(row["answer"]),
        "mem_id": row["id"],
        "match": match,
        "similarity": similarity,
        "sources": sources,
    }


def lookup_exact(question: str) -> Optional[dict]:
    """Free exact-match lookup on the normalized question."""
    row = db.find_memory_exact(normalize_text(question))
    return _result(row, "exact", 1.0) if row else None


# Filler words ignored when comparing question content. Embeddings score
# «سعر ساعة الموازي للذكاء الاصطناعي» vs «سعر ساعة التنافس للذكاء الاصطناعي»
# above 0.92 although they ask about DIFFERENT programs — the lexical guard
# below blocks such one-word content swaps from hijacking the cache.
_STOP_RAW = [
    "شو", "ما", "ماذا", "كم", "هل", "هو", "هي", "وش", "ايش", "أيش", "لماذا",
    "من", "في", "عن", "على", "الى", "إلى", "لو", "سمحت", "أرجو", "رجاء", "يا",
    "تخصص", "مادة", "مقرر", "بكم", "قديش", "اديش",
    "what", "is", "are", "the", "how", "much", "many", "for", "of", "a", "an",
    "in", "to", "please", "about", "tell", "me", "do", "does", "i", "want",
]
# Light Arabic clitic stripping so «للذكاء/الذكاء/بالذكاء» compare equal.
# Both sides of every comparison get the same treatment, so the occasional
# over-strip stays symmetric and harmless.
_PREFIXES = ("وال", "فال", "بال", "كال", "لل", "ال", "و", "ف", "ب", "ك", "ل")


def _stem(tok: str) -> str:
    changed = True
    while changed and len(tok) > 2:
        changed = False
        for p in _PREFIXES:
            if tok.startswith(p) and len(tok) - len(p) >= 2:
                tok = tok[len(p):]
                changed = True
                break
    return tok


_STOP = {_stem(normalize_text(w)) for w in _STOP_RAW}


def _content_tokens(text: str) -> set[str]:
    out = set()
    for t in normalize_text(text).split():
        t = _stem(t)
        if len(t) > 1 and t not in _STOP:
            out.add(t)
    return out


def _lexically_equivalent(q1: str, q2: str) -> bool:
    """True when two questions carry the same content words (any order)."""
    return _content_tokens(q1) == _content_tokens(q2)


def lookup_semantic(question: str, query_embedding: list[float]) -> Optional[dict]:
    """Semantic lookup; reuses the embedding already computed for retrieval.

    A hit must clear the similarity threshold AND carry the same content
    words as the cached question — high cosine similarity alone cannot
    distinguish questions that differ in one critical word (program type,
    major name, year).
    """
    hits = get_store().memory_search(query_embedding, k=1)
    if not hits:
        return None
    top = hits[0]
    if top["similarity"] < settings.MEMORY_SIM_THRESHOLD:
        return None
    row = db.get_memory_by_vector(top["id"])
    if not row:
        return None
    if not _lexically_equivalent(question, row["question"]):
        log.info("Memory semantic hit rejected by lexical guard: %r vs cached %r "
                 "(sim %.3f)", question, row["question"], top["similarity"])
        return None
    return _result(row, "semantic", top["similarity"])


# --------------------------------------------------------------------------- #
#  Mutations (admin actions)                                                  #
# --------------------------------------------------------------------------- #
def _index_vector(mem_id: int, question: str, answer: str, lang: str) -> str:
    vector_id = new_uuid()
    embedding = embed_query(question)
    get_store().memory_add(
        vector_id, embedding, document=answer,
        metadata={"mem_id": mem_id, "lang": lang, "question": question},
    )
    return vector_id


def promote_log(log_id: int, created_by: str = "") -> int:
    """Turn an existing chat log into a reusable memory entry."""
    chat = db.get_chat_log(log_id)
    if not chat:
        raise ValueError(f"Chat log #{log_id} not found.")
    question, answer = chat["question"], chat["answer"]
    lang = chat.get("lang") or detect_lang(question)
    mem_id = db.add_memory(
        question=question, question_norm=normalize_text(question), answer=answer,
        lang=lang, sources_json=chat.get("sources_json") or "[]",
        source_log_id=log_id, created_by=created_by,
    )
    vector_id = _index_vector(mem_id, question, answer, lang)
    db.update_memory(mem_id, question=question, question_norm=normalize_text(question),
                     answer=answer)  # no-op for text; keeps updated_at fresh
    _set_vector_id(mem_id, vector_id)
    db.mark_log_promoted(log_id)
    log.info("Promoted chat log #%s to memory #%s", log_id, mem_id)
    return mem_id


def add_manual(question: str, answer: str, *, lang: str = "", created_by: str = "") -> int:
    lang = lang or detect_lang(question)
    mem_id = db.add_memory(
        question=question, question_norm=normalize_text(question), answer=answer,
        lang=lang, created_by=created_by,
    )
    vector_id = _index_vector(mem_id, question, answer, lang)
    _set_vector_id(mem_id, vector_id)
    return mem_id


def update(mem_id: int, *, question: str, answer: str) -> None:
    row = db.get_memory(mem_id)
    if not row:
        raise ValueError(f"Memory #{mem_id} not found.")
    lang = row.get("lang") or detect_lang(question)
    db.update_memory(mem_id, question=question, question_norm=normalize_text(question),
                     answer=answer)
    # Re-embed using the SAME vector id (upsert overwrites in place).
    vector_id = row.get("vector_id") or new_uuid()
    embedding = embed_query(question)
    get_store().memory_add(
        vector_id, embedding, document=answer,
        metadata={"mem_id": mem_id, "lang": lang, "question": question},
    )
    _set_vector_id(mem_id, vector_id)


def delete(mem_id: int) -> None:
    row = db.delete_memory(mem_id)
    if row and row.get("vector_id"):
        get_store().memory_delete([row["vector_id"]])


def _set_vector_id(mem_id: int, vector_id: str) -> None:
    with db.get_conn() as c:
        c.execute("UPDATE memory_qa SET vector_id = ? WHERE id = ?", (vector_id, mem_id))
