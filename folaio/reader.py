"""The Reader: pulls text out of PDFs and cuts it into page-tagged chunks."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium

CHUNK_CHARS = 900


@dataclass
class Chunk:
    page: int  # 1-based, for citations
    text: str


def read_pages(path: Path) -> list[tuple[int, str]]:
    """Return (page_number, text) for every page that has text.

    Scanned pages with no text layer are skipped for now (OCR is on the roadmap).
    """
    pages = []
    pdf = pdfium.PdfDocument(path)
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            textpage = page.get_textpage()
            text = _clean(textpage.get_text_range().replace("\r\n", "\n").replace("\r", "\n"))
            textpage.close()
            page.close()
            if len(text) > 20:
                pages.append((i + 1, text))
    finally:
        pdf.close()
    return pages


def page_count(path: Path) -> int:
    pdf = pdfium.PdfDocument(path)
    try:
        return len(pdf)
    finally:
        pdf.close()


def _clean(text: str) -> str:
    text = text.replace("\ufffe\n", "").replace("\ufffe", "-")  # pdfium's hyphen marks
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


_END = re.compile(r"[.!?:;,)\]\"'”]$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“(])")


def _blocks(text: str) -> list[tuple[str, str]]:
    """Rebuild ("heading", line) and ("para", text) blocks from PDF lines.

    PDFs store visual lines, not paragraphs. A short line without a full stop,
    after a finished sentence and before a capitalised line, is a heading.
    """
    lines = [ln.strip() for ln in text.split("\n")]
    blocks, para = [], ""
    for i, line in enumerate(lines):
        if not line:
            if para:
                blocks.append(("para", para))
                para = ""
            continue
        nxt = next((ln for ln in lines[i + 1:] if ln), "")
        # (no next line: a heading at the very bottom of the page)
        if (len(line) < 60 and not _END.search(line) and (not nxt or nxt[:1].isupper())
                and (not para or _END.search(para)) and re.search(r"[A-Za-z]", line)):
            if para:
                blocks.append(("para", para))
                para = ""
            blocks.append(("heading", line))
            continue
        if para.endswith("-") and line[:1].islower():
            para = para[:-1] + line               # word split across lines
        else:
            para = f"{para} {line}".strip()
    if para:
        blocks.append(("para", para))
    return blocks


def chunk_pages(pages: list[tuple[int, str]]) -> list[Chunk]:
    """Split each page into chunks of whole sentences (~CHUNK_CHARS).

    Chunks never cross page boundaries, so every chunk has an exact page to cite.
    A heading starts a new chunk and stays on its own first line.
    """
    chunks: list[Chunk] = []
    carried = ""   # a heading at the bottom of a page belongs to the next page's text
    for page_no, text in pages:
        buf, carried = carried, ""
        blocks = _blocks(text)
        while blocks and blocks[-1][0] == "heading":
            carried = f"{blocks.pop()[1]}\n{carried}"
        for kind, block in blocks:
            if kind == "heading":
                if len(buf) > 200:
                    chunks.append(Chunk(page_no, buf))
                    buf = ""
                buf = f"{buf}\n{block}\n" if buf else f"{block}\n"
                continue
            for sent in _SENTENCE.split(block):
                if buf and len(buf) + len(sent) > CHUNK_CHARS and not buf.endswith("\n"):
                    chunks.append(Chunk(page_no, buf.strip()))
                    buf = ""
                buf = f"{buf}{sent}" if buf.endswith("\n") or not buf else f"{buf} {sent}"
        if buf.strip():
            chunks.append(Chunk(page_no, buf.strip()))
    return chunks


def is_heading(line: str) -> bool:
    line = line.strip()
    return 2 < len(line) < 80 and not _END.search(line) and bool(re.search(r"[A-Za-z]", line))


def leading_heading(text: str) -> str | None:
    """The heading a passage starts with (the most specific one if several are stacked)."""
    found = None
    for line in text.split("\n"):
        if not line.strip():
            continue
        if "\n" in text and is_heading(line):
            found = line.strip()
        else:
            break
    return found
