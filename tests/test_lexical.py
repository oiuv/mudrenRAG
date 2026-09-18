import numpy as np
import pytest

from app.config import Settings
from app.lexical import BM25Index, tokenize
from app.retrieval import reciprocal_rank_fusion, retrieve_candidates
from tests.helpers import snapshot, vector


def test_chinese_search_segmentation():
    assert "冷却" in tokenize("技能冷却时间")
    index = BM25Index([tokenize("技能冷却时间与触发条件"), tokenize("银行存款利率")])
    assert [position for position, _ in index.search("冷却时间", 10)] == [0]


def test_code_identifiers_paths_and_case_are_preserved():
    tokens = tokenize("query_temp HTTPStatusError /std/room.c combat/target")
    assert {"query_temp", "query", "temp", "httpstatuserror", "http", "status", "error", "std/room.c", "combat/target"} <= set(tokens)
    index = BM25Index([
        tokenize("query_temp controls /std/room.c"),
        tokenize("query user name"),
        tokenize("temporary display settings"),
    ])
    assert index.search("QUERY_TEMP", 1)[0][0] == 0
    assert index.search("/std/room.c", 1)[0][0] == 0
    assert tokenize("ＱＵＥＲＹ＿ＴＥＭＰ") == tokenize("query_temp")


@pytest.mark.parametrize("documents", [[], [[]], [[], []]])
def test_empty_corpora_return_no_results(documents):
    assert BM25Index(documents).search("keyword", 5) == []


def test_unmatched_words_and_punctuation_do_not_add_candidates():
    index = BM25Index([["alpha"], ["beta"]])
    assert index.search("unknownword", 20) == []
    assert index.search("！？()", 20) == []
    assert index.search("alpha", 0) == []
    assert [position for position, _ in index.search("alpha", 20)] == [0]


def test_common_terms_still_match_a_small_corpus():
    index = BM25Index([["shared"], ["shared"], []])
    results = index.search("shared", 10)
    assert [position for position, _ in results] == [0, 1]
    assert all(score > 0 for _, score in results)


def test_duplicate_query_words_do_not_change_rankings():
    index = BM25Index([["alpha", "beta"], ["alpha"], ["beta"]])
    assert index.search("alpha beta", 3) == index.search("alpha alpha beta", 3)


def test_rrf_formula_deduplication_and_score_range():
    result = reciprocal_rank_fusion([[1, 2], [2, 3]], 60)
    scores = dict(result)
    assert [position for position, _ in result] == [2, 1, 3]
    assert scores[2] == pytest.approx((1 / 62 + 1 / 61) / (2 / 61))
    assert scores[1] == pytest.approx(0.5)
    assert all(0 < score <= 1 for score in scores.values())
    assert reciprocal_rank_fusion([[1, 1, 2], [2]], 60) == reciprocal_rank_fusion([[1, 2], [2]], 60)
    assert reciprocal_rank_fusion([[1], [1]], 60) == [(1, 1.0)]
    assert reciprocal_rank_fusion([], 60) == []
    assert reciprocal_rank_fusion([[], []], 60) == []


def test_bm25_adds_candidates_outside_the_vector_window(tmp_path):
    data = snapshot(
        ids=(1, 2, 3), values=(0, 1, 5),
        keyword_texts=["weather report", "bank accounts", "query_temp"],
    )
    settings = Settings("test", "test", tmp_path, vector_candidates=1, bm25_candidates=1)
    results = retrieve_candidates(data, np.asarray([vector()]), "query_temp", settings, 1, False)
    assert {thread_id for thread_id, _ in results} == {1, 3}
    assert len(results) == 2


def test_fusion_deduplicates_shared_candidates(tmp_path):
    data = snapshot(ids=(1, 2), keyword_texts=["query_temp", "bank accounts"])
    settings = Settings("test", "test", tmp_path)
    results = retrieve_candidates(data, np.asarray([vector()]), "query_temp", settings, 2, False)
    assert [thread_id for thread_id, _ in results] == [1, 2]
    assert results[0][1] == 1.0


def test_disabling_bm25_preserves_vector_scores(tmp_path):
    data = snapshot(ids=(1, 2))
    settings = Settings("test", "test", tmp_path, bm25_enabled=False)
    results = retrieve_candidates(data, np.asarray([vector()]), "question", settings, 2, False)
    assert results == [(1, 1.0), (2, 1 / (1 + 0.25 ** 2))]
