import faiss
import numpy as np

from app.config import EMBEDDING_DIMENSION
from app.index_store import Snapshot


def vector(value=0.0):
    result = np.zeros(EMBEDDING_DIMENSION, dtype=np.float32)
    result[0] = value
    return result


def snapshot(ids=(1, 2, 3), values=None):
    index = faiss.IndexFlatL2(EMBEDDING_DIMENSION)
    values = list(values) if values is not None else [i / 4 for i in range(len(ids))]
    if ids:
        index.add(np.array([vector(value) for value in values]))
    return Snapshot(index, [{"thread_id": thread_id} for thread_id in ids])
