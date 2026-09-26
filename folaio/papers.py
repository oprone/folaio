"""Past exam papers: split a paper into its questions, find the year and marks."""
from __future__ import annotations

import re

# "1.", "1)", "Q1.", "Question 1:", with the question text following
MAIN = re.compile(r"^\s*(?:Q(?:uestion)?\.?\s*)?(\d{1,2})\s*[.):]\s*(?=[A-Za-z(\[\"'])", re.I)
# "(a)", "a)", "(ii)" sub-parts
SUB = re.compile(r"^\s*\(?([a-h]|i{1,3}|iv|vi{0,3})\)\s*(?=[A-Za-z\"'])")
MARKS = re.compile(r"[\[(]\s*(\d{1,2})\s*marks?\s*[\])]", re.I)
YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
NOISE = re.compile(r"^\s*(turn over|page \d+|\d+\s*$|answer (all|any)\b|time allowed|instructions?\b|total\b)", re.I)


def find_year(name: str, first_page: str) -> int | None:
    m = YEAR.search(name) or YEAR.search(first_page[:600])
    return int(m.group(1)) if m else None


def split_questions(pages: list[tuple[int, str]]) -> list[dict]:
    """Questions in order: {number, text, context, page, marks}.

    A main question with sub-parts becomes one question per part; its opening
    text is kept as `context`, since parts usually depend on it.
    """
    out: list[dict] = []
    main_no, main_text, current = None, "", None

    def close():
        if current and len(current["text"].split()) >= 3:
            text = current["text"].strip()
            marks = MARKS.search(text)
            current["marks"] = int(marks.group(1)) if marks else None
            current["text"] = MARKS.sub("", text).strip()
            out.append(current)

    for page, text in pages:
        for line in text.split("\n"):
            line = line.strip()
            if not line or NOISE.match(line):
                continue
            if m := MAIN.match(line):
                close()
                main_no, main_text = m.group(1), line[m.end():]
                if s := SUB.match(main_text):     # "2. (a) Explain ..." on one line
                    current = {"number": f"{main_no}({s.group(1)})", "text": main_text[s.end():],
                               "context": None, "page": page}
                    main_text = ""
                else:
                    current = {"number": main_no, "text": main_text, "context": None, "page": page}
            elif (m := SUB.match(line)) and main_no:
                if current and current["number"] == main_no and current["text"] == main_text:
                    current = None           # the stem only introduces its parts
                else:
                    close()
                current = {"number": f"{main_no}({m.group(1)})", "text": line[m.end():],
                           "context": main_text.strip() or None, "page": page}
            elif current:
                current["text"] += " " + line
                if current["number"] == main_no:
                    main_text = current["text"]
    close()
    return out


# Exam instruction words: they say what to do, not what the question is about.
COMMAND_WORDS = frozenset("""
define explain describe discuss outline state identify evaluate analyse analyze compare contrast
calculate list give suggest assess justify examine illustrate distinguish briefly meant mark marks
one two three four five reasons reason ways way examples example answer write short note notes using
your own words with reference diagram help
""".split())


def topic_text(question: str) -> str:
    """The question without instruction words, marks or numbering: what it's about."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", MARKS.sub("", question))
    return " ".join(w for w in words if w.lower() not in COMMAND_WORDS)
