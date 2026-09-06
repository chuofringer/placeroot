"""Every category slug the query corpus asks find_places for must exist in the
bundled Overture taxonomy (#479).

Corpus id x08 asked for ``fuel_station`` -- not an Overture slug (the taxonomy
has ``gas_station``) -- and the weekly ``--fail-on wrong`` gate failed on it
for three runs while find_places, correctly, returned nothing with its
unknown-slug hint (#117). A typo in the corpus must fail here, at unit-test
time, not silently every Tuesday against live data.
"""

import re
from pathlib import Path

from placeroot import categories

CORPUS = Path(__file__).resolve().parents[1] / "benchmarks" / "query_corpus.py"

# The two shapes a category literal takes in the corpus: the positional slug
# of _flow(place, slug, ...) and any category="slug" keyword.
_FLOW_SLUG = re.compile(r'_flow\("[^"]+",\s*"([a-z_]+)"')
_KW_SLUG = re.compile(r'category="([a-z_]+)"')


def corpus_category_slugs() -> set[str]:
    source = CORPUS.read_text(encoding="utf-8")
    return set(_FLOW_SLUG.findall(source)) | set(_KW_SLUG.findall(source))


def test_corpus_uses_some_category_slugs():
    # Guard the guard: if the regexes stop matching the corpus, this fails
    # loudly instead of the slug test passing on an empty set.
    assert len(corpus_category_slugs()) >= 10


def test_every_corpus_category_slug_is_in_the_taxonomy():
    unknown = sorted(
        slug for slug in corpus_category_slugs() if categories.hierarchy_for(slug) is None
    )
    assert unknown == [], (
        f"benchmarks/query_corpus.py asks find_places for category slug(s) that are not "
        f"in src/placeroot/data/overture_categories.csv: {unknown}. find_places returns "
        f"nothing for an unknown slug (with a hint), so the corpus id can never pass. "
        f"Use search_categories to find the right slug."
    )
