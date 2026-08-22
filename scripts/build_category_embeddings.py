#!/usr/bin/env python3
"""Regenerate src/placeroot/data/category_embeddings.bin (#356).

    uv run python scripts/build_category_embeddings.py

Builds one hashed n-gram embedding vector (see src/placeroot/embeddings.py
for the feature scheme) per taxonomy row, from the exact same word set the
lexical fallback matches against (categories._row_word_set: slug words +
taxonomy path words + data/category_synonyms.csv words) — so the
embeddings backend and the lexical backend agree on what a row "means",
they just differ in how fuzzily they match it.

Re-run this whenever overture_categories.csv or category_synonyms.csv
changes (same trigger as the lexicon-validity test in
tests/test_search_categories_intent.py); the artifact has no other
external input, so the build is deterministic — running it twice with an
unchanged taxonomy produces byte-identical output.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from placeroot import categories, embeddings  # noqa: E402

OUT_PATH = ROOT / "src" / "placeroot" / "data" / "category_embeddings.bin"


def main() -> int:
    rows = categories._load_categories()
    slugs = []
    vectors = []
    for row in rows:
        words = sorted(categories._row_word_set(row))
        slugs.append(row["slug"])
        vectors.append(embeddings.embed_words(words))

    embeddings.save_artifact(OUT_PATH, slugs, vectors)
    size = OUT_PATH.stat().st_size
    print(
        f"wrote {OUT_PATH.relative_to(ROOT)}: {len(slugs)} rows, "
        f"dim={embeddings.EMBED_DIM}, {size:,} bytes ({size / 1024:.1f} KiB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
