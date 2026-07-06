"""SQLite persistence layer.

Holds everything that is *not* a vector: admin accounts, ingested-document
bookkeeping (for incremental ingest + dates), chat sessions, chat logs, the
admin-promoted memory cache, and a small key/value settings store.

A fresh connection is opened per operation (``check_same_thread=False`` + WAL)
which is more than enough for an admin-scale app and avoids cross-thread issues
with Flask's threaded server.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ..config import settings
from .utils import now_iso

# --------------------------------------------------------------------------- #
#  Schema                                                                     #
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin',  -- super | admin
    created_by    TEXT,
    created_at    TEXT NOT NULL
);

-- Audit trail: every admin action, kept even after the admin is deleted
-- (username is denormalized on purpose).
CREATE TABLE IF NOT EXISTS admin_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id   INTEGER,
    username   TEXT NOT NULL,
    action     TEXT NOT NULL,            -- e.g. upload_document, memory_add
    target     TEXT,                     -- what it acted on (filename, id...)
    detail     TEXT,                     -- free-text specifics
    ip         TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_user ON admin_audit(username);

-- Ingested sources (PDF/DOCX and QA JSON files). file_hash gives us
-- idempotent, incremental ingest: a file already present is skipped.
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT NOT NULL,
    stored_path   TEXT NOT NULL,
    file_hash     TEXT UNIQUE NOT NULL,
    file_type     TEXT NOT NULL,                -- pdf | docx | qa
    source_label  TEXT,                         -- human label / category
    doc_date      TEXT NOT NULL,                -- EFFECTIVE date (recency)
    ingested_at   TEXT NOT NULL,                -- system clock at ingest
    status        TEXT NOT NULL DEFAULT 'pending', -- pending|processing|done|error
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    uploaded_by   TEXT,
    lang          TEXT
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_token  TEXT UNIQUE NOT NULL,
    created_at     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    message_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_token TEXT NOT NULL,
    question      TEXT NOT NULL,
    answer        TEXT NOT NULL,
    lang          TEXT,
    sources_json  TEXT,                          -- JSON list of source descriptors
    served_from   TEXT NOT NULL DEFAULT 'rag',   -- rag | memory | limit | error
    model         TEXT,
    latency_ms    INTEGER,
    promoted      INTEGER NOT NULL DEFAULT 0,     -- 1 once turned into a memory
    feedback      INTEGER NOT NULL DEFAULT 0,      -- 1 like, -1 dislike, 0 none
    created_at    TEXT NOT NULL
);

-- Admin-promoted Q&A. A matching question is answered from here WITHOUT
-- calling the LLM (saves tokens). vector_id links to the Chroma memory record.
CREATE TABLE IF NOT EXISTS memory_qa (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    question       TEXT NOT NULL,
    question_norm  TEXT NOT NULL,
    answer         TEXT NOT NULL,
    lang           TEXT,
    sources_json   TEXT,
    vector_id      TEXT,
    source_log_id  INTEGER,
    hits           INTEGER NOT NULL DEFAULT 0,
    created_by     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_norm ON memory_qa(question_norm);
CREATE INDEX IF NOT EXISTS idx_logs_session ON chat_logs(session_token);
CREATE INDEX IF NOT EXISTS idx_logs_created ON chat_logs(created_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Managed list of departments (the super admin curates this; admins are then
-- assigned one of these). Documents inherit their uploader's department.
CREATE TABLE IF NOT EXISTS departments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

-- Per-call token accounting. Costs are derived at read time (tokens x current
-- price), so only raw token counts are stored here.
CREATE TABLE IF NOT EXISTS token_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    model             TEXT NOT NULL,
    operation         TEXT NOT NULL,   -- chat|condense|embed|read|qa|chunk
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,   -- actual (OpenRouter) or priced
    estimated         INTEGER NOT NULL DEFAULT 0, -- 1 = backfilled estimate
    doc_id            INTEGER,         -- set for ingestion calls
    session_token     TEXT             -- set for chat calls
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON token_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_doc ON token_usage(doc_id);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(settings.DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as c:
        c.executescript(_SCHEMA)
        # Lightweight migration: add columns introduced after a DB already existed.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(chat_logs)").fetchall()}
        if "feedback" not in cols:
            c.execute("ALTER TABLE chat_logs ADD COLUMN feedback INTEGER NOT NULL DEFAULT 0")
        dcols = {r["name"] for r in c.execute("PRAGMA table_info(documents)").fetchall()}
        if "progress" not in dcols:
            c.execute("ALTER TABLE documents ADD COLUMN progress TEXT")
        acols = {r["name"] for r in c.execute("PRAGMA table_info(admins)").fetchall()}
        if "role" not in acols:
            c.execute("ALTER TABLE admins ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
            # The original (first) admin becomes the super admin.
            c.execute("UPDATE admins SET role = 'super' "
                      "WHERE id = (SELECT MIN(id) FROM admins)")
        if "created_by" not in acols:
            c.execute("ALTER TABLE admins ADD COLUMN created_by TEXT")
        if "department" not in acols:
            c.execute("ALTER TABLE admins ADD COLUMN department TEXT")
        # Documents: per-department ownership + an active/deactivated flag.
        if "department" not in dcols:
            c.execute("ALTER TABLE documents ADD COLUMN department TEXT")
        if "active" not in dcols:
            c.execute("ALTER TABLE documents ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        ucols = {r["name"] for r in c.execute("PRAGMA table_info(token_usage)").fetchall()}
        if ucols and "cost_usd" not in ucols:
            c.execute("ALTER TABLE token_usage ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0")
        if ucols and "estimated" not in ucols:
            c.execute("ALTER TABLE token_usage ADD COLUMN estimated INTEGER NOT NULL DEFAULT 0")


def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(r) if r is not None else None


def _rows(rs: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rs]


# --------------------------------------------------------------------------- #
#  Admins                                                                     #
# --------------------------------------------------------------------------- #
def create_admin(username: str, password_hash: str, role: str = "admin",
                 created_by: str = "", department: str = "") -> int:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO admins (username, password_hash, role, created_by, department, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (username, password_hash, role, created_by, department or None, now_iso()),
        )
        return int(cur.lastrowid)


def get_admin_by_username(username: str) -> Optional[dict]:
    with get_conn() as c:
        return _row(c.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone())


def get_admin(admin_id: int) -> Optional[dict]:
    with get_conn() as c:
        return _row(c.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone())


def count_admins() -> int:
    with get_conn() as c:
        return int(c.execute("SELECT COUNT(*) AS n FROM admins").fetchone()["n"])


def list_admins() -> list[dict]:
    with get_conn() as c:
        return _rows(c.execute(
            "SELECT id, username, role, department, created_by, created_at FROM admins "
            "ORDER BY (role = 'super') DESC, id"
        ).fetchall())


# --------------------------------------------------------------------------- #
#  Departments (managed list)                                                 #
# --------------------------------------------------------------------------- #
def add_department(name: str) -> None:
    with get_conn() as c:
        c.execute("INSERT OR IGNORE INTO departments (name, created_at) VALUES (?,?)",
                  (name, now_iso()))


def list_departments() -> list[dict]:
    with get_conn() as c:
        return _rows(c.execute("SELECT * FROM departments ORDER BY name").fetchall())


def department_names() -> list[str]:
    """The managed department names (used to populate selectors)."""
    return [d["name"] for d in list_departments()]


def get_department(dept_id: int) -> Optional[dict]:
    with get_conn() as c:
        return _row(c.execute("SELECT * FROM departments WHERE id = ?", (dept_id,)).fetchone())


def department_exists(name: str) -> bool:
    with get_conn() as c:
        return c.execute("SELECT 1 FROM departments WHERE name = ?", (name,)).fetchone() is not None


def delete_department(dept_id: int) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM departments WHERE id = ?", (dept_id,))


def delete_admin(admin_id: int) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM admins WHERE id = ?", (admin_id,))


def update_admin_password(admin_id: int, password_hash: str) -> None:
    with get_conn() as c:
        c.execute("UPDATE admins SET password_hash = ? WHERE id = ?", (password_hash, admin_id))


def update_admin_department(admin_id: int, department: str) -> None:
    with get_conn() as c:
        c.execute("UPDATE admins SET department = ? WHERE id = ?",
                  (department or None, admin_id))


# --------------------------------------------------------------------------- #
#  Admin audit trail                                                           #
# --------------------------------------------------------------------------- #
def add_audit(*, admin_id: Optional[int], username: str, action: str,
              target: str = "", detail: str = "", ip: str = "") -> int:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO admin_audit (admin_id, username, action, target, detail, ip, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (admin_id, username, action, target, detail, ip, now_iso()),
        )
        return int(cur.lastrowid)


def list_audit(limit: int = 50, offset: int = 0, username: str = "",
               action: str = "") -> tuple[list[dict], int]:
    where, params = [], []
    if username:
        where.append("username = ?")
        params.append(username)
    if action:
        where.append("action = ?")
        params.append(action)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with get_conn() as c:
        total = int(c.execute(
            f"SELECT COUNT(*) AS n FROM admin_audit {clause}", params).fetchone()["n"])
        rows = c.execute(
            f"SELECT * FROM admin_audit {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return _rows(rows), total


def audit_usernames() -> list[str]:
    """Distinct usernames appearing in the audit trail (for the filter)."""
    with get_conn() as c:
        return [r["username"] for r in
                c.execute("SELECT DISTINCT username FROM admin_audit ORDER BY username")]


# --------------------------------------------------------------------------- #
#  Documents                                                                  #
# --------------------------------------------------------------------------- #
def add_document(*, filename: str, stored_path: str, file_hash: str, file_type: str,
                 doc_date: str, source_label: str = "", uploaded_by: str = "",
                 lang: str = "", status: str = "pending", department: str = "") -> int:
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO documents
               (filename, stored_path, file_hash, file_type, source_label,
                doc_date, ingested_at, status, chunk_count, uploaded_by, lang,
                department, active)
               VALUES (?,?,?,?,?,?,?,?,0,?,?,?,1)""",
            (filename, stored_path, file_hash, file_type, source_label,
             doc_date, now_iso(), status, uploaded_by, lang, department or None),
        )
        return int(cur.lastrowid)


