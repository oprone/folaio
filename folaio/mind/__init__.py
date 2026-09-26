"""Folaio Core: Folaio's own AI. No pretrained models.

It learns the user's library (FolaioNet), then uses what it learned to find
passages, answer with the documents' own sentences, summarize, and write
fill-in-the-blank quizzes whose answer keys can't be wrong.
"""
from __future__ import annotations

import math
import os
import random
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import text as T
from .net import FolaioNet

TOPIC_CHARS = 1500          # smallest "topic" the network learns to recognise
MAX_TOPICS = 800            # big libraries get bigger topics, so learning stays fast
MIN_KNOWN = 0.5             # share of question words the library must know (Cony's idea)
TARGET_STEPS = 4000         # training steps; enough for small and large libraries


@dataclass
class Found:
    chunk_id: int
    score: float


class Mind:
    def __init__(self, folder: Path):
        self.file = folder / "mind.npz"
        self.net: FolaioNet | None = None
        self.accuracy: float | None = None
        self.learned_at: float | None = None
        self.load()

    @property
    def ready(self) -> bool:
        return self.net is not None

    # ======================= learning =======================
    def learn(self, chunks: list[dict], progress=None) -> None:
        """Learn the whole library. `chunks`: dicts with id, doc_id, text, in reading order."""
        if not chunks:
            self.forget()
            return
        texts = [c["text"] for c in chunks]
        tf = T.encode(texts)
        n = len(chunks)
        df = np.bincount(tf.indices, minlength=T.DIM).astype(np.float32)
        idf = np.log(1 + (n - df + 0.5) / (df + 0.5)).astype(np.float32)

        # Automatic labels: consecutive passages of each document form a topic.
        topic_chars = max(TOPIC_CHARS, sum(map(len, texts)) // MAX_TOPICS)
        topics = np.zeros(n, dtype=np.int64)
        topic, size, prev = -1, 0, None
        for i, c in enumerate(chunks):
            if c["doc_id"] != prev or size >= topic_chars:
                topic, size, prev = topic + 1, 0, c["doc_id"]
            topics[i] = topic
            size += len(c["text"])

        # Training examples: every sentence (and every whole passage) -> its topic.
        ex_text, ex_topic = [], []
        for i, t in enumerate(texts):
            for s in T.sentences(t):
                ex_text.append(s)
                ex_topic.append(topics[i])
            ex_text.append(t)
            ex_topic.append(topics[i])
        X = T.encode(ex_text).weighted(idf).normalized()
        y = np.array(ex_topic)

        # Keep 10% aside to measure honestly how well it learned.
        perm = np.random.default_rng(1).permutation(X.n_rows)
        cut = X.n_rows // 10 if X.n_rows >= 50 else 0
        test, train = perm[:cut], perm[cut:]
        net = FolaioNet(topic + 1)
        batches = max(1, math.ceil(len(train) / 64))
        epochs = int(np.clip(math.ceil(TARGET_STEPS / batches), 6, 400))
        net.fit(X.take(train), y[train], epochs=epochs, progress=progress)
        # Accuracy = how often it recognises which document a sentence it never saw came from.
        topic_doc = np.zeros(topic + 1, dtype=np.int64)
        topic_doc[topics] = [c["doc_id"] for c in chunks]
        accuracy = (float((topic_doc[net.proba(X.take(test)).argmax(1)] == topic_doc[y[test]]).mean())
                    if cut else None)

        vocab = sorted({T.stem(w) for t in texts for w in T.content_words(t)})
        state = dict(
            net.state(), idf=idf, tf_indptr=tf.indptr, tf_indices=tf.indices, tf_data=tf.data,
            emb=net.embed(tf.weighted(idf).normalized()), topics=topics,
            chunk_ids=np.array([c["id"] for c in chunks]), chunk_docs=np.array([c["doc_id"] for c in chunks]),
            vocab=np.array(vocab, dtype=str), accuracy=np.float32(-1 if accuracy is None else accuracy),
            learned_at=np.float64(time.time()),
        )
        tmp = self.file.with_suffix(".tmp.npz")
        np.savez(tmp, **state)
        os.replace(tmp, self.file)
        self._use(state)

    def load(self) -> None:
        if self.file.exists():
            try:
                with np.load(self.file) as z:
                    self._use({k: z[k] for k in z.files})
            except Exception:
                self.net = None

    def forget(self) -> None:
        self.net = None
        self.file.unlink(missing_ok=True)

    def _use(self, s: dict) -> None:
        self.net = FolaioNet.from_state(s)
        self.idf = s["idf"]
        self.tf = T.Sparse(s["tf_indptr"], s["tf_indices"], s["tf_data"])
        self.doc_len = np.diff(self.tf.indptr).astype(np.float32)
        self.avg_len = float(self.doc_len.mean()) if len(self.doc_len) else 1.0
        self.emb, self.topics = s["emb"], s["topics"]
        self.chunk_ids, self.chunk_docs = s["chunk_ids"], s["chunk_docs"]
        self.vocab = set(s["vocab"].tolist())
        acc = float(s["accuracy"])
        self.accuracy = None if acc < 0 else acc
        self.learned_at = float(s["learned_at"])

    # ======================= finding =======================
    def known_ratio(self, question: str) -> float:
        """How much of the question the library has ever seen (0..1)."""
        ws = T.content_words(question)
        if not ws or not self.ready:
            return 0.0
        return sum(T.stem(w) in self.vocab for w in ws) / len(ws)

    def search(self, query: str, k: int = 5, doc_id: int | None = None) -> list[Found]:
        if not self.ready or not T.content_words(query):
            return []
        q_ids = np.unique(T.feature_ids(query))
        keyword = self._bm25(q_ids)
        qx = T.encode([query]).weighted(self.idf).normalized()
        meaning = np.maximum(self.emb @ self.net.embed(qx)[0], 0)
        topic = self.net.proba(qx)[0][self.topics]
        score = (0.55 * keyword / max(keyword.max(), 1e-9)
                 + 0.25 * meaning
                 + 0.20 * topic / max(topic.max(), 1e-9))
        score[keyword == 0] *= 0.6   # passages sharing no words with the question rank lower
        if doc_id is not None:
            score[self.chunk_docs != doc_id] = -1
        top = np.argsort(-score)[:k]
        return [Found(int(self.chunk_ids[i]), float(score[i])) for i in top if score[i] > 0]

    def _bm25(self, q_ids: np.ndarray, k1: float = 1.2, b: float = 0.75) -> np.ndarray:
        mask = np.isin(self.tf.indices, q_ids)
        rows = self.tf.row_ids()[mask]
        tf = self.tf.data[mask]
        part = self.idf[self.tf.indices[mask]] * tf * (k1 + 1) / (
            tf + k1 * (1 - b + b * self.doc_len[rows] / self.avg_len))
        out = np.zeros(self.tf.n_rows, dtype=np.float32)
        np.add.at(out, rows, part)
        return out

    # ======================= answering =======================
    def best_sentences(self, question: str, passages: list[str], n: int = 3,
                       min_ratio: float = 0.0) -> list[tuple[str, int]]:
        """The sentences that best answer the question: (sentence, passage number from 1).
        With `min_ratio`, only sentences scoring at least that share of the best one."""
        q_feats = set(T.feature_ids(question))
        q_weight = sum(self.idf[f] for f in q_feats) or 1.0
        q_emb = self.net.embed(T.encode([question]).weighted(self.idf).normalized())[0]
        scored = []
        for num, passage in enumerate(passages, start=1):
            sents = T.sentences(passage)
            if not sents:
                continue
            embs = self.net.embed(T.encode(sents).weighted(self.idf).normalized())
            for s, e in zip(sents, embs):
                cover = sum(self.idf[f] for f in set(T.feature_ids(s)) & q_feats) / q_weight
                scored.append((cover + 0.3 * float(e @ q_emb) - 0.03 * num, cover, s, num))
        scored.sort(reverse=True)
        top = scored[0][0] if scored else 0
        picked, seen = [], set()
        for score, cover, s, num in scored:
            key = s.lower()[:60]
            if cover == 0 and picked or key in seen or (picked and score < min_ratio * top):
                continue
            seen.add(key)
            picked.append((s, num))
            if len(picked) == n:
                break
        return picked

    def answer(self, question: str, passages: list[str], max_points: int = 3) -> str:
        """Answer with the documents' own best sentences, citing [n] = passage number."""
        q_stems = {T.stem(w) for w in T.content_words(question)}
        return "\n".join(f"- {_bold(s, q_stems)} [{n}]" for s, n in self.best_sentences(question, passages, max_points))

    # ======================= checking a student's answer =======================
    def check_answer(self, question: str, answer: str, passages: list[str], points: int = 4) -> dict:
        """Mark a written answer against the book.

        Key points = the book's best sentences for the question. Each one's key words
        (important words that aren't already in the question) are looked for in the
        answer, allowing different word endings. Answer sentences whose words the book
        doesn't support are flagged for the student to double-check.
        """
        q_stems = {T.stem(w) for w in T.content_words(question)}
        a_stems = {T.stem(w) for w in T.content_words(answer)}
        a_prefix = {st[:T.PREFIX] for st in a_stems if len(st) > T.PREFIX}
        has = lambda st: st in a_stems or (len(st) > T.PREFIX and st[:T.PREFIX] in a_prefix)
        weight = lambda st: float(self.idf[T.word_features(st)[0]])

        results = []
        candidates = self.best_sentences(question, passages, points + 2, min_ratio=0.6)
        # Stay in the part of the book where the best answer is (other chapters may
        # use the same words for a different idea).
        home = {num for _, num in candidates[:2]}
        for sentence, num in candidates:
            if num not in home:
                continue
            if re.match(r"(if|for (example|instance)|such as|e\.g\.)\b", sentence, re.I):
                continue   # an example illustrates a point; it isn't a key point itself
            if len(results) == points:
                break
            words, seen_st = [], set()
            for w in T.content_words(sentence):
                st = T.stem(w)
                if (st not in q_stems and st not in seen_st and len(w) >= 4 and w not in T.GENERIC
                        and st not in T.GENERIC and weight(st) > 0.5):
                    seen_st.add(st)
                    words.append((w, st))
            key = sorted(words, key=lambda x: -weight(x[1]))[:4]   # the most telling words
            if not key:
                continue
            got = _terms(sentence, [w for w, st in key if has(st)])
            missing = _terms(sentence, [w for w, st in key if not has(st)])
            share = sum(weight(st) for w, st in key if has(st)) / sum(weight(st) for _, st in key)
            status = "covered" if share >= 0.5 else "partly" if share > 0 else "missed"
            results.append({"text": sentence, "passage": num, "status": status,
                            "matched": got, "missing": missing})

        # Sentences in the answer the book doesn't back up (possible mistakes).
        book_stems = {T.stem(w) for p in passages for w in T.content_words(p)} | q_stems
        unsupported = []
        for s in T.sentences(answer, min_words=3) or [answer]:
            ws = [T.stem(w) for w in T.content_words(s)]
            if len(ws) >= 2:
                known = sum(st in book_stems or st in self.vocab for st in ws) / len(ws)
                if known < 0.5:
                    unsupported.append(s)

        done = sum(1 for r in results if r["status"] == "covered") + 0.5 * sum(
            1 for r in results if r["status"] == "partly")
        return {"points": results, "unsupported": unsupported,
                "score": round(done / len(results), 2) if results else 0.0}

    # ======================= studying =======================
    def summarize(self, passage_text: str, points: int = 4) -> list[str]:
        """Pick the sentences that best represent the passage, in reading order."""
        sents = [s for s in T.sentences(passage_text) if 6 <= len(T.words(s)) <= 60]
        if len(sents) <= points:
            return sents
        X = T.encode(sents).weighted(self.idf).normalized()
        centre = np.zeros(T.DIM, dtype=np.float32)
        np.add.at(centre, X.indices, X.data)
        per = X.data * centre[X.indices]
        sim = np.zeros(X.n_rows, dtype=np.float32)
        np.add.at(sim, X.row_ids(), per)
        best = sorted(np.argsort(-sim)[:points])
        return [sents[i] for i in best]

    def quiz(self, sections: list[tuple[int, str]], per_section: int = 3) -> list[dict]:
        """Fill-in-the-blank questions made from real sentences: the answer is always right.

        `sections`: (page, text) for each study section of one document. The word to blank
        is a *key term of that section*: concentrated there rather than spread over the
        book, preferably the subject of a definition ("Hyperinflation is ...") or a term
        from a heading. Wrong choices are other key terms of the same kind that the
        network learned are related, so they're tempting but clearly wrong.
        """
        texts = [t for _, t in sections]
        units = [_units(t) for t in texts]             # (key, surface) per word/compound
        per_sec = [Counter(k for k, _ in u) for u in units]
        total, forms = Counter(), {}
        for u in units:
            for k, w in u:
                total[k] += 1
                forms.setdefault(k, Counter())[w] += 1
        headings = {k for t in texts for line in t.split("\n") if _is_heading(line) for k, _ in _units(line)}
        defined = {}                                     # key -> sentence that defines it
        for t in texts:
            for sent in T.sentences(t):
                for k in _defined_keys(sent):
                    defined.setdefault(k, sent)
        # Words the book uses as things ("the frontier", "a tariff", "of scarcity"): answers
        # and wrong choices must be such terms, never verbs or describing words.
        nounish = {k for t in texts for k in _nouns(t)} | set(defined) | headings
        nounish |= {k for k in total if "-" in k}

        def idf(k):
            parts = [T.stem(p) for p in k.split("-")]
            return float(np.mean([self.idf[T.word_features(p)[0]] for p in parts]))

        def score(i, k):
            if (total[k] < 2 and k not in defined) or k not in nounish:
                return 0.0
            s = idf(k) * (1 + math.log(total[k])) * per_sec[i][k] / total[k]
            return s * (1.5 if k in headings else 1) * (1.3 if "-" in k else 1) * (1.3 if k in defined else 1)

        best = {k: max(score(i, k) for i in range(len(texts))) for k in total}
        keys = [k for k, v in best.items() if v > 0]
        if len(keys) < 4:
            return []

        def vec(k):
            return np.mean([self.net.W1[T.word_features(T.stem(p))[0]] for p in k.split("-")], axis=0)

        top = max(best.values())
        cards, used = [], set()
        for i, (page, text) in enumerate(sections):
            options = []
            for sent in T.sentences(text):
                n_words = len(T.words(sent))
                if not 7 <= n_words <= 40 or re.match(r"(if|for (example|instance))\b", sent, re.I):
                    continue
                in_sent = {k for k, _ in _units(sent)}
                subject = [k for k in _defined_keys(sent) if score(i, k) > 0]
                pool = subject or [k for k in in_sent if score(i, k) > 0]
                pool = [k for k in pool if k not in used]
                if not pool:
                    continue
                ans = max(pool, key=lambda k: score(i, k))
                options.append((score(i, ans) + (top if subject else 0), sent, ans, in_sent))
            made = 0
            for _, sent, ans, in_sent in sorted(options, key=lambda o: -o[0]):
                if made == per_section:
                    break
                if ans in used:
                    continue
                shown = _shown(sent, ans)
                question, blanks = _blank(sent, shown)
                if not 1 <= blanks <= 2:
                    continue
                shape = _shape(shown)
                others = [k for k in keys if k not in in_sent and k[:4] != ans[:4]]
                same = [k for k in others if _shape(_form(forms[k])) == shape and ("-" in k) == ("-" in ans)]
                if len(same) < 3:
                    same = [k for k in others if ("-" in k) == ("-" in ans)
                            and _shape(_form(forms[k]))[1] in (shape[1], "")]
                a = vec(ans)
                rank = lambda k: float(vec(k) @ a) / (np.linalg.norm(vec(k)) * np.linalg.norm(a) + 1e-9) \
                    + 0.5 * best[k] / top
                wrong = []
                for k in sorted(same, key=rank, reverse=True):
                    if all(k[:4] != o[:4] for o in wrong):
                        wrong.append(k)
                    if len(wrong) == 3:
                        break
                if len(wrong) < 3:
                    continue
                choices = [shown] + [_match_case(_form(forms[k]), shown) for k in wrong]
                random.shuffle(choices)
                cards.append({"page": page, "question": question, "choices": choices,
                              "answer": choices.index(shown),
                              "explanation": f"From the text: \u201c{sent}\u201d"})
                used.add(ans)
                made += 1
        return cards

def _terms(sentence: str, ws: list[str]) -> list[str]:
    """Show words as the sentence writes them: 'push' -> 'cost-push' (no repeats)."""
    out = []
    for w in ws:
        m = re.search(rf"(?:[\w']+-)*\b{re.escape(w)}\b(?:-[\w']+)*", sentence, re.I)
        term = m.group(0) if m else w   # as written, e.g. "Ricardo", "cost-push"
        if term.lower() not in (t.lower() for t in out):
            out.append(term)
    return out


def _bold(sentence: str, stems: set[str]) -> str:
    return re.sub(r"\b[\w']+\b",
                  lambda m: f"**{m.group()}**" if T.stem(m.group().lower()) in stems else m.group(), sentence)


def _form(forms: Counter) -> str:
    """How a word is normally written: lowercase unless it's always capitalised (a name)."""
    lower = [w for w in forms if w[:1].islower()]
    return max(lower, key=forms.get) if lower else forms.most_common(1)[0][0]


def _shape(w: str) -> tuple[bool, str]:
    """Rough word type, so wrong choices look like the right one (noun vs verb...)."""
    lw = w.lower()
    ending = next((e for e in ("ing", "ed", "ly") if lw.endswith(e)), "s" if lw.endswith("s") else "")
    return w[:1].isupper(), ending


def _match_case(w: str, like: str) -> str:
    return w[:1].upper() + w[1:] if like[:1].isupper() else w


_COMPOUND = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)+")
_DEFINITION = re.compile(
    r"^(?:the |a |an )?((?:[\w'-]+ ){0,2}[\w'-]+) (?:is|are|was|means|refers to|measures|occurs)\b", re.I)


