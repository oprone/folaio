"""The Memory: one SQLite file holding documents, their passages, summaries
and quiz cards. (What Folaio *learned* lives next to it, in mind/mind.npz.)
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass

from . import config

SCHEMA_VERSION = 4
SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reuse ids of removed documents
    name TEXT NOT NULL,
    file TEXT NOT NULL,
    sha256 TEXT UNIQUE NOT NULL,
    pages INTEGER DEFAULT 0,
    status TEXT DEFAULT 'queued',      -- queued | reading | learning | ready | error
    notes_done INTEGER DEFAULT 0,      -- Folaio Core made its summaries and quizzes
    study_status TEXT DEFAULT 'none',  -- Folaio Plus notes: none | queued | working | done
    study_done INTEGER DEFAULT 0,
    study_total INTEGER DEFAULT 0,
    error TEXT,
    exam_date TEXT,                    -- optional, YYYY-MM-DD
    kind TEXT DEFAULT 'book',          -- book | paper (a past exam paper)
    year INTEGER,                      -- past papers: the exam year, if found
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE TABLE IF NOT EXISTS sections (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_from INTEGER NOT NULL,
    page_to INTEGER NOT NULL,
    summary TEXT NOT NULL,
    source TEXT DEFAULT 'core'  -- core | plus
);
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    question TEXT NOT NULL,
    choices TEXT NOT NULL,      -- JSON list
    answer INTEGER NOT NULL,    -- index into choices
    explanation TEXT,
    source TEXT DEFAULT 'core', -- core | plus
    box INTEGER DEFAULT 0,      -- spaced-repetition level
    due_at REAL DEFAULT 0,
    seen INTEGER DEFAULT 0,
    correct INTEGER DEFAULT 0
);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS reviews (   -- one row per answered question: streaks and daily progress
    id INTEGER PRIMARY KEY,
    card_id INTEGER,
    doc_id INTEGER,
    correct INTEGER NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_at ON reviews(at);
CREATE TABLE IF NOT EXISTS paper_questions (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    number TEXT NOT NULL,        -- "3(a)"
    text TEXT NOT NULL,          -- the question as printed
    context TEXT,                -- the main question's opening text, for sub-parts
    page INTEGER,
    marks INTEGER,
    book_id INTEGER,             -- the textbook chapter that answers it (if any)
    section INTEGER,
    score REAL
);
"""

# Days until a card is shown again, per spaced-repetition box.
BOX_DAYS = [0, 1, 3, 7, 14, 30, 60]


@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    doc_name: str
    page: int
    text: str
    score: float