def get_document(doc_id: int) -> Optional[dict]:
    with get_conn() as c:
        return _row(c.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone())


def get_document_by_hash(file_hash: str) -> Optional[dict]:
    with get_conn() as c:
        return _row(c.execute("SELECT * FROM documents WHERE file_hash = ?", (file_hash,)).fetchone())


def list_documents(department: Optional[str] = None) -> list[dict]:
    """All documents, or only those owned by ``department`` when given."""
    with get_conn() as c:
        if department is None:
            rows = c.execute("SELECT * FROM documents ORDER BY ingested_at DESC").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM documents WHERE department = ? ORDER BY ingested_at DESC",
                (department,)).fetchall()
        return _rows(rows)


def set_document_active(doc_id: int, active: bool) -> None:
    """Activate (bot answers from it) or deactivate (treated as deleted) a document."""
    with get_conn() as c:
        c.execute("UPDATE documents SET active = ? WHERE id = ?",
                  (1 if active else 0, doc_id))


def inactive_doc_ids() -> set[int]:
    """Doc ids that are deactivated — excluded from chatbot retrieval."""
    with get_conn() as c:
        return {int(r["id"]) for r in
                c.execute("SELECT id FROM documents WHERE active = 0").fetchall()}


def update_document(doc_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as c:
        c.execute(f"UPDATE documents SET {cols} WHERE id = ?", (*fields.values(), doc_id))


def delete_document(doc_id: int) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


# --------------------------------------------------------------------------- #
#  Chat sessions & limits                                                     #
# --------------------------------------------------------------------------- #
def get_or_create_session(token: str) -> dict:
    with get_conn() as c:
        row = c.execute("SELECT * FROM chat_sessions WHERE session_token = ?", (token,)).fetchone()
        if row is None:
            ts = now_iso()
            c.execute(
                "INSERT INTO chat_sessions (session_token, created_at, last_seen) VALUES (?,?,?)",
                (token, ts, ts),
            )
            row = c.execute("SELECT * FROM chat_sessions WHERE session_token = ?", (token,)).fetchone()
        return dict(row)


def session_message_count(token: str) -> int:
    with get_conn() as c:
        row = c.execute(
            "SELECT message_count FROM chat_sessions WHERE session_token = ?", (token,)
        ).fetchone()
        return int(row["message_count"]) if row else 0


def bump_session(token: str) -> int:
    """Increment a session's message counter and return the new value."""
    with get_conn() as c:
        c.execute(
            """UPDATE chat_sessions
               SET message_count = message_count + 1, last_seen = ?
               WHERE session_token = ?""",
            (now_iso(), token),
        )
        row = c.execute(
            "SELECT message_count FROM chat_sessions WHERE session_token = ?", (token,)
        ).fetchone()
        return int(row["message_count"]) if row else 0


# --------------------------------------------------------------------------- #
#  Chat logs                                                                  #
# --------------------------------------------------------------------------- #
def add_chat_log(*, session_token: str, question: str, answer: str, lang: str = "",
                 sources_json: str = "", served_from: str = "rag", model: str = "",
                 latency_ms: int = 0) -> int:
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO chat_logs
               (session_token, question, answer, lang, sources_json, served_from,
                model, latency_ms, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (session_token, question, answer, lang, sources_json, served_from,
             model, latency_ms, now_iso()),
        )
        return int(cur.lastrowid)


def list_chat_logs(limit: int = 50, offset: int = 0, served_from: str = "",
                   search: str = "") -> tuple[list[dict], int]:
    where, params = [], []
    if served_from:
        where.append("served_from = ?")
        params.append(served_from)
    if search:
        where.append("(question LIKE ? OR answer LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with get_conn() as c:
        total = int(c.execute(f"SELECT COUNT(*) AS n FROM chat_logs {clause}", params).fetchone()["n"])
        rows = c.execute(
            f"SELECT * FROM chat_logs {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return _rows(rows), total


def get_chat_log(log_id: int) -> Optional[dict]:
    with get_conn() as c:
        return _row(c.execute("SELECT * FROM chat_logs WHERE id = ?", (log_id,)).fetchone())


def recent_logs_for_session(session_token: str, limit: int) -> list[dict]:
    """Most-recent-first logs for one session (for conversation history)."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM chat_logs WHERE session_token = ? ORDER BY id DESC LIMIT ?",
            (session_token, limit),
        ).fetchall()
        return _rows(rows)


def mark_log_promoted(log_id: int) -> None:
    with get_conn() as c:
        c.execute("UPDATE chat_logs SET promoted = 1 WHERE id = ?", (log_id,))


def set_feedback(log_id: int, value: int) -> None:
    """Record student feedback on an answer: 1 = like, -1 = dislike, 0 = clear."""
    value = 1 if value > 0 else (-1 if value < 0 else 0)
    with get_conn() as c:
        c.execute("UPDATE chat_logs SET feedback = ? WHERE id = ?", (value, log_id))


# --------------------------------------------------------------------------- #
#  Memory cache                                                               #
# --------------------------------------------------------------------------- #
def add_memory(*, question: str, question_norm: str, answer: str, lang: str = "",
               sources_json: str = "", vector_id: str = "", source_log_id: Optional[int] = None,
               created_by: str = "") -> int:
    ts = now_iso()
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO memory_qa
               (question, question_norm, answer, lang, sources_json, vector_id,
                source_log_id, hits, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,0,?,?,?)""",
            (question, question_norm, answer, lang, sources_json, vector_id,
             source_log_id, created_by, ts, ts),
        )
        return int(cur.lastrowid)


def find_memory_exact(question_norm: str) -> Optional[dict]:
    with get_conn() as c:
        return _row(c.execute(
            "SELECT * FROM memory_qa WHERE question_norm = ? ORDER BY id DESC LIMIT 1",
            (question_norm,),
        ).fetchone())


def get_memory(mem_id: int) -> Optional[dict]:
    with get_conn() as c:
        return _row(c.execute("SELECT * FROM memory_qa WHERE id = ?", (mem_id,)).fetchone())


def get_memory_by_vector(vector_id: str) -> Optional[dict]:
    with get_conn() as c:
        return _row(c.execute("SELECT * FROM memory_qa WHERE vector_id = ?", (vector_id,)).fetchone())


def list_memory() -> list[dict]:
    with get_conn() as c:
        return _rows(c.execute("SELECT * FROM memory_qa ORDER BY id DESC").fetchall())


def update_memory(mem_id: int, *, question: str, question_norm: str, answer: str) -> None:
    with get_conn() as c:
        c.execute(
            """UPDATE memory_qa SET question = ?, question_norm = ?, answer = ?, updated_at = ?
               WHERE id = ?""",
            (question, question_norm, answer, now_iso(), mem_id),
        )


def increment_memory_hits(mem_id: int) -> None:
    with get_conn() as c:
        c.execute("UPDATE memory_qa SET hits = hits + 1 WHERE id = ?", (mem_id,))


def delete_memory(mem_id: int) -> Optional[dict]:
    """Delete and return the row (so the caller can clean up the vector)."""
    mem = get_memory(mem_id)
    with get_conn() as c:
        c.execute("DELETE FROM memory_qa WHERE id = ?", (mem_id,))
    return mem


# --------------------------------------------------------------------------- #
#  Settings (key/value)                                                       #
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with get_conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_conn() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def get_int_setting(key: str, default: int) -> int:
    val = get_setting(key)
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
#  Token usage                                                                #
# --------------------------------------------------------------------------- #
def add_token_usage(*, model: str, operation: str, prompt_tokens: int,
                    completion_tokens: int, cost_usd: float = 0.0,
                    estimated: int = 0, doc_id: Optional[int] = None,
                    session_token: Optional[str] = None) -> None:
    with get_conn() as c:
        c.execute(
            """INSERT INTO token_usage
               (created_at, model, operation, prompt_tokens, completion_tokens,
                cost_usd, estimated, doc_id, session_token)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (now_iso(), model, operation, prompt_tokens, completion_tokens,
             float(cost_usd or 0.0), int(estimated or 0), doc_id, session_token),
        )


def delete_estimated_usage(doc_id: int) -> None:
    """Drop backfilled estimates for a doc (real tracking supersedes them)."""
    with get_conn() as c:
        c.execute("DELETE FROM token_usage WHERE doc_id=? AND estimated=1", (doc_id,))


def token_month_cost() -> float:
    """Total recorded cost (USD) for the current calendar month."""
    month = now_iso()[:7]  # YYYY-MM
    with get_conn() as c:
        r = c.execute("SELECT COALESCE(SUM(cost_usd),0) AS c FROM token_usage "
                      "WHERE substr(created_at,1,7) = ?", (month,)).fetchone()
        return float(r["c"])


def token_totals(today_only: bool = False) -> dict:
    clause = "WHERE substr(created_at,1,10) = substr(?,1,10)" if today_only else ""
    params = (now_iso(),) if today_only else ()
    with get_conn() as c:
        r = c.execute(
            f"""SELECT COALESCE(SUM(prompt_tokens),0) AS prompt,
                       COALESCE(SUM(completion_tokens),0) AS completion,
                       COALESCE(SUM(cost_usd),0) AS cost,
                       COALESCE(SUM(estimated),0) AS estimated_calls,
                       COUNT(*) AS calls
                FROM token_usage {clause}""", params).fetchone()
        return {"prompt": int(r["prompt"]), "completion": int(r["completion"]),
                "cost": float(r["cost"]), "estimated_calls": int(r["estimated_calls"]),
                "calls": int(r["calls"])}


def token_breakdown(today_only: bool = False) -> list[dict]:
    """Token + cost sums grouped by (model, operation)."""
    clause = "WHERE substr(created_at,1,10) = substr(?,1,10)" if today_only else ""
    params = (now_iso(),) if today_only else ()
    with get_conn() as c:
        return _rows(c.execute(
            f"""SELECT model, operation,
                       COALESCE(SUM(prompt_tokens),0) AS prompt,
                       COALESCE(SUM(completion_tokens),0) AS completion,
                       COALESCE(SUM(cost_usd),0) AS cost,
                       COUNT(*) AS calls
                FROM token_usage {clause}
                GROUP BY model, operation""", params).fetchall())


def token_by_document(limit: int = 30) -> list[dict]:
    """Token + cost sums per document (joined to filename)."""
    with get_conn() as c:
        return _rows(c.execute(
            """SELECT u.doc_id AS doc_id, d.filename AS filename,
                      d.file_type AS file_type,
                      COALESCE(SUM(u.prompt_tokens),0) AS prompt,
                      COALESCE(SUM(u.completion_tokens),0) AS completion,
                      COALESCE(SUM(u.cost_usd),0) AS cost,
                      MAX(u.estimated) AS estimated
               FROM token_usage u
               LEFT JOIN documents d ON d.id = u.doc_id
               WHERE u.doc_id IS NOT NULL
               GROUP BY u.doc_id
               ORDER BY SUM(u.cost_usd) DESC
               LIMIT ?""", (limit,)).fetchall())


# --------------------------------------------------------------------------- #
#  Analytics                                                                  #
# --------------------------------------------------------------------------- #
def chat_feedback_stats() -> dict:
    with get_conn() as c:
        r = c.execute(
            """SELECT
                 SUM(CASE WHEN feedback = 1 THEN 1 ELSE 0 END) AS up,
                 SUM(CASE WHEN feedback = -1 THEN 1 ELSE 0 END) AS down,
                 COUNT(*) AS total
               FROM chat_logs""").fetchone()
        up, down = int(r["up"] or 0), int(r["down"] or 0)
        return {"up": up, "down": down, "rated": up + down, "total": int(r["total"] or 0)}


def served_breakdown() -> list[dict]:
    with get_conn() as c:
        return _rows(c.execute(
            "SELECT served_from, COUNT(*) AS n FROM chat_logs "
            "GROUP BY served_from ORDER BY n DESC").fetchall())


def lang_breakdown() -> list[dict]:
    with get_conn() as c:
        return _rows(c.execute(
            "SELECT COALESCE(NULLIF(lang,''),'?') AS lang, COUNT(*) AS n FROM chat_logs "
            "GROUP BY lang ORDER BY n DESC").fetchall())


def chats_per_day(days: int = 14) -> list[dict]:
    with get_conn() as c:
        return _rows(c.execute(
            """SELECT substr(created_at,1,10) AS day, COUNT(*) AS n
               FROM chat_logs GROUP BY day ORDER BY day DESC LIMIT ?""",
            (days,)).fetchall())


def recent_rag_logs(limit: int = 600) -> list[dict]:
    """Recent RAG/memory answers for top-question + gap analysis."""
    with get_conn() as c:
        return _rows(c.execute(
            "SELECT question, lang, sources_json, served_from, feedback FROM chat_logs "
            "WHERE served_from IN ('rag','memory') ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall())


# --------------------------------------------------------------------------- #
#  Dashboard stats                                                            #
# --------------------------------------------------------------------------- #
def get_dashboard_stats() -> dict:
    with get_conn() as c:
        docs = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(chunk_count),0) AS chunks FROM documents "
            "WHERE file_type IN ('pdf','docx','txt')"
        ).fetchone()
        qa = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(chunk_count),0) AS pairs FROM documents "
            "WHERE file_type = 'qa'"
        ).fetchone()
        chats = c.execute("SELECT COUNT(*) AS n FROM chat_logs").fetchone()
        today = c.execute(
            "SELECT COUNT(*) AS n FROM chat_logs WHERE substr(created_at,1,10) = substr(?,1,10)",
            (now_iso(),),
        ).fetchone()
        from_mem = c.execute(
            "SELECT COUNT(*) AS n FROM chat_logs WHERE served_from = 'memory'"
        ).fetchone()
        mem = c.execute("SELECT COUNT(*) AS n FROM memory_qa").fetchone()
        sessions = c.execute("SELECT COUNT(*) AS n FROM chat_sessions").fetchone()
    return {
        "documents": int(docs["n"]),
        "doc_chunks": int(docs["chunks"]),
        "qa_files": int(qa["n"]),
        "qa_pairs": int(qa["pairs"]),
        "chats_total": int(chats["n"]),
        "chats_today": int(today["n"]),
        "chats_from_memory": int(from_mem["n"]),
        "memory_entries": int(mem["n"]),
        "sessions": int(sessions["n"]),
    }
