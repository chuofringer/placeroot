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
"""

import importlib.resources

# Overture schema tag the bundled CSV was taken from; see data/README.md,
# which is the thing to update in lockstep when the snapshot is refreshed.
SCHEMA_VERSION = "v1.9.0"

_CATEGORIES: list[dict] | None = None


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


def search_categories(query: str, limit: int = 8) -> list[dict]:
    """Free text -> ranked {"slug", "path"} rows from the bundled taxonomy.

    Case-insensitive. Ranked: exact slug match > slug prefix > slug
    substring > query matches a path segment (root/mid tier, not the
    slug itself) — e.g. "cafe" surfaces both the "cafe" mid-tier category
    and slugs like "coffee_shop" that sit under it, disambiguating
    siblings. Ties break by shorter slug, then alphabetically. Empty or
    whitespace-only query returns [].
    """
    query = query.strip().lower()
    if not query:
        return []

    scored = []
    for row in _load_categories():
        rank = _match_rank(row["slug"], query)
        if rank is None:
            # No slug match — fall back to a path-segment match (tier 3).
            if any(query in seg.lower() for seg in row["path"]):
                rank = 3
            else:
                continue
        scored.append((rank, len(row["slug"]), row["slug"], row))

    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return [{"slug": r["slug"], "path": r["path"]} for _, _, _, r in scored[:limit]]
