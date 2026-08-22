#!/usr/bin/env python3
"""Hit-rate corpus for `search_categories`' embeddings tail extension (#356).

    uv run python benchmarks/category_intent_corpus.py

Offline, no network — pure exercise of `placeroot.categories.search_categories`
against the bundled taxonomy/lexicon/embeddings artifacts. Each entry is a
free-text intent phrase and the set of taxonomy slugs that would be a
sensible answer; a query is a "hit" if any acceptable slug appears in the
top `limit` results.

Two groups, because the acceptance criteria in #356/#307 ask for an honest
number, not a flattering one:

- **lexicon-covered** control queries (words already in
  data/category_synonyms.csv or a slug/path segment) — these MUST stay a
  hit with embeddings on, proving the "never degrades a lexical hit" gate
  (categories._EMBED_MATCH_MAX < categories._TOKEN_MATCH_MIN) actually
  holds, not just in theory.
- **long-tail** queries chosen to share no token with any slug, path
  segment, or synonym row (misspellings, morphological variants, and
  genuine semantic paraphrases with zero vocabulary overlap) — this is
  the set embeddings can move at all, and where a hashed-n-gram bag (no
  real semantics, see embeddings.py's docstring) is expected to help with
  typos/word-fragments and not with true paraphrase.

Run with `--lexical-only` to see the pre-#356 baseline (patches
embeddings.embedding_similarities to return [] for the run).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from placeroot import categories, embeddings  # noqa: E402

LIMIT = 5

# (query, acceptable slugs, group) — group is "control" (already lexicon-
# reachable; must not regress) or "longtail" (shares no token with any
# slug/path/synonym word; only embeddings can reach these at all).
CASES = [
    # -- control: unchanged from #355, must still hit --------------------
    ("fix my cracked phone screen", {"mobile_phone_repair"}, "control"),
    ("place to work out",
     {"gym", "boxing_gym", "rock_climbing_gym", "sports_and_fitness_instruction"}, "control"),
    ("coffee shops", {"coffee_shop"}, "control"),
    ("grocery stores", {"grocery_store"}, "control"),
    ("somewhere to get my nails done", {"nail_salon"}, "control"),
    ("need to charge my phone",
     {"ev_charging_station", "mobile_phone_accessories", "mobile_phone_store"}, "control"),
    ("a place to donate old clothes",
     {"thrift_store", "used_vintage_and_consignment"}, "control"),
    ("somewhere to buy fresh flowers",
     {"florist", "flower_markets", "flowers_and_gifts_shop", "farmers_market"}, "control"),
    ("somewhere quiet to study",
     {"library", "tea_room", "cafe", "coffee_shop", "tutoring_center"}, "control"),
    ("somewhere to get a tattoo", {"tattoo", "tattoo_and_piercing"}, "control"),
    # -- long-tail: misspellings / morphological variants -----------------
    ("need a plumer", {"plumbing"}, "longtail"),
    ("crackd screen repare", {"mobile_phone_repair"}, "longtail"),
    ("need my flat tyre fixd",
     {"tire_shop", "tire_repair_shop", "tire_dealer_and_repair", "bike_repair_maintenance"},
     "longtail"),
    ("reserv a haircutt", {"hair_salon", "barber"}, "longtail"),
    ("librairy near me", {"library"}, "longtail"),
    ("bicicle repair shop", {"bicycle_shop", "bike_repair_maintenance"}, "longtail"),
    ("veterinarian clinick for my dog", {"veterinarian"}, "longtail"),
    ("dentistt appointment", {"dentist", "cosmetic_dentist", "general_dentistry"}, "longtail"),
    ("bakerey for bread", {"bakery"}, "longtail"),
    ("farmer's markett produce", {"farmers_market"}, "longtail"),
    # -- long-tail: genuine semantic paraphrase, zero vocabulary overlap --
    ("somewhere romantic for a date",
     {"restaurant", "bar", "cocktail_bar", "wine_bar"}, "longtail"),
    ("looking for a place to meditate", {"yoga_studio", "meditation_center"}, "longtail"),
    ("a place for kids to burn off energy",
     {"playground", "gymnastics_center", "kids_recreation_and_party", "trampoline_park"},
     "longtail"),
    ("somewhere to buy sneakers", {"shoe_store", "sporting_goods"}, "longtail"),
    ("need a mechanic for my car", {"automotive_repair"}, "longtail"),
    ("place to get a massage", {"massage", "massage_therapy"}, "longtail"),
    ("somewhere calm to read for an hour", {"library", "bookstore"}, "longtail"),
    ("somewhere to get my resume printed", {"commercial_printer", "shipping_center"}, "longtail"),
]


def _hits(limit: int, *, lexical_only: bool) -> tuple[int, int, list[str]]:
    orig = embeddings.embedding_similarities
    if lexical_only:
        embeddings.embedding_similarities = lambda *a, **k: []
    misses = []
    hit_n = 0
    try:
        for query, acceptable, _group in CASES:
            results = categories.search_categories(query, limit=limit)
            slugs = {r["slug"] for r in results}
            if slugs & acceptable:
                hit_n += 1
            else:
                misses.append(query)
    finally:
        embeddings.embedding_similarities = orig
    return hit_n, len(CASES), misses


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--limit", type=int, default=LIMIT)
    ap.add_argument("--lexical-only", action="store_true",
                     help="disable the embeddings tail extension for this run")
    args = ap.parse_args()

    print(f"{len(CASES)} queries, top-{args.limit} hit check "
          f"({'lexical-only' if args.lexical_only else 'lexical + embeddings blend'})\n")

    for group in ("control", "longtail"):
        cases = [c for c in CASES if c[2] == group]
        hit_n = 0
        for query, acceptable, _ in cases:
            embeddings_backup = embeddings.embedding_similarities
            if args.lexical_only:
                embeddings.embedding_similarities = lambda *a, **k: []
            results = categories.search_categories(query, limit=args.limit)
            embeddings.embedding_similarities = embeddings_backup
            slugs = {r["slug"] for r in results}
            ok = bool(slugs & acceptable)
            hit_n += ok
            mark = "hit " if ok else "MISS"
            top = [r["slug"] for r in results][:3]
            print(f"  {mark} {query[:42]:<44} -> {top}")
        print(f"{group}: {hit_n}/{len(cases)} hit\n")

    hit_n, total, misses = _hits(args.limit, lexical_only=args.lexical_only)
    print(f"TOTAL: {hit_n}/{total} hit@{args.limit}")
    if misses:
        print("misses:", misses)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
