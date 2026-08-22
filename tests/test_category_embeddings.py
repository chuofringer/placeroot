"""Tests for search_categories' embeddings tail extension (#356)."""

import importlib.resources

from placeroot import categories, embeddings


def _artifact_path():
    return importlib.resources.files("placeroot") / "data" / "category_embeddings.bin"


def test_artifact_is_bundled_and_small():
    path = _artifact_path()
    assert path.is_file(), "category_embeddings.bin must ship with the package"
    size = path.stat().st_size
    assert size < 3 * 1024 * 1024, f"artifact grew to {size} bytes, over the 3 MiB target"


def test_embed_words_is_deterministic_and_unit_normalized():
    v1 = embeddings.embed_words(["coffee", "shop"])
    v2 = embeddings.embed_words(["coffee", "shop"])
    assert v1 == v2
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_embed_words_empty_input_is_zero_vector():
    v = embeddings.embed_words([])
    assert all(x == 0.0 for x in v)


def test_embedding_similarities_missing_artifact_returns_empty(monkeypatch, tmp_path):
    embeddings.reset_cache()
    missing = tmp_path / "does_not_exist.bin"
    monkeypatch.setattr(embeddings, "_artifact_path", lambda: missing)
    try:
        assert embeddings.embedding_similarities(["coffee"]) == []
    finally:
        embeddings.reset_cache()


def test_search_categories_falls_back_cleanly_when_artifact_missing(monkeypatch, tmp_path):
    """Missing/unreadable artifact must not raise and must not change
    lexical-only behavior — the fallback contract #356 promises."""
    with_artifact = categories.search_categories("fix my cracked phone screen")

    embeddings.reset_cache()
    missing = tmp_path / "does_not_exist.bin"
    monkeypatch.setattr(embeddings, "_artifact_path", lambda: missing)
    try:
        without_artifact = categories.search_categories("fix my cracked phone screen")
    finally:
        embeddings.reset_cache()

    assert without_artifact == with_artifact
    assert without_artifact[0]["slug"] == "mobile_phone_repair"


def test_embeddings_never_outrank_a_lexical_hit():
    # Enforced structurally too (band comparison below), but exercise the
    # real pipeline: a query with a strong lexical hit must keep it first
    # regardless of what the embeddings tail would have ranked highest.
    results = categories.search_categories("coffee_shop", limit=20)
    assert results[0]["slug"] == "coffee_shop"
    assert results[0]["confidence"] == 1.0


def test_embed_confidence_band_stays_below_token_band():
    assert categories._EMBED_MATCH_MAX < categories._TOKEN_MATCH_MIN


def test_embeddings_only_fill_slots_lexical_left_empty():
    # "fix my cracked phone screen" already fills every slot via the
    # lexical fallback for a small limit — embeddings must contribute
    # nothing extra when there is no empty slot to fill.
    lexical_only = categories.search_categories("fix my cracked phone screen", limit=1)
    assert len(lexical_only) == 1
    assert lexical_only[0]["slug"] == "mobile_phone_repair"


def test_embeddings_extend_a_query_lexical_finds_nothing_for():
    # "need a plumer" (misspelled) shares no token with any slug, path
    # segment, or synonym row, so the lexical tiers return nothing; the
    # embeddings tail (character n-gram overlap with "plumbing") should
    # surface a plausible category where lexical-only returns [].
    lexical_index_before = categories._LEX_INDEX
    query_tokens = categories._tokenize("need a plumer")
    rows = categories._load_categories()
    lexical_scored = categories._whole_query_matches("need a plumer", rows) or (
        categories._token_matches(query_tokens, rows) if query_tokens else []
    )
    assert lexical_scored == [], "test assumption: lexical alone finds nothing for this query"
    assert lexical_index_before is not None  # sanity: index was built by the calls above

    results = categories.search_categories("need a plumer", limit=5)
    assert results, "embeddings should extend the tail when lexical is empty"
    for row in results:
        assert row["confidence"] < categories._TOKEN_MATCH_MIN


def test_server_tool_still_works_with_embeddings_enabled():
    from placeroot import server

    result = server.search_categories(query="fix my cracked phone screen")
    assert "error" not in result
    assert result["results"]
    assert result["results"][0]["slug"] == "mobile_phone_repair"
