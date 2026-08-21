"""Overture places category taxonomy: free text -> valid category slugs.

Backs the `search_categories` MCP tool (server.py). The taxonomy is a
bundled CSV snapshot (src/placeroot/data/overture_categories.csv, pinned to
Overture schema v1.9.0 — see data/README.md for format and refresh notes),
not a live query against the places dataset: this is a lookup-only helper,
with no geo filtering and no dependency on the upstream Overture connection
(overture.py). It works even if the upstream scan is unavailable.

CSV format (semicolon-space delimited, UTF-8 with BOM):
    Category code; Overture Taxonomy
    coffee_shop; [eat_and_drink,cafe,coffee_shop]

Each data row's second column is a bracketed, comma-separated taxonomy path
from root to leaf (the row's own slug is always the last segment).

A second bundled CSV (data/category_synonyms.csv, see data/README.md)
curates ~100 rows of intent words per slug ("mobile_phone_repair; phone
screen cracked repair fix cell smartphone") — it backs the token-based
fallback below for phrase intents like "fix my cracked phone screen" that
share no substring with any slug (#355).
"""

import importlib.resources

# Overture schema tag the bundled CSV was taken from; see data/README.md,
# which is the thing to update in lockstep when the snapshot is refreshed.
SCHEMA_VERSION = "v1.9.0"

_CATEGORIES: list[dict] | None = None
_SYNONYMS: dict[str, set[str]] | None = None

# Dropped before tokenizing a query or row text — filler words common in
# agent-phrased intents ("find me somewhere to get my phone fixed near
# here") that would otherwise dilute token-coverage scoring. Content words
# ("phone", "fixed") survive; this list is deliberately small so it never
# eats a real category word.
_STOPWORDS = {
    "a",
    "an",
    "the",
    "my",
    "me",
    "i",
    "to",
    "for",
    "of",
    "in",
    "on",
    "at",
    "and",
    "or",
    "with",
    "near",
    "nearby",
    "here",
    "close",
    "somewhere",
    "some",
    "any",
    "place",
    "places",
    "spot",
    "spots",
    "get",
    "go",
    "find",
    "want",
    "need",
    "looking",
    "look",
    "is",
    "are",
    "there",
    "that",
    "this",
    "good",
    "best",
    "please",
    "can",
    "you",
    "where",
    "do",
    "does",
}


def _load_categories() -> list[dict]:
    """Parse and cache the bundled taxonomy CSV. Read once per process."""
    global _CATEGORIES
    if _CATEGORIES is not None:
        return _CATEGORIES

    # Traversal from the package root, never a dotted "placeroot.data"
    # anchor: the dotted form imports the data directory as a module, which
    # is the one resolution step a polluted sys.path can break. Matches
    # every other data reader in the package (geocode.py, manifest.py,
    # land_use.py, release.py); guarded by tests/test_import_hardening.py.
    csv_path = importlib.resources.files("placeroot") / "data" / "overture_categories.csv"
    rows = []
    with csv_path.open("r", encoding="utf-8-sig") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            if i == 0:
                # Header row: "Category code; Overture Taxonomy"
                continue
            slug, _, taxonomy = line.partition("; ")
            slug = slug.strip()
            taxonomy = taxonomy.strip()
            if not slug or not taxonomy.startswith("[") or not taxonomy.endswith("]"):
                continue
            path = [seg.strip() for seg in taxonomy[1:-1].split(",") if seg.strip()]
            rows.append({"slug": slug, "path": path})

    _CATEGORIES = rows
    return _CATEGORIES


