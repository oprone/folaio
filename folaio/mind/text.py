"""Text tools for Folaio Core (English).

No dictionaries and no pretrained models. Words are turned into numbers with a
fixed hash, so the memory needed never grows with the size of the library.
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass

import numpy as np

TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)?")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
DIM = 1 << 17          # number of hashed features
PREFIX = 5             # "inflation" ~ "inflationary": words sharing a start are related

STOP = frozenset("""
a an the and or but if of to in on at by for with from as is are was were be been being it its
this that these those there here he she they them his her their we you your our i me my not no
so than then too very can could will would shall should may might must do does did done have
has had having into over under about above below up down out off again once which who whom what
when where why how all any both each few more most other some such only own same just also
while because until during before after between through against per via
""".split())


# Everyday words that are never the "key term" of an idea (used when marking answers).
GENERIC = frozenset("""
example examples instance happen happens happened comes come coming came make makes made making
mean means meant use uses used using include includes including included call called calls
create creates creating created become becomes became give gives given take takes taken get gets
show shows shown way ways thing things part parts kind kinds type types often usually also still
every many much several various certain general main important different same other another
new old high low large small big little long short good bad better best next last first second
time times year years day days number amount level levels lot lots case cases fact point points
something someone anything everything nothing people person able whether rather instead however
chapter chapters section sections page pages figure figures table tables unit units lesson lessons
right left top bottom side sides end ends
""".split())


def words(text: str) -> list[str]:
    return [w.lower().removesuffix("'s") for w in TOKEN.findall(text)]


def content_words(text: str) -> list[str]:
    return [w for w in words(text) if w not in STOP and not (len(w) == 1 and w.isascii())]


def sentences(text: str, min_words: int = 4) -> list[str]:
    """Split into sentences. Heading lines (short, no full stop) are left out;
    very short fragments are glued onto the next sentence."""
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or (len(TOKEN.findall(line)) < 10 and not re.search(r"[.!?:;]$", line)):
            continue   # empty or a heading
        buf = ""
        for part in SENTENCE_END.split(line):
            buf = f"{buf} {part.strip()}".strip()
            if len(TOKEN.findall(buf)) >= min_words:
                out.append(buf)
                buf = ""
        if buf:
            if out:
                out[-1] = f"{out[-1]} {buf}"
            else:
                out.append(buf)
    return out


def _h(s: str) -> int:
    return zlib.crc32(s.encode("utf-8")) % DIM


_ENDINGS = (("ational", "ate"), ("ization", "ize"), ("fulness", "ful"), ("iveness", "ive"),
            ("ies", "y"), ("sses", "ss"), ("xes", "x"), ("ches", "ch"), ("shes", "sh"),
            ("ing", ""), ("ed", ""), ("ly", ""), ("s", ""))


def stem(w: str) -> str:
    """Tiny English stemmer: 'causes' -> 'cause', 'prices' -> 'price', 'taxes' -> 'tax'."""
    if w.endswith(("ss", "us", "is")):
        return w
    for end, rep in _ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= (2 if end.endswith("hes") or end == "xes" else 3):
            return w[:-len(end)] + rep
    return w


def word_features(w: str) -> list[int]:
    """A word's stem and its prefix, so different endings of a word still match."""
    st = stem(w)
    feats = [_h(st)]
    if len(st) > PREFIX + 1:
        feats.append(_h("~" + st[:PREFIX]))
    return feats


def feature_ids(text: str) -> list[int]:
    ids: list[int] = []
    for w in content_words(text):
        ids.extend(word_features(w))
    return ids


@dataclass
class Sparse:
    """A compact rows-of-features matrix (CSR), with just the math Folaio needs."""
    indptr: np.ndarray   # row i owns indices[indptr[i]:indptr[i+1]]
    indices: np.ndarray  # feature ids
    data: np.ndarray     # values

    @property
    def n_rows(self) -> int:
        return len(self.indptr) - 1

    def row_ids(self) -> np.ndarray:
        return np.repeat(np.arange(self.n_rows), np.diff(self.indptr))

    def take(self, rows: np.ndarray) -> "Sparse":
        starts, ends = self.indptr[rows], self.indptr[rows + 1]
        lens = ends - starts
        idx = np.concatenate([np.arange(s, e) for s, e in zip(starts, ends)]) if len(rows) else np.array([], int)
        return Sparse(np.concatenate([[0], np.cumsum(lens)]), self.indices[idx], self.data[idx])

    def dot(self, W: np.ndarray, block: int = 1024) -> np.ndarray:
        """(rows x DIM) @ (DIM x H) without ever building the big dense matrix.
        Works through `block` rows at a time so memory stays small."""
        out = np.zeros((self.n_rows, W.shape[1]), dtype=np.float32)
        for r0 in range(0, self.n_rows, block):
            r1 = min(r0 + block, self.n_rows)
            ptr = self.indptr[r0:r1 + 1]
            lo, hi = ptr[0], ptr[-1]
            nonempty = np.diff(ptr) > 0
            if hi > lo and nonempty.any():
                contrib = self.data[lo:hi, None] * W[self.indices[lo:hi]]
                out[r0:r1][nonempty] = np.add.reduceat(contrib, (ptr[:-1] - lo)[nonempty], axis=0)
        return out

    def weighted(self, weights: np.ndarray) -> "Sparse":
        return Sparse(self.indptr, self.indices, (self.data * weights[self.indices]).astype(np.float32))

    def normalized(self) -> "Sparse":
        sq = np.zeros(self.n_rows, dtype=np.float32)
        np.add.at(sq, self.row_ids(), self.data ** 2)
        norm = np.sqrt(np.maximum(sq, 1e-12))
        return Sparse(self.indptr, self.indices, (self.data / norm[self.row_ids()]).astype(np.float32))


def encode(texts: list[str]) -> Sparse:
    """Texts -> term counts per row (log-scaled, like Cony's sublinear_tf)."""
    indptr, indices, data = [0], [], []
    for t in texts:
        ids = np.array(feature_ids(t), dtype=np.int64)
        if len(ids):
            u, c = np.unique(ids, return_counts=True)
            indices.append(u)
            data.append(1.0 + np.log(c))
            indptr.append(indptr[-1] + len(u))
        else:
            indptr.append(indptr[-1])
    cat = lambda xs, t: np.concatenate(xs).astype(t) if xs else np.array([], dtype=t)
    return Sparse(np.array(indptr, dtype=np.int64), cat(indices, np.int64), cat(data, np.float32))
