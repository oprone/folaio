"""The Coordinator: connects Reader, Memory, Folaio Core (the Mind) and the
optional Plus brain, and runs background work one job at a time.
"""
from __future__ import annotations

import hashlib
import math
import queue
import re
import shutil
import threading
import time
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

from . import config, packs, papers, reader, study
from .brain import Writer
from .memory import Memory
from .mind import MIN_KNOWN, Mind

SECTION_MIN_CHARS = 3000
SECTION_MAX_CHARS = 8000
MAX_SECTIONS = 60


class Folaio:
    def __init__(self):
        self.memory = Memory()
        self.mind = Mind(config.MIND_DIR)
        self.writer = Writer()
        self._reads: queue.Queue[int] = queue.Queue()
        self._relearn = threading.Event()
        self._rematch = threading.Event()   # link past-paper questions to chapters again
        self.activity = "idle"
        # Resume anything interrupted last time the app closed.
        for d in self.memory.documents():
            if d["status"] in ("queued", "reading"):
                self._reads.put(d["id"])
            elif d["status"] == "learning":
                self._relearn.set()
        if not self.mind.ready and self.memory.library_chunks():
            self._relearn.set()
        self._rematch.set()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._watch_inbox, daemon=True).start()

    # ---------- adding documents ----------
    def add_file(self, src: Path, name: str | None = None, move: bool = False, kind: str = "book") -> dict:
        name = name or src.name
        sha = _sha256(src)
        dest = config.FILES_DIR / f"{sha[:12]}_{_safe(name)}"
        doc_id = self.memory.add_document(name, dest.name, sha, kind)
        if doc_id is None:
            if move:
                src.unlink(missing_ok=True)
            return {"duplicate": True, "name": name}
        (shutil.move if move else shutil.copy2)(str(src), dest)
        self._reads.put(doc_id)
        return {"id": doc_id, "name": name}

    def delete(self, doc_id: int):
        doc = self.memory.document(doc_id)
        if doc:
            self.memory.delete_document(doc_id)
            (config.FILES_DIR / doc["file"]).unlink(missing_ok=True)
            if doc.get("kind", "book") == "book":
                self._relearn.set()   # forget what this document taught

    def file_path(self, doc_id: int) -> Path | None:
        doc = self.memory.document(doc_id)
        return config.FILES_DIR / doc["file"] if doc else None

    # ---------- background work ----------
    def _worker(self):
        while True:
            if self._drain_reads() or self._relearn.is_set():
                self._learn()
                continue
            if self._rematch.is_set():
                self._match_papers()
                continue
            doc = next((d for d in self.memory.documents()
                        if d["status"] == "ready" and not d["notes_done"]), None)
            if doc:
                self._core_notes(doc)
                continue
            doc = self._next_for_plus()
            if doc:
                self._plus_notes(doc)
                continue
            self.activity = "idle"
            try:
                self._reads.put(self._reads.get(timeout=3))
            except queue.Empty:
                pass

    def _drain_reads(self) -> bool:
        """Read every waiting PDF. Returns True if anything new was read."""
        read = False
        while True:
            try:
                read |= self._read(self._reads.get_nowait())
            except queue.Empty:
                return read

    def _read(self, doc_id: int) -> bool:
        doc = self.memory.document(doc_id)
        if not doc:
            return False
        m = self.memory
        if doc.get("kind") == "paper":
            return self._read_paper(doc)
        try:
            m.update_document(doc_id, status="reading", error=None)
            self.activity = f"Reading {doc['name']}"
            path = config.FILES_DIR / doc["file"]
            m.clear_chunks(doc_id)  # restart cleanly
            chunks = reader.chunk_pages(reader.read_pages(path))
            m.update_document(doc_id, pages=reader.page_count(path))
            if not chunks:
                raise ValueError("No text found. This may be a scanned PDF (OCR support is coming).")
            m.add_chunks(doc_id, [c.page for c in chunks], [c.text for c in chunks])
            m.update_document(doc_id, status="learning", notes_done=0, study_status="queued")
            return True
        except Exception as e:
            m.update_document(doc_id, status="error", error=str(e))
            return False

    def _read_paper(self, doc: dict) -> bool:
        """A past exam paper: split it into questions (it isn't learned like a textbook)."""
        m = self.memory
        try:
            m.update_document(doc["id"], status="reading", error=None)
            self.activity = f"Reading past paper {doc['name']}"
            path = config.FILES_DIR / doc["file"]
            pages = reader.read_pages(path)
            questions = papers.split_questions(pages)
            if not questions:
                raise ValueError("No numbered questions found. Is this an exam paper?")
            m.set_questions(doc["id"], questions)
            m.update_document(doc["id"], status="ready", pages=reader.page_count(path), notes_done=1,
                              study_status="none",
                              year=papers.find_year(doc["name"], pages[0][1] if pages else ""))
            self._rematch.set()
        except Exception as e:
            m.update_document(doc["id"], status="error", error=str(e))
        return False   # nothing new to learn

    def _match_papers(self):
        """Link every past-paper question to the textbook chapter that answers it."""
        self._rematch.clear()
        questions = self.memory.questions()
        if not questions:
            return
        self.activity = "Linking past-paper questions to your books"
        where = {}   # passage id -> (book, section)
        for d in self.memory.documents():
            if d["status"] == "ready" and d.get("kind", "book") == "book":
                for i, sec in enumerate(_sections(self.memory.chunks_for(d["id"]), with_ids=True)):
                    for cid in sec[3]:
                        where[cid] = (d["id"], i)
        for q in questions:
            topic = papers.topic_text(q["text"])
            full = papers.topic_text(f"{q['context'] or ''} {q['text']}")
            if not self.mind.ready or self.mind.known_ratio(topic or full) < MIN_KNOWN:
                self.memory.match_question(q["id"], None, None, None)
                continue
            # The part itself decides; the shared opening text (often a made-up scenario,
            # "Country A…") only helps a little.
            votes: dict = {}
            for text, weight in ((topic or full, 1.0), (full, 0.3)):
                for f in self.mind.search(text, k=3):
                    if f.chunk_id in where:
                        votes[where[f.chunk_id]] = votes.get(where[f.chunk_id], 0) + weight * f.score
            if votes:
                (book, section), score = max(votes.items(), key=lambda kv: kv[1])
                self.memory.match_question(q["id"], book, section, round(score, 3))
            else:
                self.memory.match_question(q["id"], None, None, None)

    def past_papers(self) -> dict:
        """Which chapters the exams ask about most, and every paper's questions."""
        docs = self.memory.documents()
        paper_docs = [d for d in docs if d.get("kind") == "paper"]
        books = {b["doc_id"]: b for b in self.knowledge_map()}
        section_of = lambda q: (books[q["book_id"]]["sections"][q["section"]]
                                if q["book_id"] in books and q["section"] is not None
                                and q["section"] < len(books[q["book_id"]]["sections"]) else None)
        topics: dict = {}
        by_paper: dict = {}
        for q in self.memory.questions():
            s = section_of(q)
            item = dict(q, chapter=None)
            if s:
                b = books[q["book_id"]]
                item["chapter"] = {"doc_id": b["doc_id"], "book": b["name"], **{k: s[k] for k in (
                    "index", "title", "page_from", "page_to", "status", "questions")}}
                t = topics.setdefault((b["doc_id"], s["index"]), {
                    **item["chapter"], "count": 0, "marks": 0, "papers": set(), "years": set()})
                t["count"] += 1
                t["marks"] += q["marks"] or 0
                t["papers"].add(q["doc_id"])
                if q["year"]:
                    t["years"].add(q["year"])
            by_paper.setdefault(q["doc_id"], []).append(item)
        ranked = sorted(topics.values(), key=lambda t: (-len(t["papers"]), -t["count"], -t["marks"]))
        for t in ranked:
            t["papers"], t["years"] = len(t["papers"]), sorted(t["years"])
        return {
            "papers": [{"doc_id": d["id"], "name": d["name"], "year": d.get("year"), "status": d["status"],
                        "error": d["error"], "questions": by_paper.get(d["id"], [])}
                       for d in sorted(paper_docs, key=lambda d: (d.get("year") or 0, d["id"]))],
            "topics": ranked,
            "books": len(books),
        }

    def _learn(self):
        """Folaio Core learns the whole library again (seconds, even for many books)."""
        self._relearn.clear()
        self.activity = "Learning your library"
        progress = lambda e, n: setattr(self, "activity", f"Learning your library ({round(100 * e / n)}%)")
        self.mind.learn(self.memory.library_chunks(), progress=progress)
        for d in self.memory.documents():
            if d["status"] == "learning":
                self.memory.update_document(d["id"], status="ready")
        self._rematch.set()   # chapters may have changed

    def _core_notes(self, doc: dict):
        """Summaries and fill-in-the-blank quizzes from Folaio Core: instant, no download."""
        doc_id = doc["id"]
        self.activity = f"Making study notes for {doc['name']}"
        sections = _sections(self.memory.chunks_for(doc_id))
        self.memory.clear_study(doc_id, "core")
        for page_from, page_to, text in sections:
            points = self.mind.summarize(text)
            if points:
                self.memory.add_section(doc_id, page_from, page_to,
                                        "\n".join(f"• {p}" for p in points), "core")
        for card in self.mind.quiz([(p, t) for p, _, t in sections]):
            self.memory.add_card(doc_id, card["page"], card["question"], card["choices"],
                                 card["answer"], card["explanation"], "core")
        self.memory.update_document(doc_id, notes_done=1)

    def _next_for_plus(self) -> dict | None:
        if not self.writer.available:
            return None
        for d in self.memory.documents():
            if d["status"] == "ready" and d["study_status"] in ("queued", "working"):
                return d
        return None

    def _plus_notes(self, doc: dict):
        """Written summaries and multiple-choice questions from the optional Plus brain."""
        doc_id = doc["id"]
        sections = _sections(self.memory.chunks_for(doc_id))
        start = doc["study_done"] if doc["study_status"] == "working" else 0
        if start == 0:
            self.memory.clear_study(doc_id, "plus")
        self.memory.update_document(doc_id, study_status="working", study_total=len(sections))
        for idx in range(start, len(sections)):
            if self._drain_reads():
                self._relearn.set()
                return   # new PDFs first; come back to this later
            if not self.memory.document(doc_id):
                return  # deleted meanwhile
            page_from, page_to, text = sections[idx]
            self.activity = f"Folaio Plus is writing notes for {doc['name']} ({idx + 1}/{len(sections)})"
            try:
                quizzes = self.writer.spec.quizzes
                raw = self.writer.chat(study.study_messages(text, quizzes), max_tokens=900,
                                       json_schema=study.STUDY_SCHEMA if quizzes else study.SUMMARY_SCHEMA,
                                       background=True)
                summary, questions = study.parse_study(raw)
            except Exception:
                summary, questions = [], []   # skip a bad section, keep going
            if summary:
                self.memory.add_section(doc_id, page_from, page_to,
                                        "\n".join(f"• {s}" for s in summary), "plus")
            for q in questions:
                self.memory.add_card(doc_id, page_from, q["question"], q["choices"],
                                     q["answer"], q["explanation"], "plus")
            self.memory.update_document(doc_id, study_done=idx + 1)
        self.memory.update_document(doc_id, study_status="done")

    def _watch_inbox(self):
        while True:
            for f in sorted(config.INBOX_DIR.iterdir()):
                if f.suffix.lower() not in (".pdf", *packs.EXTENSIONS):
                    continue
                try:
                    # Skip files that are still being copied in.
                    size = f.stat().st_size
                    time.sleep(1)
                    if f.stat().st_size != size:
                        continue
                    if f.suffix.lower() == ".pdf":
                        self.add_file(f, move=True)
                    else:
                        try:
                            self.import_pack(f)
                            f.unlink(missing_ok=True)
                        except packs.PackError:
                            f.rename(f.with_suffix(".folaio-failed"))   # don't retry forever
                except OSError:
                    pass
            time.sleep(5)

    # ---------- subject packs ----------
    def export_pack(self, doc_ids: list[int], name: str, author: str = "", description: str = "") -> Path:
        return packs.export_pack(self.memory, config.FILES_DIR, doc_ids, name, author, description)

    def import_pack(self, path: Path) -> dict:
        """Add a Subject Pack: its documents arrive already read, with notes and quizzes."""
        manifest, data, z = packs.read_pack(path)
        added, skipped, books = [], [], False
        m = self.memory
        with z:
            # Check every PDF first, so a damaged pack adds nothing at all.
            for d in manifest["documents"]:
                if not m.document_by_sha(d["key"]):
                    packs.pdf_bytes(z, d["key"])
            for d in manifest["documents"]:
                key, name, kind = d["key"], d["name"][:200], d["kind"]
                if m.document_by_sha(key):
                    skipped.append(name)          # already in the library
                    continue
                raw = packs.pdf_bytes(z, key)
                entry = packs.clean_doc_data(data[key])
                if kind == "book" and not entry["chunks"]:
                    skipped.append(name)
                    continue
                dest = config.FILES_DIR / f"{key[:12]}_{packs.safe_name(name)}"
                dest.write_bytes(raw)
                doc_id = m.add_document(name, dest.name, key, kind)
                pages = d["pages"] if isinstance(d.get("pages"), int) else 0
                if kind == "book":
                    m.add_chunks(doc_id, [c["page"] for c in entry["chunks"]], [c["text"] for c in entry["chunks"]])
                    for s in entry["sections"]:
                        m.add_section(doc_id, s["page_from"], s["page_to"], s["summary"], s["source"])
                    for c in entry["cards"]:
                        m.add_card(doc_id, c["page"], c["question"], c["choices"], c["answer"],
                                   c["explanation"], c["source"])
                    has_plus = any(x["source"] == "plus" for x in entry["sections"] + entry["cards"])
                    m.update_document(doc_id, status="learning", pages=pages,
                                      notes_done=int(bool(entry["cards"] or entry["sections"])),
                                      study_status="done" if has_plus else "queued")
                    books = True
                else:
                    m.set_questions(doc_id, entry["questions"])
                    year = d.get("year") if isinstance(d.get("year"), int) else None
                    m.update_document(doc_id, status="ready", pages=pages, notes_done=1,
                                      study_status="none", year=year)
                added.append(name)
        if books:
            self._relearn.set()
        self._rematch.set()
        return {"name": manifest["name"], "author": manifest.get("author", ""),
                "description": manifest.get("description", ""), "added": added, "skipped": skipped}

    # ---------- using the knowledge ----------
    def search(self, q: str, k: int = 10, doc_id: int | None = None) -> list[dict]:
        return [h.__dict__ for h in self.memory.hits(self.mind.search(q, k=k, doc_id=doc_id))]

    def ask(self, question: str, doc_id: int | None = None) -> Iterator[dict]:
        def reply(text, sources=()):
            yield {"type": "sources", "sources": list(sources)}
            yield {"type": "token", "text": text}
            yield {"type": "done"}

        if not self.mind.ready:
            yield from reply("Folaio hasn't learned anything yet. Add some PDFs in the Library first.")
            return
        # Cony's idea: if most of the question's words never appear in the library, don't guess.
        if self.mind.known_ratio(question) < MIN_KNOWN:
            yield from reply(study.NOT_FOUND)
            return
        hits = self.memory.hits(self.mind.search(question, k=4, doc_id=doc_id))
        if not hits:
            yield from reply(study.NOT_FOUND)
            return
        sources = [h.__dict__ for h in hits]
        exact = self.mind.answer(question, [h.text for h in hits])
        if not self.writer.available:
            yield from reply(exact, sources)
            return
        # The document's own sentences first (always right), then a tiny brain explains them.
        yield {"type": "sources", "sources": sources}
        yield {"type": "token", "text": exact + "\n\n**In simple words:** "}
        chosen = [s for s, _ in self.mind.best_sentences(question, [h.text for h in hits], 3)]
        for piece in self.writer.stream(study.explain_messages(question, chosen), max_tokens=160):
            yield {"type": "token", "text": piece.replace("\n", " ")}
        yield {"type": "done"}

    def _section_cards(self, doc_id: int) -> tuple[list, list[list[dict]]]:
        """A document's study sections, and which quiz questions belong to each.

        Questions are matched by the sentence they were made from (several sections
        can share a page); by page only when that's all there is to go on.
        """
        sections = _sections(self.memory.chunks_for(doc_id))
        flat = [re.sub(r"\s+", " ", t) for _, _, t in sections]
        groups: list[list[dict]] = [[] for _ in sections]
        for card in self.memory.cards_for(doc_id):
            quoted = re.search(r"\u201c(.+)\u201d", card["explanation"] or "")
            probe = re.sub(r"\s+", " ", quoted.group(1))[:80] if quoted else None
            idx = next((i for i, t in enumerate(flat) if probe and probe in t), None)
            if idx is None:
                inside = [i for i, (pf, pt, _) in enumerate(sections) if pf <= card["page"] <= pt]
                idx = inside[0] if inside else None
            if idx is not None:
                groups[idx].append(card)
        return sections, groups

    def section_of_chunk(self, chunk_id: int) -> dict | None:
        """Which study section (chapter) a passage belongs to."""
        for d in self.memory.documents():
            if d.get("kind", "book") != "book" or d["status"] != "ready":
                continue
            for i, sec in enumerate(_sections(self.memory.chunks_for(d["id"]), with_ids=True)):
                if chunk_id in sec[3]:
                    return {"doc_id": d["id"], "index": i}
        return None

    def practice_card(self, doc_id: int, section: int) -> dict | None:
        sections, groups = self._section_cards(doc_id)
        if not 0 <= section < len(groups):
            return None
        return self.memory.next_card(doc_id, [c["id"] for c in groups[section]])

    def check_answer(self, question: str, answer: str, doc_id: int | None = None) -> dict:
        if not self.mind.ready:
            return {"error": "Folaio hasn't learned anything yet. Add some PDFs in the Library first."}
        if self.mind.known_ratio(question) < MIN_KNOWN:
            return {"error": study.NOT_FOUND}
        hits = self.memory.hits(self.mind.search(question, k=4, doc_id=doc_id))
        if not hits:
            return {"error": study.NOT_FOUND}
        result = self.mind.check_answer(question, answer, [h.text for h in hits])
        result["sources"] = [h.__dict__ for h in hits]
        return result

    def knowledge_map(self) -> list[dict]:
        """Every section of every document with how well the student knows it."""
        now = time.time()
        out = []
        hits: dict = {}
        for q in self.memory.questions():
            if q["book_id"] is not None:
                hits[(q["book_id"], q["section"])] = hits.get((q["book_id"], q["section"]), 0) + 1
        for d in self.memory.documents():
            if d["status"] != "ready" or d.get("kind", "book") != "book":
                continue
            sections, groups = self._section_cards(d["id"])
            rows = []
            for i, ((pf, pt, text), cards) in enumerate(zip(sections, groups)):
                rows.append({
                    "index": i, "title": _title(text) or f"Pages {pf}\u2013{pt}",
                    "page_from": pf, "page_to": pt,
                    "questions": len(cards), "seen": sum(1 for c in cards if c["seen"]),
                    "right": sum(c["correct"] for c in cards), "answered": sum(c["seen"] for c in cards),
                    "due": sum(1 for c in cards if c["due_at"] <= now),
                    "exam_hits": hits.get((d["id"], i), 0),   # past-paper questions on it
                    **_mastery(cards),
                })
            quizzable = [r for r in rows if r["questions"]]
            out.append({
                "doc_id": d["id"], "name": d["name"], "sections": rows,
                "mastery": round(sum(r["strength"] for r in quizzable) / len(quizzable), 2) if quizzable else 0,
            })
        return out

    def study_plan(self, minutes: int = 20) -> dict:
        """Today's plan within the time budget: review what's due, fix weak chapters,
        then learn new ones, paced so every book is covered before its exam."""
        today = date.today()
        midnight = time.mktime(today.timetuple())
        docs = {d["id"]: d for d in self.memory.documents() if d["status"] == "ready"}
        books = self.knowledge_map()
        budget, tasks = float(minutes), []

        # 1. Spaced-repetition reviews (about 30 seconds a question).
        due = self.memory.reviews_due()
        if due:
            n = min(due, max(4, int(budget * 2)))
            tasks.append({"kind": "review", "count": n, "total": due, "minutes": math.ceil(n / 2)})
            budget -= n / 2

        # 2. Exams: how far along each book is, and how fast new chapters must be learned.
        exams, pace = [], {}
        for b in books:
            exam = docs[b["doc_id"]].get("exam_date")
            quizzable = [s for s in b["sections"] if s["questions"]]
            left = [s for s in quizzable if s["status"] != "mastered"]
            info = {"doc_id": b["doc_id"], "name": b["name"], "exam_date": exam,
                    "chapters": len(quizzable), "mastered": len(quizzable) - len(left)}
            if exam:
                days = (date.fromisoformat(exam) - today).days
                info["days_left"] = days
                if days >= 0:
                    new = [s for s in left if s["status"] == "new"]
                    pace[b["doc_id"]] = math.ceil(len(new) / max(days, 1)) if new else 0
                    info["new_per_day"] = pace[b["doc_id"]]
            exams.append(info)

        # 3. Chapters with wrong answers, weakest first (5 minutes each). Chapters that are
        #    only "shaky" because they're new come back through the spaced reviews instead.
        weak = sorted((dict(s, doc_id=b["doc_id"], doc=b["name"]) for b in books for s in b["sections"]
                       if s["status"] in ("weak", "shaky") and s["right"] < 0.8 * s["answered"]),
                      key=lambda s: s["strength"] - 0.15 * min(s["exam_hits"], 4))  # often examined first
        for s in weak[:2]:
            if budget < 4 and tasks:
                break
            tasks.append({"kind": "practise", "section": s, "minutes": 5})
            budget -= 5

        # 4. New chapters in book order; books with the nearest exam first.
        def urgency(b):
            e = docs[b["doc_id"]].get("exam_date")
            return (0, e) if e and date.fromisoformat(e) >= today else (1, "")
        for b in sorted(books, key=urgency):
            quota = pace.get(b["doc_id"], 1)
            new = sorted((s for s in b["sections"] if s["status"] == "new"), key=lambda s: -s["exam_hits"])
            for s in new[:max(quota, 1)]:
                if budget < 5 and tasks:
                    break
                tasks.append({"kind": "learn", "section": dict(s, doc_id=b["doc_id"], doc=b["name"]),
                              "minutes": 8})
                budget -= 8

        # Streak: days in a row with at least one answer (today still counts if not started yet).
        days, streak, day = self.memory.study_days(), 0, today
        if day.isoformat() not in days:
            day -= timedelta(days=1)
        while day.isoformat() in days:
            streak += 1
            day -= timedelta(days=1)

        return {"minutes": minutes, "tasks": tasks, "exams": exams, "streak": streak,
                "today": self.memory.answered_since(midnight),
                "planned_minutes": sum(t["minutes"] for t in tasks)}

    def status(self) -> dict:
        spec = self.writer.spec
        rec = config.recommended_writer()
        ram = config.hardware()["ram_gb"]
        return {
            "hardware": config.hardware() | {"disk_free_gb": config.disk_free_gb()},
            "activity": self.activity,
            "brain": {
                "installed": asdict(spec) if spec else None,
                "loaded": self.writer.loaded,
                "recommended": rec.key,
                "off": config.load_settings().get("brain") == "off",
                "folder": str(config.models_dir()),
                "folder_is_default": config.models_dir() == config.DEFAULT_MODELS_DIR,
                "folder_ok": config.models_dir().is_dir(),
                "options": [asdict(w) | {"installed": w.path.exists(),
                                         "active": spec is not None and w.key == spec.key,
                                         "fits": ram >= w.min_ram_gb} for w in config.WRITERS],
                "download": self.writer.download,
            },
            "mind": {
                "ready": self.mind.ready,
                "accuracy": self.mind.accuracy,
                "learned_at": self.mind.learned_at,
                "words": len(self.mind.vocab) if self.mind.ready else 0,
            },
            "study": self.memory.study_stats(),
            "settings": config.load_settings(),
            "data_dir": str(config.DATA),
            "inbox": str(config.INBOX_DIR),
        }