def _load_synonyms() -> dict[str, set[str]]:
    """Parse and cache data/category_synonyms.csv: slug -> synonym word set.

    Same traversal convention as _load_categories (see its comment) and
    same encoding/delimiter as the taxonomy CSV. A slug with no lexicon row
    simply has an empty synonym set; a lexicon row for a slug absent from
    the taxonomy is silently skipped (the lexicon-validity test in
    tests/test_search_categories_intent.py is what actually enforces every
    row is real).
    """
    global _SYNONYMS
    if _SYNONYMS is not None:
        return _SYNONYMS

    csv_path = importlib.resources.files("placeroot") / "data" / "category_synonyms.csv"
    words: dict[str, set[str]] = {}
    with csv_path.open("r", encoding="utf-8-sig") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line or i == 0:
                continue
            slug, _, synonyms = line.partition(";")
            slug = slug.strip()
            if not slug:
                continue
            words[slug] = set(_tokenize(synonyms))

    _SYNONYMS = words
    return _SYNONYMS


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on runs of non-alphanumerics, drop stopwords.

    Splits slug underscores into words too ("mobile_phone_repair" ->
    "mobile", "phone", "repair"), so a query token can match a word buried
    in a multi-word slug. Empty-after-stopwords input returns [].
    """
    words = []
    current = []
    for ch in text.lower():
        if ch.isalnum():
            current.append(ch)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return [w for w in words if w not in _STOPWORDS]


def _row_word_set(row: dict) -> set[str]:
    """A row's matchable words: slug words + path-segment words + its
    curated synonym words (data/category_synonyms.csv)."""
    words = set(_tokenize(row["slug"]))
    for seg in row["path"]:
        words.update(_tokenize(seg))
    words.update(_load_synonyms().get(row["slug"], set()))
    return words


def _document_frequencies(rows: list[dict]) -> dict[str, int]:
    """How many rows' word sets contain each token — used to down-weight
    tokens so common they'd surface half the taxonomy on their own (e.g.
    "shop", "service", "store")."""
    df: dict[str, int] = {}
    for row in rows:
        for word in _row_word_set(row):
            df[word] = df.get(word, 0) + 1
    return df


def taxonomy_summary() -> dict:
    """Shape of the taxonomy: {"total": int, "top_level": [(name, count), ...]}.

    `top_level` is every root segment of the taxonomy paths with how many
    slugs sit beneath it, ordered by descending count then name — a stable
    order that does not depend on CSV row order or dict iteration. Backs
    the `placeroot://categories` resource (resources.py), which is a
    summary precisely because the full 2,100-slug list belongs behind
    search_categories rather than in every conversation's context.
    """
    rows = _load_categories()
    counts: dict[str, int] = {}
    for row in rows:
        if row["path"]:
            counts[row["path"][0]] = counts.get(row["path"][0], 0) + 1
    top_level = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"total": len(rows), "top_level": top_level}


def hierarchy_for(slug: str) -> list[str] | None:
    """A slug's full taxonomy path, root first, e.g. playground ->
    ["active_life", "sports_and_recreation_venue", "playground"]. None if
    the slug isn't in the bundled taxonomy.

    Used by recreation.py to fill `taxonomy.hierarchy` and `basic_category`
    (its root segment) on the base-theme rows it projects into the places
    shape, from the same pinned snapshot search_categories answers from
    rather than a second hand-written table that could disagree with it.
    """
    for row in _load_categories():
        if row["slug"] == slug:
            return list(row["path"]) or None
    return None


def slugs_under(slug: str) -> list[str]:
    """Every bundled slug whose path contains `slug` as a segment.

    Includes the slug itself when it is in the taxonomy. Used by verdict
    compose to expand a need (school → elementary_school) without the
    substring false positives of ILIKE (driving_school is not a school).
    Unknown slugs return [].
    """
    needle = (slug or "").strip().lower()
    if not needle:
        return []
    return [
        row["slug"]
        for row in _load_categories()
        if any(seg.lower() == needle for seg in row["path"])
    ]


def _match_rank(slug: str, query: str) -> int | None:
    """Lower is better; None means no match. query is already lowercased."""
    slug_l = slug.lower()
    if slug_l == query:
        return 0
    if slug_l.startswith(query):
        return 1
    if query in slug_l:
        return 2
    return None


# Whole-query tier -> confidence. Tier 0-2 are slug exact/prefix/substring,
# tier 3 is a path-segment match — the same four tiers _match_rank and the
# path-segment fallback have always produced, now surfaced as a number.
_TIER_CONFIDENCE = {0: 1.0, 1: 0.9, 2: 0.75, 3: 0.6}

# A token that shows up in more than this fraction of rows' word sets
# ("shop", "service", "store", ...) is too generic to carry a match on its
# own; it still counts but at reduced weight (see _TOKEN_MATCH_BAND).
_HIGH_DF_FRACTION = 0.10
_HIGH_DF_WEIGHT = 0.3

# Token-coverage confidence band for the phrase-intent fallback (#355):
# a query matching none of its tokens never reaches this code path (see
# search_categories), and full coverage tops out below the whole-query
# tiers above so an exact/substring slug hit always outranks a synonym hit.
_TOKEN_MATCH_MIN = 0.2
_TOKEN_MATCH_MAX = 0.7


def _whole_query_matches(query: str, rows: list[dict]) -> list[tuple]:
    """Today's exact/prefix/substring/path-segment tiers, scored rows."""
    scored = []
    for row in rows:
        rank = _match_rank(row["slug"], query)
        if rank is None:
            # No slug match — fall back to a path-segment match (tier 3).
            if any(query in seg.lower() for seg in row["path"]):
                rank = 3
            else:
                continue
        scored.append((_TIER_CONFIDENCE[rank], len(row["slug"]), row["slug"], row))
    return scored


