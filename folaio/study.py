"""Prompts for the Writer: cited answers, section summaries and quiz questions."""
from __future__ import annotations

import json
import random

from .memory import Hit

NOT_FOUND = "I couldn't find this in your documents."

EXPLAIN_SYSTEM = """You are Folaio, a friendly tutor for students.
Using ONLY the passages given, explain the answer to the student's question
in 2 or 3 short, simple sentences. Do not add facts that are not in the passages."""


def explain_messages(question: str, hits: list[Hit]) -> list[dict]:
    passages = "\n\n".join(h.text for h in hits)
    return [
        {"role": "system", "content": EXPLAIN_SYSTEM},
        {"role": "user", "content": f"Passages:\n\n{passages}\n\nQuestion: {question}"},
    ]


STUDY_SYSTEM = """You are Folaio, a study assistant that turns textbook passages into study material.
Use ONLY the passage given. Write in simple, clear language for a student."""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5}},
    "required": ["summary"],
}

STUDY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5},
        "questions": {
            "type": "array", "minItems": 1, "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "choices": {"type": "array", "items": {"type": "string"},
                                "minItems": 4, "maxItems": 4},
                    "answer": {"type": "integer", "enum": [0, 1, 2, 3]},
                    "explanation": {"type": "string"},
                },
                "required": ["question", "choices", "answer", "explanation"],
            },
        },
    },
    "required": ["summary", "questions"],
}


def study_messages(passage: str, quizzes: bool = True) -> list[dict]:
    return [
        {"role": "system", "content": STUDY_SYSTEM},
        {"role": "user", "content": (
            f"Passage:\n\n{passage}\n\n"
            "Return JSON with:\n"
            '- "summary": 2-5 short bullet points of the key ideas.' + (
            '\n- "questions": 1-3 multiple-choice questions testing understanding. '
            'Each has "question", exactly 4 "choices", the index of the correct choice '
            'as "answer" (0-3), and a one-sentence "explanation".' if quizzes else ""))},
    ]


def parse_study(raw: str) -> tuple[list[str], list[dict]]:
    data = json.loads(raw)
    summary = [s.strip() for s in data.get("summary", []) if s.strip()]
    questions = []
    for q in data.get("questions", []):
        choices = [c.strip() for c in q.get("choices", [])]
        ans = q.get("answer")
        if len(choices) != 4 or not isinstance(ans, int) or not 0 <= ans < 4:
            continue
        if len(set(c.lower() for c in choices)) < 4 or not q.get("question", "").strip():
            continue
        # Small models love putting the answer first: shuffle the choices.
        correct = choices[ans]
        random.shuffle(choices)
        questions.append({"question": q["question"].strip(), "choices": choices,
                          "answer": choices.index(correct),
                          "explanation": q.get("explanation", "").strip()})
    return summary, questions