class Memory:
    def __init__(self, path=config.DB_PATH):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self):
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            # v1 stored downloaded-model fingerprints; drop them and re-read every PDF.
            self._db.executescript("""
                DROP TRIGGER IF EXISTS chunks_ai; DROP TRIGGER IF EXISTS chunks_ad;
                DROP TABLE IF EXISTS chunks_fts; DROP TABLE IF EXISTS chunks;
                DROP TABLE IF EXISTS sections; DROP TABLE IF EXISTS cards;""")
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(documents)")}
            if cols and "notes_done" not in cols:
                self._db.execute("ALTER TABLE documents ADD COLUMN notes_done INTEGER DEFAULT 0")
            if cols:
                self._db.execute("UPDATE documents SET status='queued', notes_done=0, study_status='none'")
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(documents)")}
        if cols and "exam_date" not in cols:   # v2 -> v3
            self._db.execute("ALTER TABLE documents ADD COLUMN exam_date TEXT")
        if cols and "kind" not in cols:        # v3 -> v4
            self._db.execute("ALTER TABLE documents ADD COLUMN kind TEXT DEFAULT 'book'")
            self._db.execute("ALTER TABLE documents ADD COLUMN year INTEGER")
        self._db.executescript(SCHEMA)
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.commit()

    # ---------- helpers ----------
    def _q(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql, args=()):
        with self._lock, self._db:
            return self._db.execute(sql, args)

    # ---------- documents ----------
    def add_document(self, name: str, file: str, sha256: str, kind: str = "book") -> int | None:
        """Returns the new id, or None if this exact file is already in the library."""
        try:
            return self._x(
                "INSERT INTO documents(name, file, sha256, kind, added_at) VALUES (?,?,?,?,?)",
                (name, file, sha256, kind, time.time()),
            ).lastrowid
        except sqlite3.IntegrityError:
            return None

    def update_document(self, doc_id: int, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE documents SET {cols} WHERE id=?", (*fields.values(), doc_id))

    def documents(self) -> list[dict]:
        rows = self._q("""
            SELECT d.*, (SELECT COUNT(*) FROM chunks c WHERE c.doc_id=d.id) AS chunks,
                        (SELECT COUNT(*) FROM cards k WHERE k.doc_id=d.id) AS cards
            FROM documents d ORDER BY added_at DESC""")
        return [dict(r) for r in rows]

    def document(self, doc_id: int) -> dict | None:
        rows = self._q("SELECT * FROM documents WHERE id=?", (doc_id,))
        return dict(rows[0]) if rows else None

    def delete_document(self, doc_id: int):
        self._x("DELETE FROM documents WHERE id=?", (doc_id,))

    # ---------- passages ----------
    def add_chunks(self, doc_id: int, pages: list[int], texts: list[str]):
        with self._lock, self._db:
            self._db.executemany("INSERT INTO chunks(doc_id, page, text) VALUES (?,?,?)",
                                 [(doc_id, p, t) for p, t in zip(pages, texts)])

    def clear_chunks(self, doc_id: int):
        self._x("DELETE FROM chunks WHERE doc_id=?", (doc_id,))

    def chunks_for(self, doc_id: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT id, page, text FROM chunks WHERE doc_id=? ORDER BY id", (doc_id,))]

    def library_chunks(self) -> list[dict]:
        """Every passage of every readable document, in reading order (what Folaio learns)."""
        return [dict(r) for r in self._q("""
            SELECT c.id, c.doc_id, c.page, c.text FROM chunks c JOIN documents d ON d.id=c.doc_id
            WHERE d.status IN ('learning', 'ready') AND d.kind='book' ORDER BY c.doc_id, c.id""")]

    # ---------- past exam papers ----------
    def set_questions(self, doc_id: int, questions: list[dict]):
        with self._lock, self._db:
            self._db.execute("DELETE FROM paper_questions WHERE doc_id=?", (doc_id,))
            self._db.executemany(
                "INSERT INTO paper_questions(doc_id, number, text, context, page, marks) VALUES (?,?,?,?,?,?)",
                [(doc_id, q["number"], q["text"], q.get("context"), q["page"], q.get("marks"))
                 for q in questions])

    def questions(self, doc_id: int | None = None) -> list[dict]:
        sql = """SELECT q.*, d.name AS paper, d.year FROM paper_questions q
                 JOIN documents d ON d.id=q.doc_id WHERE d.status='ready'"""
        args = []
        if doc_id:
            sql += " AND q.doc_id=?"
            args.append(doc_id)
        return [dict(r) for r in self._q(sql + " ORDER BY d.year, d.id, q.id", args)]

    def match_question(self, qid: int, book_id: int | None, section: int | None, score: float | None):
        self._x("UPDATE paper_questions SET book_id=?, section=?, score=? WHERE id=?",
                (book_id, section, score, qid))

    def hits(self, found: list) -> list[Hit]:
        """Turn the Mind's (chunk_id, score) results into full passages."""
        if not found:
            return []
        marks = ",".join("?" * len(found))
        rows = {r["id"]: r for r in self._q(f"""
            SELECT c.id, c.doc_id, c.page, c.text, d.name FROM chunks c
            JOIN documents d ON d.id=c.doc_id WHERE c.id IN ({marks})""", [f.chunk_id for f in found])}
        return [Hit(f.chunk_id, rows[f.chunk_id]["doc_id"], rows[f.chunk_id]["name"], rows[f.chunk_id]["page"],
                    rows[f.chunk_id]["text"], f.score) for f in found if f.chunk_id in rows]

    # ---------- study material ----------
    def add_section(self, doc_id: int, page_from: int, page_to: int, summary: str, source: str):
        self._x("INSERT INTO sections(doc_id, page_from, page_to, summary, source) VALUES (?,?,?,?,?)",
                (doc_id, page_from, page_to, summary, source))

    def sections(self, doc_id: int) -> list[dict]:
        """Plus summaries when they exist (written in its own words), otherwise Core's."""
        rows = self._q("SELECT * FROM sections WHERE doc_id=? ORDER BY source='core', page_from", (doc_id,))
        best = rows[0]["source"] if rows else None
        return [dict(r) for r in rows if r["source"] == best]

    def all_sections(self, doc_id: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM sections WHERE doc_id=? ORDER BY source, page_from, id", (doc_id,))]

    def all_cards(self, doc_id: int) -> list[dict]:
        rows = [dict(r) for r in self._q("SELECT * FROM cards WHERE doc_id=? ORDER BY id", (doc_id,))]
        for r in rows:
            r["choices"] = json.loads(r["choices"])
        return rows

    def document_by_sha(self, sha256: str) -> dict | None:
        rows = self._q("SELECT * FROM documents WHERE sha256=?", (sha256,))
        return dict(rows[0]) if rows else None

    def clear_study(self, doc_id: int, source: str):
        self._x("DELETE FROM sections WHERE doc_id=? AND source=?", (doc_id, source))
        self._x("DELETE FROM cards WHERE doc_id=? AND source=?", (doc_id, source))

    def add_card(self, doc_id: int, page: int, question: str, choices: list[str],
                 answer: int, explanation: str, source: str):
        self._x("""INSERT INTO cards(doc_id, page, question, choices, answer, explanation, source)
                   VALUES (?,?,?,?,?,?,?)""",
                (doc_id, page, question, json.dumps(choices), answer, explanation, source))

    def cards_for(self, doc_id: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT id, page, question, explanation, box, seen, correct, due_at FROM cards WHERE doc_id=?",
            (doc_id,))]

    def next_card(self, doc_id: int | None = None, card_ids: list[int] | None = None) -> dict | None:
        """The next question that's due. With `card_ids` ("practise this section"),
        any of those questions, due and weakest first."""
        now = time.time()
        sql = """SELECT k.*, d.name AS doc_name FROM cards k JOIN documents d ON d.id=k.doc_id WHERE 1"""
        args: list = []
        if doc_id:
            sql += " AND k.doc_id=?"
            args.append(doc_id)
        if card_ids is not None:
            if not card_ids:
                return None
            sql += f" AND k.id IN ({','.join('?' * len(card_ids))}) ORDER BY k.due_at > ?, k.box, RANDOM() LIMIT 1"
            args += [*card_ids, now]
        else:
            sql += " AND k.due_at <= ? ORDER BY k.due_at, RANDOM() LIMIT 1"
            args.append(now)
        rows = self._q(sql, args)
        if not rows:
            return None
        card = dict(rows[0])
        card["choices"] = json.loads(card["choices"])
        return card

    def grade_card(self, card_id: int, correct: bool):
        row = self._q("SELECT box FROM cards WHERE id=?", (card_id,))
        if not row:
            return
        box = min(row[0]["box"] + 1, len(BOX_DAYS) - 1) if correct else 0
        # Wrong answers come back in 10 minutes; right ones after BOX_DAYS.
        wait = BOX_DAYS[box] * 86400 if correct else 600
        self._x("""UPDATE cards SET box=?, due_at=?, seen=seen+1, correct=correct+?
                   WHERE id=?""", (box, time.time() + wait, int(correct), card_id))
        self._x("""INSERT INTO reviews(card_id, doc_id, correct, at)
                   SELECT id, doc_id, ?, ? FROM cards WHERE id=?""", (int(correct), time.time(), card_id))

    def answered_since(self, t: float) -> dict:
        r = self._q("SELECT COUNT(*) AS n, SUM(correct) AS right FROM reviews WHERE at >= ?", (t,))[0]
        return {"answered": r["n"] or 0, "right": r["right"] or 0}

    def study_days(self) -> set[str]:
        """Local dates (YYYY-MM-DD) on which at least one question was answered."""
        return {r[0] for r in self._q(
            "SELECT DISTINCT date(at, 'unixepoch', 'localtime') FROM reviews")}

    def reviews_due(self) -> int:
        """Questions seen before whose spaced-repetition review is due now."""
        return self._q("SELECT COUNT(*) FROM cards WHERE seen > 0 AND due_at <= ?", (time.time(),))[0][0]

    def study_stats(self) -> dict:
        r = self._q("""SELECT COUNT(*) AS total,
                              SUM(due_at <= ?) AS due,
                              SUM(box >= 3) AS mastered,
                              SUM(seen) AS seen, SUM(correct) AS correct FROM cards""",
                    (time.time(),))[0]
        return {k: (r[k] or 0) for k in r.keys()}
