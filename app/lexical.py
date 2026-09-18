"""Local BM25 retrieval with consistent Chinese/code tokenization."""
import heapq
import re
import unicodedata

import jieba
import numpy as np
from rank_bm25 import BM25L

TOKENIZER_VERSION = "jieba-code-v1"
_TOKENIZER = jieba.Tokenizer()
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LEXEMES = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff]+|[A-Za-z0-9_]+(?:(?:::|[./\\:-])[A-Za-z0-9_]+)*|[^\W_]+",
    re.UNICODE,
)
_CAMEL_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+")


def tokenize(text: str) -> list[str]:
    tokens = []
    for match in _LEXEMES.finditer(unicodedata.normalize("NFKC", text)):
        term = match.group()
        if _CJK.fullmatch(term):
            tokens.extend(token for token in _TOKENIZER.cut_for_search(term, HMM=False) if token.strip())
            continue
        # Preserve exact identifiers/paths, and also index snake_case/camelCase components.
        aliases = [term.casefold()]
        for part in re.findall(r"[A-Za-z0-9]+", term):
            aliases.append(part.casefold())
            aliases.extend(piece.casefold() for piece in _CAMEL_PARTS.findall(part))
        tokens.extend(dict.fromkeys(aliases))
    return tokens


class BM25Index:
    def __init__(self, documents: list[list[str]]):
        # BM25L has positive IDF even for tiny corpora and common terms.
        # rank-bm25 needs at least one nonempty document to compute average length.
        self._engine = BM25L(documents) if any(documents) else None

    def search(self, query: str, limit: int) -> list[tuple[int, float]]:
        if self._engine is None or limit <= 0:
            return []
        terms = list(dict.fromkeys(term for term in tokenize(query) if term in self._engine.idf))
        if not terms:
            return []
        scores = self._engine.get_scores(terms)
        matches = np.flatnonzero(np.isfinite(scores) & (scores > 0))
        positions = heapq.nsmallest(
            limit, matches, key=lambda position: (-float(scores[position]), int(position)),
        )
        return [(int(position), float(scores[position])) for position in positions]
