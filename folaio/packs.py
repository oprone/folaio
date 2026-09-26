"""Subject Packs: share a subject (books, past papers, notes and quizzes) as one file.

A .folaio pack is a zip file:
    manifest.json   what's inside (name, author, documents)
    data.json       Folaio's reading of each document, summaries, quiz questions,
                    past-paper questions. Never anyone's personal progress.
    files/<sha256>.pdf

Packs come from other people, so importing treats the file as untrusted: only the
expected entries are read, nothing is extracted to a path chosen by the file,
sizes are limited, and every PDF must match its checksum.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path

FORMAT = 1
EXTENSIONS = (".folaio", ".herapack")   # .herapack: packs made before the rename
APPS = ("Folaio", "HeraAI")
MAX_PDF_BYTES = 500 * 1024 * 1024
MAX_PACK_BYTES = 4 * 1024 * 1024 * 1024
MAX_DATA_BYTES = 200 * 1024 * 1024
MAX_DOCS = 500


class PackError(ValueError):
    pass


# ======================= export =======================
def export_pack(memory, files_dir: Path, doc_ids: list[int], name: str,
                author: str = "", description: str = "") -> Path:
    docs = [d for d in (memory.document(i) for i in doc_ids) if d and d["status"] == "ready"]
    if not docs:
        raise PackError("Choose at least one ready document to share.")
    manifest = {
        "format": FORMAT, "app": "Folaio", "name": name.strip() or "Subject Pack",
        "author": author.strip(), "description": description.strip(), "created": time.time(),
        "documents": [],
    }
    data = {}
    fd, tmp = tempfile.mkstemp(suffix=".folaio")
    os.close(fd)
    out = Path(tmp)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for d in docs:
            key = d["sha256"]
            manifest["documents"].append({
                "key": key, "name": d["name"], "kind": d.get("kind") or "book",
                "pages": d["pages"], "year": d.get("year"),
            })
            data[key] = {
                "chunks": [{"page": c["page"], "text": c["text"]} for c in memory.chunks_for(d["id"])],
                "sections": [{k: s[k] for k in ("page_from", "page_to", "summary", "source")}
                             for s in memory.all_sections(d["id"])],
                "cards": [{k: c[k] for k in ("page", "question", "choices", "answer", "explanation", "source")}
                          for c in memory.all_cards(d["id"])],
                "questions": [{k: q[k] for k in ("number", "text", "context", "page", "marks")}
                              for q in memory.questions(d["id"])],
            }
            # PDFs are already compressed; store them as they are.
            z.write(files_dir / d["file"], f"files/{key}.pdf", compress_type=zipfile.ZIP_STORED)
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
        z.writestr("data.json", json.dumps(data))
    return out


# ======================= import =======================
def read_pack(path: Path) -> tuple[dict, dict, zipfile.ZipFile]:
    """Open and check a pack. Returns (manifest, data, open zip)."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise PackError("This isn't a Folaio Subject Pack (or the file is damaged).")
    try:
        infos = {i.filename: i for i in z.infolist()}
        if sum(i.file_size for i in infos.values()) > MAX_PACK_BYTES:
            raise PackError("This pack is too large.")
        for required in ("manifest.json", "data.json"):
            if required not in infos:
                raise PackError("This isn't a Folaio Subject Pack (manifest missing).")
        if infos["data.json"].file_size > MAX_DATA_BYTES or infos["manifest.json"].file_size > 1024 * 1024:
            raise PackError("This pack's contents are too large.")
        try:
            manifest = json.loads(z.read("manifest.json"))
            data = json.loads(z.read("data.json"))
        except ValueError:
            raise PackError("This pack is damaged (unreadable contents).")
        if not isinstance(manifest, dict) or manifest.get("app") not in APPS:
            raise PackError("This isn't a Folaio Subject Pack.")
        if manifest.get("format") != FORMAT:
            raise PackError("This pack was made by a newer version of Folaio. Please update Folaio.")
        docs = manifest.get("documents")
        if not isinstance(docs, list) or not 0 < len(docs) <= MAX_DOCS or not isinstance(data, dict):
            raise PackError("This pack has no documents.")
        for d in docs:
            if not (isinstance(d, dict) and _is_sha(d.get("key")) and isinstance(d.get("name"), str)
                    and d.get("kind") in ("book", "paper") and d["key"] in data):
                raise PackError("This pack is damaged (bad document entry).")
            info = infos.get(f"files/{d['key']}.pdf")
            if not info or info.file_size > MAX_PDF_BYTES:
                raise PackError(f"This pack is missing the PDF for {d['name']!r}.")
        return manifest, data, z
    except PackError:
        z.close()
        raise


def pdf_bytes(z: zipfile.ZipFile, key: str) -> bytes:
    """A document's PDF, only if it matches its checksum."""
    raw = z.read(f"files/{key}.pdf")
    if hashlib.sha256(raw).hexdigest() != key or not raw.startswith(b"%PDF"):
        raise PackError("A PDF in this pack doesn't match its checksum; the pack may have been changed.")
    return raw


def clean_doc_data(entry: dict) -> dict:
    """Keep only well-formed items (anything else in the file is ignored)."""
    def text(v, limit=20000):
        return v[:limit] if isinstance(v, str) else None

    def num(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1_000_000 else None

    chunks = [{"page": num(c.get("page")) or 1, "text": text(c.get("text"))}
              for c in entry.get("chunks", []) if isinstance(c, dict) and text(c.get("text"))]
    sections = [{"page_from": num(s.get("page_from")) or 1, "page_to": num(s.get("page_to")) or 1,
                 "summary": text(s.get("summary")), "source": s.get("source") if s.get("source") in ("core", "plus") else "core"}
                for s in entry.get("sections", []) if isinstance(s, dict) and text(s.get("summary"))]
    cards = []
    for c in entry.get("cards", []):
        if not isinstance(c, dict):
            continue
        choices = c.get("choices")
        if (text(c.get("question"), 2000) and isinstance(choices, list) and 2 <= len(choices) <= 6
                and all(isinstance(x, str) and 0 < len(x) < 300 for x in choices)
                and num(c.get("answer")) is not None and c["answer"] < len(choices)):
            cards.append({"page": num(c.get("page")) or 1, "question": text(c["question"], 2000),
                          "choices": choices, "answer": c["answer"],
                          "explanation": text(c.get("explanation"), 4000) or "",
                          "source": c.get("source") if c.get("source") in ("core", "plus") else "core"})
    questions = [{"number": text(q.get("number"), 20), "text": text(q.get("text"), 4000),
                  "context": text(q.get("context"), 4000), "page": num(q.get("page")) or 1,
                  "marks": num(q.get("marks"))}
                 for q in entry.get("questions", [])
                 if isinstance(q, dict) and text(q.get("number"), 20) and text(q.get("text"), 4000)]
    return {"chunks": chunks, "sections": sections, "cards": cards, "questions": questions}


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]", "_", Path(name).name)[:120] or "document.pdf"
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def _is_sha(v) -> bool:
    return isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) is not None
