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

_CATEGORIES: list[dict] | None = None


def _load_categories() -> list[dict]:
    """Parse and cache the bundled taxonomy CSV. Read once per process."""
    global _CATEGORIES
    if _CATEGORIES is not None:
        return _CATEGORIES

    csv_path = importlib.resources.files("placeroot.data").joinpath("overture_categories.csv")
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
