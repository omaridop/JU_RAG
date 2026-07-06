"""Offline tests for retrieval dedupe, QA prep, chunking, and token math."""
from app.chatbot.memory_cache import _lexically_equivalent
from app.core import tokens
from app.pipeline.chunker import _split_by_size
from app.pipeline.doc_to_qa import _loads_qa, _normalize_unit, qa_units_to_prepared
from app.pipeline.vectorstore import _dedupe_key


# --- retrieval dedupe (paraphrase fan-out collapses) ----------------------- #
def test_dedupe_key_collapses_same_unit():
    a = _dedupe_key({"doc_id": 1, "unit": 0}, "d1")
    b = _dedupe_key({"doc_id": 1, "unit": 0}, "d2")
    c = _dedupe_key({"doc_id": 1, "unit": 1}, "d3")
    assert a == b and a != c


# --- memory cache lexical guard (one-word swaps rejected) ------------------ #
def test_memory_guard_rejects_program_swap():
    assert not _lexically_equivalent(
        "كم سعر ساعة الموازي لتخصص الذكاء الاصطناعي",
        "كم سعر ساعة التنافس لتخصص الذكاء الاصطناعي")
    assert _lexically_equivalent(
        "شو سعر ساعة التنافس للذكاء الاصطناعي؟",
        "كم سعر ساعة التنافس لتخصص الذكاء الاصطناعي")


# --- QA JSON parsing never returns None ------------------------------------ #
def test_loads_qa_is_always_a_list():
    assert _loads_qa('{"qa": null}') == []
    assert _loads_qa("") == []
    assert _loads_qa('{"qa": [{"x": 1}]}') == [{"x": 1}]


# --- page-number heuristic (batch-local vs absolute) ----------------------- #
def test_normalize_unit_page_mapping():
    u = _normalize_unit({"page": 2, "questions_en": ["q"], "answer_en": "a"},
                        page_offset=3, fallback_page=4, span=3)
    assert u["page"] == 5                      # local 2 within pages 4-6
    u = _normalize_unit({"page": 99, "questions_en": ["q"], "answer_en": "a"},
                        page_offset=3, fallback_page=4, span=3)
    assert u["page"] == 4                      # implausible -> fallback


# --- QA -> prepared records (paraphrases + answer vector, unit tagged) ------ #
def test_qa_units_to_prepared_shapes():
    units = [{"topic": "fees", "page": 2, "questions_en": ["q1", "q2"],
              "answer_en": "ans", "questions_ar": ["س1"], "answer_ar": "جواب"}]
    prep = qa_units_to_prepared(units, doc_id=1, file_hash="h", filename="f",
                                source_label="s", default_date="2026-01-01")
    assert len(prep) == 5                      # 2 en-q + 1 en-ans + 1 ar-q + 1 ar-ans
    assert len({p["id"] for p in prep}) == 5   # ids unique
    assert all(p["metadata"]["unit"] == 0 for p in prep)


# --- deterministic splitter keeps overlap & respects size ------------------ #
def test_split_by_size_overlaps():
    text = "\n\n".join(f"paragraph number {i} with some words" for i in range(40))
    pieces = _split_by_size(text, 200, 40)
    assert len(pieces) > 1
    assert all(len(p) <= 200 + 60 for p in pieces)


# --- token cost & estimate ------------------------------------------------- #
def test_token_cost_and_estimate():
    pricing = {"m": {"input": 1.0, "output": 2.0}}
    assert abs(tokens.cost("m", 1_000_000, 0, pricing) - 1.0) < 1e-9
    assert abs(tokens.cost("m", 0, 1_000_000, pricing) - 2.0) < 1e-9
    assert tokens.estimate_tokens("") == 0
    assert tokens.estimate_tokens("hello world test string") > 0


def test_usage_cost_extracts_openrouter_cost():
    class U:  # mimics the SDK usage object with an extra `cost` field
        cost = 0.00042
    assert tokens.usage_cost(U()) == 0.00042
    assert tokens.usage_cost(None) is None