def _title(text: str) -> str | None:
    """A section's heading: the one it starts with, else the first one inside it."""
    if head := reader.leading_heading(text):
        return head
    return next((ln.strip() for ln in text.split("\n") if "\n" in text and reader.is_heading(ln)), None)


def _mastery(cards: list[dict]) -> dict:
    """How well a section is known, from its quiz questions' spaced-repetition history."""
    seen = [c for c in cards if c["seen"]]
    if not cards:
        return {"status": "none", "strength": 0.0}
    if not seen:
        return {"status": "new", "strength": 0.0}
    # Long-term memory: a question counts as learned at box 3 (right ~3 times, spaced out).
    memory = sum(min(c["box"], 3) / 3 for c in cards) / len(cards)
    # Plus how often answers have been right so far.
    accuracy = sum(c["correct"] for c in seen) / max(1, sum(c["seen"] for c in seen))
    strength = 0.6 * memory + 0.4 * accuracy
    status = "mastered" if strength >= 0.67 else "shaky" if strength >= 0.34 else "weak"
    return {"status": status, "strength": round(strength, 2)}


def _sections(chunks: list[dict], with_ids: bool = False) -> list[tuple]:
    """Group passages into study sections that follow the book's own headings.

    A new section starts at a heading once the current one has some substance,
    or when it gets too long; a book makes roughly MAX_SECTIONS sections at most.
    """
    total = sum(len(c["text"]) for c in chunks)
    size = min(max(total // MAX_SECTIONS, SECTION_MIN_CHARS), SECTION_MAX_CHARS)
    out, buf, ids, first, length = [], [], [], None, 0

    def close():
        section = (first, buf_last_page, "\n\n".join(buf))
        out.append(section + (ids,) if with_ids else section)

    for c in chunks:
        starts_heading = reader.leading_heading(c["text"]) is not None
        if buf and (length >= size or (starts_heading and length >= size // 3)):
            close()
            buf, ids, first, length = [], [], None, 0
        first = first or c["page"]
        buf.append(c["text"])
        ids.append(c.get("id"))
        buf_last_page = c["page"]
        length += len(c["text"])
    if buf:
        close()
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", name)[:120]