def _token_matches(query_tokens: list[str], rows: list[dict]) -> list[tuple]:
    """Lexical phrase-intent fallback: score each row by weighted token
    coverage against its slug/path/synonym words (see module docstring).

    A row only qualifies if at least one matched token is not "high
    document-frequency" (shared across a large slice of the taxonomy) —
    otherwise a word like "shop" alone would surface half the taxonomy.

    Plural queries fold to the singular the taxonomy uses: a token ending
    in "s" (length > 3, so "gas" survives) also matches its bare stem, so
    "coffee shops" scores 2/2 against coffee_shop's {coffee, shop} instead
    of losing the "shops" token and tying with every other coffee-synonym
    row.
    """
    rows_with_words = [(row, _row_word_set(row)) for row in rows]
    df = _document_frequencies(rows)
    threshold = _HIGH_DF_FRACTION * len(rows)

    def _hit(token: str, words: set[str]) -> str | None:
        if token in words:
            return token
        if len(token) > 3 and token.endswith("s") and token[:-1] in words:
            return token[:-1]
        return None

    scored = []
    for row, words in rows_with_words:
        matched = [hit for t in query_tokens if (hit := _hit(t, words)) is not None]
        if not matched:
            continue
        weights = [1.0 if df.get(t, 0) <= threshold else _HIGH_DF_WEIGHT for t in matched]
        if all(df.get(t, 0) > threshold for t in matched):
            # Every matched token is too generic on its own — drop the row
            # rather than let it ride shared filler words into the results.
            continue
        coverage = sum(weights) / len(query_tokens)
        coverage = min(coverage, 1.0)
        confidence = _TOKEN_MATCH_MIN + coverage * (_TOKEN_MATCH_MAX - _TOKEN_MATCH_MIN)
        scored.append((confidence, len(row["slug"]), row["slug"], row))
    return scored


def search_categories(query: str, limit: int = 8) -> list[dict]:
    """Free text -> ranked {"slug", "path", "confidence"} rows from the
    bundled taxonomy.

    Case-insensitive. Tries today's whole-query tiers first: exact slug
    match (confidence 1.0) > slug prefix (0.9) > slug substring (0.75) >
    query matches a path segment, root/mid tier not the slug itself (0.6)
    — e.g. "cafe" surfaces both the "cafe" mid-tier category and slugs
    like "coffee_shop" that sit under it, disambiguating siblings.

    If nothing matches the whole query, falls back to a lexical
    phrase-intent match (#355): the query is tokenized (lowercased,
    split on non-alphanumerics, stopwords dropped) and scored against
    each row's slug words, path-segment words, and curated synonym words
    (data/category_synonyms.csv) — e.g. "fix my cracked phone screen"
    reaches mobile_phone_repair even though it shares no substring with
    it. Confidence there is weighted token coverage scaled into 0.2-0.7,
    always below the whole-query tiers; a token shared across a large
    slice of the taxonomy ("shop", "service") counts for less, and a row
    that only matched such tokens is dropped.

    Ties break by shorter slug, then alphabetically. Empty, whitespace,
    or stopword-only query returns [].
    """
    query = query.strip().lower()
    if not query:
        return []

    rows = _load_categories()
    scored = _whole_query_matches(query, rows)
    if not scored:
        query_tokens = _tokenize(query)
        if query_tokens:
            scored = _token_matches(query_tokens, rows)

    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    return [
        {"slug": r["slug"], "path": r["path"], "confidence": round(conf, 2)}
        for conf, _, _, r in scored[:limit]
    ]