def _key_word(w: str) -> str | None:
    """Can this word be a quiz answer? Returns its key (stem), or None for everyday words."""
    lw = w.lower()
    if (len(lw) < 4 or lw in T.STOP or lw in T.GENERIC or lw.isdigit() or lw.endswith("ly")
            or T.stem(lw) in T.GENERIC):
        return None
    return T.stem(lw)


def _units(text: str) -> list[tuple[str, str]]:
    """Candidate terms in reading order as (key, as written): hyphenated compounds
    ("cost-push") count as one term; other words by their stem."""
    out, taken = [], []
    for m in _COMPOUND.finditer(text):
        if all(len(p) >= 2 for p in m.group().split("-")):
            out.append((m.group().lower(), m.group()))
            taken.append(m.span())
    for m in re.finditer(r"[^\W_]+", text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        k = _key_word(m.group())
        if k:
            out.append((k, m.group()))
    return out


def _defined_keys(sentence: str) -> list[str]:
    """Terms a definition sentence is about: "Frictional unemployment is ..." -> the subject's
    candidate terms, most specific (first) first."""
    m = _DEFINITION.match(sentence)
    return [k for k, _ in _units(m.group(1))] if m else []


def _is_heading(line: str) -> bool:
    line = line.strip()
    return 2 < len(line) < 80 and not re.search(r"[.!?,;:]$", line)


def _blank(sentence: str, shown: str) -> tuple[str, int]:
    """Replace every appearance of the answer (as written, any capitals) with a blank."""
    count = 0

    def repl(m):
        nonlocal count
        count += 1
        return "_____"
    # whole word only, and not part of a hyphenated compound
    out = re.sub(rf"(?<![\w-]){re.escape(shown)}(?![\w-])", repl, sentence, flags=re.I)
    return out, count


def _nouns(text: str) -> set[str]:
    """Keys of words used as things: after the/a/an/of/its… (maybe with one word between)."""
    out = set()
    det = r"\b(?:the|a|an|of|its|their|this|these|those|each|every|some|any|no)\s+"
    adjective = re.compile(r"(al|ive|ic|ous|ful|less|able|ible|ical|ern)$", re.I)
    for m in re.finditer(det + r"([A-Za-z'-]+)(?:\s+([A-Za-z'-]+))?", text, re.I):
        first, second = m.group(1), m.group(2)
        words = [first]
        if second and adjective.search(first):   # "economic growth", "frictional unemployment"
            words.append(second)
        for w in words:
            k = w.lower() if "-" in w else _key_word(w)
            if k:
                out.add(k)
    return out


def _shown(sentence: str, key: str) -> str:
    """The answer as the sentence writes it."""
    for k, w in _units(sentence):
        if k == key:
            return w
    return key
