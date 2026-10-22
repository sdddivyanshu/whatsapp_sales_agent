"""Document retrieval over catalogue text chunks (BM25 keyword ranking).

Kept dependency-free on purpose: catalogues are small, and exact technical terms
(model codes, "wire cut", "taper") matter more than semantic similarity. The
``Retriever`` interface makes it easy to swap in an embedding index later.
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from app.repositories.database import Database

_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "is", "are", "it", "this", "that",
    "with", "on", "at", "by", "be", "we", "you", "i", "me", "my", "your", "our", "can", "do",
    "does", "what", "which", "how", "please", "pls", "hi", "hello", "sir", "madam", "ji",
}


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 1]


@dataclass
class Snippet:
    source_file: str
    source_reference: str
    text: str
    score: float


class Retriever(Protocol):
    def search(self, query: str, k: int = 4) -> list[Snippet]: ...


class BM25Retriever:
    def __init__(self, db: Database, k1: float = 1.5, b: float = 0.75) -> None:
        self.db = db
        self.k1, self.b = k1, b
        self._version: str | None = None
        self._docs: list[tuple[str, str, str, Counter, int]] = []
        self._df: Counter = Counter()
        self._avgdl = 1.0
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        version = self.db.get_meta("kb_version") or ""
        if version == self._version and self._docs:
            return
        with self._lock:
            rows = self.db.query("SELECT source_file, source_reference, text FROM doc_chunks")
            docs = []
            df: Counter = Counter()
            for r in rows:
                tokens = tokenize(r["text"])
                tf = Counter(tokens)
                docs.append((r["source_file"], r["source_reference"] or "", r["text"], tf, len(tokens)))
                df.update(tf.keys())
            self._docs, self._df = docs, df
            self._avgdl = (sum(d[4] for d in docs) / len(docs)) if docs else 1.0
            self._version = version

    def search(self, query: str, k: int = 4) -> list[Snippet]:
        self._refresh()
        terms = tokenize(query)
        if not terms or not self._docs:
            return []
        n = len(self._docs)
        scored: list[Snippet] = []
        for source, ref, text, tf, length in self._docs:
            score = 0.0
            for term in terms:
                if term not in tf:
                    continue
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                freq = tf[term]
                score += idf * freq * (self.k1 + 1) / (
                    freq + self.k1 * (1 - self.b + self.b * length / self._avgdl))
            if score > 0:
                scored.append(Snippet(source, ref, text, score))
        scored.sort(key=lambda s: -s.score)
        return scored[:k]
