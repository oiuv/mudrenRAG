import faiss
import numpy as np

from app.config import DEFAULT_EMBEDDING_DIMENSION, DEFAULT_EMBEDDING_MODEL
from app.index_store import Snapshot
from app.lexical import tokenize


def vector(value=0.0, dimension=DEFAULT_EMBEDDING_DIMENSION):
    result = np.zeros(dimension, dtype=np.float32)
    result[0] = value
    return result


def snapshot(
    ids=(1, 2, 3), values=None, model=DEFAULT_EMBEDDING_MODEL, dimension=DEFAULT_EMBEDDING_DIMENSION,
    keyword_texts=None,
):
    index = faiss.IndexFlatL2(dimension)
    values = list(values) if values is not None else [i / 4 for i in range(len(ids))]
    if ids:
        index.add(np.array([vector(value, dimension) for value in values]))
    tokens = [tokenize(text) for text in keyword_texts] if keyword_texts is not None else None
    return Snapshot(index, [{"thread_id": thread_id} for thread_id in ids], model, tokens)
