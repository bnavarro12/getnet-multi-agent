"""BM25 keyword index (Okapi BM25), dependency-free.

Dense embeddings are good at meaning ("when does my money arrive" ~ "settlement period") but weak
at exact tokens: product names (Get Tap), codes (D+1, MDR), numbers. BM25 is the opposite. The
retriever fuses both rankings with Reciprocal Rank Fusion (see retriever.py).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

TOKEN_RE = re.compile(r"[a-z0-9]+(?:\+[0-9]+)?")   # keeps "d+1", "d+30" as single tokens
STOPWORDS = set("""
a o as os de da do das dos e em no na nos nas um uma para por com que se ao aos como mais ou sua seu
the of and to in for on with is are be can my your it this that how what do does i at by an or from
""".split())


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return [t for t in TOKEN_RE.findall(text) if t not in STOPWORDS and len(t) > 1]


class BM25:
    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(d)) for d in documents]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avgdl = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = tokenize(query)
        out = []
        for d, length in zip(self.docs, self.lengths):
            score = 0.0
            for t in terms:
                f = d.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * length / (self.avgdl or 1))
                score += self.idf.get(t, 0.0) * f * (self.k1 + 1) / denom
            out.append(score)
        return out
