"""Offline tests for the JU web-search tool's pure logic (no network)."""
from app.chatbot import rag, web_search


# --- domain scoping (only ju.edu.jo is ever cited) ------------------------- #
def test_in_domain_accepts_ju_and_subdomains():
    assert web_search._in_domain("https://ju.edu.jo/Home.aspx")
    assert web_search._in_domain("https://www.ju.edu.jo/x")
    assert web_search._in_domain("https://registration.ju.edu.jo/a/b")


def test_in_domain_rejects_lookalikes():
    assert not web_search._in_domain("https://ju.edu.jo.evil.com/x")
    assert not web_search._in_domain("https://notju.edu.jo.attacker.net/")
    assert not web_search._in_domain("https://example.com/ju.edu.jo")


# --- DuckDuckGo redirect resolution ---------------------------------------- #
def test_resolve_ddg_unwraps_redirect():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fju.edu.jo%2FPages%2FHow.aspx&rut=abc"
    assert web_search._resolve_ddg(href) == "https://ju.edu.jo/Pages/How.aspx"
    assert web_search._resolve_ddg("//ju.edu.jo/x").startswith("https://")
    assert web_search._resolve_ddg("https://ju.edu.jo/y") == "https://ju.edu.jo/y"


# --- HTML → text extraction ------------------------------------------------ #
def test_html_to_text_strips_scripts_and_unescapes():
    html = "<html><head><style>x{}</style></head><body><script>bad()</script>" \
           "<p>Fees are 70&nbsp;JD &amp; up</p></body></html>"
    text = web_search._html_to_text(html)
    assert "bad()" not in text and "x{}" not in text
    assert "Fees are 70" in text and "& up" in text


# --- annotation citation extraction (dict + object) ------------------------ #
def test_annotation_citation_dict_and_object():
    d = {"url_citation": {"url": "https://ju.edu.jo/a", "title": "T", "content": "C"}}
    out = web_search._annotation_citation(d)
    assert out["url"] == "https://ju.edu.jo/a" and out["title"] == "T"

    class Cit:
        url, title, content = "https://ju.edu.jo/b", "T2", "C2"

    class Ann:
        url_citation = Cit()
    assert web_search._annotation_citation(Ann())["url"] == "https://ju.edu.jo/b"


# --- source formatting for the UI ------------------------------------------ #
def test_web_sources_formatting():
    out = rag._web_sources([{"url": "https://ju.edu.jo/x", "title": "Page X"}])
    assert out == [{"label": "Page X", "url": "https://ju.edu.jo/x",
                    "type": "web", "date": "", "page": None}]


# --- enable switch respects the admin setting ------------------------------ #
def test_web_enabled_respects_setting(monkeypatch):
    monkeypatch.setattr(rag.db, "get_setting", lambda k: "false")
    assert rag._web_enabled() is False
    monkeypatch.setattr(rag.db, "get_setting", lambda k: "true")
    assert rag._web_enabled() is True


def _prep_mocks(monkeypatch):
    """Common monkeypatches so _prepare reaches the retrieval step offline."""
    monkeypatch.setattr(rag, "_web_enabled", lambda: True)               # feature available
    monkeypatch.setattr(rag.db, "get_int_setting", lambda k, d: 9999)    # under limit
    monkeypatch.setattr(rag.memory_cache, "lookup_exact", lambda q: None)
    monkeypatch.setattr(rag.memory_cache, "lookup_semantic", lambda q, e: None)
    monkeypatch.setattr(rag, "embed_query", lambda q: [0.0])
    monkeypatch.setattr(rag.db, "session_message_count", lambda t: 0)
    monkeypatch.setattr(rag, "_monthly_budget", lambda: 0.0)
    monkeypatch.setattr(rag.db, "inactive_doc_ids", lambda: set())


def _store_returning(hits):
    class _Store:
        def search(self, *a, **k):
            return list(hits)
    return _Store()


# --- per-message gating: needs BOTH availability AND the student opt-in ----- #
def test_web_on_requires_optin_and_availability(monkeypatch):
    _prep_mocks(monkeypatch)
    monkeypatch.setattr(rag.settings, "WEB_SEARCH_AUTO", False)  # isolate the opt-in logic
    # Strong KB hit so the auto-fallback wouldn't trigger anyway.
    monkeypatch.setattr(rag, "get_store",
                        lambda: _store_returning([{"similarity": 0.9, "metadata": {}, "document": "x"}]))

    assert rag._prepare("q", "tok", use_web=True)["web_on"] is True
    assert rag._prepare("q", "tok", use_web=False)["web_on"] is False    # not opted in
    monkeypatch.setattr(rag, "_web_enabled", lambda: False)              # admin disabled
    assert rag._prepare("q", "tok", use_web=True)["web_on"] is False


# --- auto-search: armed when available, escalates on a punt ----------------- #
def test_web_auto_flag(monkeypatch):
    _prep_mocks(monkeypatch)
    monkeypatch.setattr(rag, "get_store", lambda: _store_returning([]))

    monkeypatch.setattr(rag.settings, "WEB_SEARCH_AUTO", True)
    p = rag._prepare("q", "tok", use_web=False)
    assert p["web_on"] is False and p["web_auto"] is True     # auto escalation armed
    p = rag._prepare("q", "tok", use_web=True)
    assert p["web_on"] is True and p["web_auto"] is False      # opt-in -> agent directly

    monkeypatch.setattr(rag.settings, "WEB_SEARCH_AUTO", False)
    p = rag._prepare("q", "tok", use_web=False)
    assert p["web_on"] is False and p["web_auto"] is False     # no auto, no opt-in

    monkeypatch.setattr(rag.settings, "WEB_SEARCH_AUTO", True)
    monkeypatch.setattr(rag, "_web_enabled", lambda: False)
    p = rag._prepare("q", "tok", use_web=False)
    assert p["web_on"] is False and p["web_auto"] is False     # admin master switch off


# --- punt detection: the reliable trigger for escalating to a web search ----- #
def test_looks_like_punt():
    assert rag._looks_like_punt("The provided context does not contain information about new programs.")
    assert rag._looks_like_punt("I don't have that; please contact the Admission and Registration Unit.")
    assert rag._looks_like_punt("لا تتوفر لدي هذه المعلومات؛ يرجى التواصل مع دائرة القبول والتسجيل.")
    assert rag._looks_like_punt("راجع الموقع الرسمي للجامعة لمزيد من التفاصيل.")
    assert not rag._looks_like_punt("The parallel-program credit-hour fee is 75 JOD.")
    assert not rag._looks_like_punt("رسوم الساعة المعتمدة للبرنامج الموازي هي 75 ديناراً.")


def test_strip_tool_leak_removes_dsml():
    leak = ('<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name="search_ju_website">'
            '<｜｜DSML｜｜parameter name="query" string="true">new programs</｜｜DSML｜｜parameter>'
            '</｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>')
    assert "DSML" not in rag._strip_tool_leak(leak)
    assert rag._strip_tool_leak("Tuition is 75 JOD.") == "Tuition is 75 JOD."
