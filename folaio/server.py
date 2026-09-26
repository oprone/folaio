"""Local HTTP API + web UI. Listens on 127.0.0.1 only: nothing leaves the computer."""
from __future__ import annotations

import json
import re
import tempfile
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, packs, picker
from .core import Folaio

app = FastAPI(title="Folaio")
folaio = Folaio()
WEB = config.ROOT / "web"


@app.middleware("http")
async def only_from_folaio(request: Request, call_next):
    """Other websites open in the browser can send requests to 127.0.0.1 too.
    Changes must carry Folaio's own header, which browsers never let other sites add."""
    if request.method not in ("GET", "HEAD") and request.headers.get("x-folaio") != "1":
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)


class Question(BaseModel):
    question: str
    doc_id: int | None = None


class Grade(BaseModel):
    correct: bool


@app.get("/api/status")
def status():
    return folaio.status()


@app.get("/api/documents")
def documents():
    return folaio.memory.documents()


@app.post("/api/documents")
async def upload(files: list[UploadFile], kind: str = "book"):
    """Add PDFs: textbooks and notes (kind=book) or past exam papers (kind=paper)."""
    if kind not in ("book", "paper"):
        raise HTTPException(400, "kind must be book or paper")
    results = []
    for f in files:
        if not (f.filename or "").lower().endswith(".pdf"):
            results.append({"name": f.filename, "error": "Only PDF files are supported for now."})
            continue
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            while block := await f.read(1 << 20):
                tmp.write(block)
        results.append(folaio.add_file(Path(tmp.name), name=f.filename, move=True, kind=kind))
    return results


@app.delete("/api/documents/{doc_id}")
def delete(doc_id: int):
    folaio.delete(doc_id)
    return {"ok": True}


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: int):
    path = folaio.file_path(doc_id)
    if not path or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf")


@app.get("/api/documents/{doc_id}/summary")
def summary(doc_id: int):
    return folaio.memory.sections(doc_id)


@app.get("/api/search")
def search(q: str, doc_id: int | None = None):
    return folaio.search(q, doc_id=doc_id) if q.strip() else []


@app.post("/api/ask")
def ask(body: Question):
    def events():
        try:
            for event in folaio.ask(body.question, body.doc_id):
                yield json.dumps(event) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "text": str(e)}) + "\n"
    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/api/quiz/next")
def next_card(doc_id: int | None = None, section: int | None = None):
    if doc_id is not None and section is not None:
        return folaio.practice_card(doc_id, section)   # "practise this section"
    return folaio.memory.next_card(doc_id)


class PackRequest(BaseModel):
    doc_ids: list[int]
    name: str
    author: str = ""
    description: str = ""


@app.post("/api/packs/export")
def export_pack(body: PackRequest):
    """Make a .folaio pack of the chosen documents and send it to the browser to save."""
    try:
        path = folaio.export_pack(body.doc_ids, body.name[:100], body.author[:100], body.description[:1000])
    except packs.PackError as e:
        raise HTTPException(400, str(e))
    filename = re.sub(r"[^\w\- ]", "", body.name).strip()[:60] or "Subject Pack"
    return FileResponse(path, media_type="application/octet-stream", filename=f"{filename}.folaio",
                        background=BackgroundTask(path.unlink, missing_ok=True))


@app.post("/api/packs")
async def import_pack(file: UploadFile):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".folaio") as tmp:
        while block := await file.read(1 << 20):
            tmp.write(block)
    try:
        return folaio.import_pack(Path(tmp.name))
    except packs.PackError as e:
        raise HTTPException(400, str(e))
    finally:
        Path(tmp.name).unlink(missing_ok=True)


class AnswerCheck(BaseModel):
    question: str
    answer: str
    doc_id: int | None = None


@app.post("/api/check")
def check_answer(body: AnswerCheck):
    if len(body.answer.split()) < 3:
        raise HTTPException(400, "Please write a little more (at least a sentence).")
    return folaio.check_answer(body.question, body.answer, body.doc_id)


class Exam(BaseModel):
    date: str | None = None   # YYYY-MM-DD, or None to clear


@app.post("/api/documents/{doc_id}/exam")
def set_exam(doc_id: int, body: Exam):
    if body.date:
        try:
            date.fromisoformat(body.date)
        except ValueError:
            raise HTTPException(400, "Please give the date as YYYY-MM-DD.")
    folaio.memory.update_document(doc_id, exam_date=body.date or None)
    return {"ok": True}


@app.get("/api/plan")
def study_plan(minutes: int = 20):
    return folaio.study_plan(max(5, min(minutes, 180)))


@app.get("/api/papers")
def past_papers():
    return folaio.past_papers()


@app.get("/api/map")
def knowledge_map():
    return folaio.knowledge_map()


@app.post("/api/quiz/{card_id}")
def grade(card_id: int, body: Grade):
    folaio.memory.grade_card(card_id, body.correct)
    return {"ok": True}


class Settings(BaseModel):
    welcomed: bool | None = None


@app.post("/api/settings")
def update_settings(body: Settings):
    return config.save_settings(**body.model_dump(exclude_none=True))


def _check_brain(key: str):
    if key not in {w.key for w in config.WRITERS}:
        raise HTTPException(404)


@app.post("/api/brain/{key}")
def download_brain(key: str):
    _check_brain(key)
    folaio.writer.start_download(key)
    return {"ok": True}


@app.post("/api/brain/{key}/use")
def use_brain(key: str):
    """Pick which downloaded brain Folaio Plus uses, or "off" for Folaio Core only."""
    if key != "off":
        _check_brain(key)
    config.save_settings(brain=key)
    folaio.writer.switched()
    return {"ok": True}


@app.delete("/api/brain/{key}")
def remove_brain(key: str):
    _check_brain(key)
    folaio.writer.remove(key)
    if config.load_settings().get("brain") == key:
        config.save_settings(brain=None)
    return {"ok": True}


class Folder(BaseModel):
    path: str | None = None   # None = back to the default folder


@app.post("/api/brain-folder/pick")
def pick_brain_folder():
    """Open the computer's own folder window so the user can choose where brains go."""
    try:
        path = picker.pick_folder()
    except NotImplementedError:
        return {"unsupported": True}
    return {"path": path} if path else {"cancelled": True}


@app.post("/api/brain-folder")
def set_brain_folder(body: Folder):
    try:
        folaio.writer.move_to(Path(body.path) if body.path else None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"folder": str(config.models_dir())}


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
