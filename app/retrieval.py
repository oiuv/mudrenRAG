"""Candidate retrieval and reciprocal rank fusion, before model reranking."""
import numpy as np

from .config import Settings
from .index_store import Snapshot


def reciprocal_rank_fusion(rankings: list[list[int]], rank_constant: int) -> list[tuple[int, float]]:
    """Normalize RRF by its theoretical maximum so Dify scores remain in [0, 1]."""
    if rank_constant < 1:
        raise ValueError("RRF rank constant must be positive.")
    if not rankings:
        return []
    scores = {}
    for ranking in rankings:
        seen = set()
        rank = 0
        for position in ranking:
            if position in seen:
                continue
            seen.add(position)
            rank += 1
            scores[position] = scores.get(position, 0.0) + 1.0 / (rank_constant + rank)
    maximum = len(rankings) / (rank_constant + 1)
    # Stable sorting resolves ties by first appearance in the input rankings.
    return [
        (position, min(1.0, score / maximum))
        for position, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)
    ]


def retrieve_candidates(
    snapshot: Snapshot, query_vector: np.ndarray, query_text: str, settings: Settings,
    candidate_limit: int, filtering: bool,
) -> list[tuple[int, float]]:
    total = snapshot.index.ntotal
    if total == 0:
        return []
    vector_limit = max(candidate_limit, settings.vector_candidates) if settings.bm25_enabled else candidate_limit
    vector_limit = total if filtering else min(vector_limit, total)
    distances, indices = snapshot.index.search(query_vector, vector_limit)
    vectors = []
    for position, distance in zip(indices[0], distances[0]):
        if position < 0 or not np.isfinite(distance):
            continue
        vectors.append((int(position), 1.0 / (1.0 + max(0.0, float(distance)))))
    if settings.bm25_enabled:
        if snapshot.keyword_index is None:
            raise ValueError("BM25 index is missing; run python scripts/sync_data.py.")
        keyword_limit = total if filtering else min(max(candidate_limit, settings.bm25_candidates), total)
        keywords = snapshot.keyword_index.search(query_text, keyword_limit)
        ranked = reciprocal_rank_fusion(
            [[position for position, _ in vectors], [position for position, _ in keywords]],
            settings.rrf_k,
        )
    else:
        ranked = vectors
    return [(snapshot.entries[position]["thread_id"], score) for position, score in ranked]
