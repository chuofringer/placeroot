#!/usr/bin/env python3
"""Builds the optional supplemental places layer (docs/SUPPLEMENT.md).

Overture's places theme is derived from business listings, which is exactly
the right shape for "where is the nearest pharmacy" and exactly the wrong
shape for family and recreation places that nobody lists as a business —
playgrounds, splash pads, beaches, trailheads, campgrounds. This script
builds a *local, opt-in* GeoParquet file of those places from open data and
`PLACEROOT_PLACES_SUPPLEMENT` points the query layer at it (see
overture.py). Nothing on the critical path changes: unset the variable and
PlaceRoot is Overture-only, keyless, zero-ETL, exactly as before
(CONTRIBUTING.md design rule #3).

Two sources, both keyless:

- **OpenStreetMap**, via the public Overpass API. ODbL 1.0 — attribution
  plus share-alike on derived databases, which is why this output is built
  locally and never redistributed with PlaceRoot.
- **IMLS Public Libraries Survey** outlet CSV, a US federal dataset in the
  public domain. Optional (`--imls-csv`); download it yourself from
  <https://www.imls.gov/research-evaluation/surveys/public-libraries-survey-pls>.

Every row carries its origin in `sources[0].dataset`, so a consumer can tell
a supplement row from an Overture one and honor the right license. Row `id`s
are `osm:<type>/<id>` or `imls:<FSCSKEY>-<FSCS_SEQ>` — **not GERS ids**; they
are stable within this dataset and meaningless outside it.

The output matches the full Overture places schema (overture.REQUIRED_COLUMNS
with the same struct types), so the query layer's degraded-fields logic keys
off upstream and needs no special case.

Usage:

    uv run python scripts/build_supplement.py \
        --bbox -74.05,40.60,-73.85,40.85 \
        --out ~/.placeroot/supplement-nyc.parquet

    uv run python scripts/build_supplement.py \
        --bbox -122.55,37.70,-122.35,37.83 \
        --categories playground,splash_pad,beach,park \
        --imls-csv ~/Downloads/pls_fy2023_outlet.csv \
        --out ~/.placeroot/supplement-sf.parquet

    export PLACEROOT_PLACES_SUPPLEMENT=~/.placeroot/supplement-nyc.parquet
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from placeroot import categories as categories_mod  # noqa: E402
from placeroot import db, overture, release  # noqa: E402

logger = logging.getLogger("build_supplement")

SCRIPT_VERSION = "1"

DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Overpass is a donated, shared resource with no key and no quota to buy.
# One request at a time, at most one every THROTTLE_S, and back off rather
# than retry hard — the polite-client contract that keeps it keyless.
THROTTLE_S = 3.0
MAX_ATTEMPTS = 4
BACKOFF_BASE_S = 10.0
OVERPASS_TIMEOUT_S = 300
USER_AGENT = (
    f"placeroot-build-supplement/{SCRIPT_VERSION} "
    "(+https://github.com/chuofringer/placeroot; hello@placeroot.dev)"
)

# Confidence is Overture's 0-1 "how sure are we this place is real". These
# rows don't come with one, so the values below are a documented heuristic,
# not a measurement: an IMLS library outlet is a surveyed federal record
# (0.7); an OSM feature is a volunteer edit of unknown vintage (0.5). Both
# sit below the confidence typical Overture places carry, so a caller
# filtering on min_confidence can exclude the supplement wholesale.
OSM_CONFIDENCE = 0.5
IMLS_CONFIDENCE = 0.7

# Supplement rows within this distance of an Overture place with the same
# normalized name are dropped as duplicates. Generous enough to cover the
# usual disagreement between a POI node and a building centroid, tight
# enough that two same-named branches a block apart both survive.
DEDUP_RADIUS_M = 150.0

EARTH_RADIUS_M = 6371000.0

# IMLS uses negative sentinels rather than blanks for "not applicable" /
# "not reported" / "suppressed". Left in place they become coordinates in
# the Atlantic and phone numbers of "-3".
IMLS_SENTINELS = {"-1", "-2", "-3", "-4", "-9"}
# Outlet types: CE central library, BR branch, BS bookmobile, BM
# books-by-mail. Only the first two are addresses a person can visit; a
# bookmobile's coordinate is a depot, not a destination.
IMLS_VISITABLE_TYPES = ("CE", "BR")


# --- OSM category mapping ---------------------------------------------------


@dataclass(frozen=True)
class OsmCategory:
    """One fetch-and-map unit: what to ask Overpass for, and what it becomes.

    `tags` is the OR-set of (key, value) pairs that identify the feature;
    each becomes its own Overpass filter (and its own node+way pair in the
    query). `taxonomy`/`alternates` are Overture category slugs — the
    alternates land in `taxonomy.alternates`, which find_places' category
    filter matches exactly, so a splash pad is reachable both as
    `playground` and as `water_park`.

    `allow_unnamed` marks the categories that are routinely mapped without
    a name and still worth returning: a playground, splash pad, trailhead
    or beach is useful as "there is one here" even nameless. Everything
    else without a name is dropped — an unnamed museum row is noise.
    """

    name: str
    tags: tuple[tuple[str, str], ...]
    taxonomy: str
    alternates: tuple[str, ...] = ()
    allow_unnamed: bool = False

    def matches(self, tags: dict) -> bool:
        return any(tags.get(k) == v for k, v in self.tags)


# Order is significant: classify() returns the first match, so the more
# specific splash-pad rule has to precede the plain playground one — a
# splash pad is tagged leisure=playground too, and would otherwise lose its
# water_park alternate.
CATEGORIES: tuple[OsmCategory, ...] = (
    OsmCategory(
        "splash_pad",
        (("playground", "splash_pad"), ("playground:water", "yes"), ("amenity", "splash_pad")),
        "playground", ("water_park",), allow_unnamed=True,
    ),
    OsmCategory("playground", (("leisure", "playground"),), "playground", allow_unnamed=True),
    OsmCategory("water_park", (("leisure", "water_park"),), "water_park"),
    OsmCategory("park", (("leisure", "park"),), "park"),
    OsmCategory("museum", (("tourism", "museum"),), "museum"),
    OsmCategory("library", (("amenity", "library"),), "library"),
    OsmCategory("zoo", (("tourism", "zoo"),), "zoo"),
    OsmCategory("aquarium", (("tourism", "aquarium"),), "aquarium"),
    OsmCategory("beach", (("natural", "beach"),), "beach", allow_unnamed=True),
    OsmCategory(
        "trailhead", (("information", "trailhead"),), "hiking_trail", ("trail",),
        allow_unnamed=True,
    ),
    OsmCategory(
        "campground", (("tourism", "camp_site"), ("tourism", "caravan_site")), "campground"
    ),
)
CATEGORIES_BY_NAME = {c.name: c for c in CATEGORIES}


def classify(tags: dict) -> OsmCategory | None:
    """The category an OSM element's tags map to, or None if none apply."""
    for category in CATEGORIES:
        if category.matches(tags):
            return category
    return None


def basic_category_for(slug: str) -> str | None:
    """Overture's top-level branch for a category slug, e.g. playground ->
    active_life. Read from the bundled taxonomy CSV so the value can't drift
    from what search_categories reports."""
    return categories_mod.top_level_branch(slug)


# --- row construction -------------------------------------------------------

# Same column order as tests/fixtures/places.parquet and Overture's own
# places schema, so a row tuple can be inserted positionally.
COLUMNS = (
    "id", "bbox", "names", "taxonomy", "basic_category", "operating_status",
    "confidence", "addresses", "websites", "phones", "socials", "brand", "sources",
)

CREATE_TABLE_SQL = """
    CREATE TABLE supplement (
        id VARCHAR,
        bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
        names STRUCT("primary" VARCHAR),
        taxonomy STRUCT("primary" VARCHAR, alternates VARCHAR[]),
        basic_category VARCHAR,
        operating_status VARCHAR,
        confidence DOUBLE,
        addresses STRUCT(
            freeform VARCHAR, locality VARCHAR, region VARCHAR,
            postcode VARCHAR, country VARCHAR
        )[],
        websites VARCHAR[],
        phones VARCHAR[],
        socials VARCHAR[],
        brand STRUCT("names" STRUCT("primary" VARCHAR)),
        sources STRUCT(dataset VARCHAR, record_id VARCHAR)[]
    )
"""


def place_row(
    *,
    id: str,
    lat: float,
    lon: float,
    name: str | None,
    taxonomy: str,
    alternates: tuple[str, ...] | list[str],
    confidence: float,
    dataset: str,
    record_id: str,
    address: dict | None = None,
    websites: list[str] | None = None,
    phones: list[str] | None = None,
) -> tuple:
    """One Overture-shaped places row.

    bbox is a degenerate point box (xmin == xmax, ymin == ymax) because
    that is how the query layer reads a place's coordinates —
    `bbox.ymin` is the latitude and `bbox.xmin` the longitude in every
    find_places/place_details SELECT.

    operating_status is always "open": these sources record features that
    exist, and neither carries a business-lifecycle field. brand and socials
    stay empty — a playground has no chain and no Instagram.
    """
    return (
        id,
        {"xmin": lon, "ymin": lat, "xmax": lon, "ymax": lat},
        {"primary": name},
        {"primary": taxonomy, "alternates": list(alternates)},
        basic_category_for(taxonomy),
        "open",
        confidence,
        [address] if address else [],
        websites or [],
        phones or [],
        [],
        None,
        [{"dataset": dataset, "record_id": record_id}],
    )


def osm_address(tags: dict) -> dict | None:
    """An Overture address struct from OSM addr:* tags, or None if there
    aren't any. freeform is housenumber + street, the shape Overture uses."""
    number = tags.get("addr:housenumber")
    street = tags.get("addr:street")
    freeform = " ".join(p for p in (number, street) if p) or None
    parts = {
        "freeform": freeform,
        "locality": tags.get("addr:city"),
        "region": tags.get("addr:state"),
        "postcode": tags.get("addr:postcode"),
        "country": tags.get("addr:country"),
    }
    if not any(parts.values()):
        return None
    return parts


def osm_row(element: dict) -> tuple | None:
    """One Overpass element -> a places row, or None if it can't be used.

    Dropped: elements with no usable coordinate (a way with no `center`,
    which is what `out center` exists to provide), tags matching no
    category, and unnamed elements outside the allow_unnamed categories.
    """
    tags = element.get("tags") or {}
    category = classify(tags)
    if category is None:
        return None
    name = tags.get("name")
    if not name and not category.allow_unnamed:
        return None

    lat, lon = element.get("lat"), element.get("lon")
    if lat is None or lon is None:
        center = element.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        return None

    record_id = f"{element.get('type')}/{element.get('id')}"
    website = tags.get("website") or tags.get("url")
    phone = tags.get("phone") or tags.get("contact:phone")
    return place_row(
        id=f"osm:{record_id}",
        lat=float(lat),
        lon=float(lon),
        name=name or None,
        taxonomy=category.taxonomy,
        alternates=category.alternates,
        confidence=OSM_CONFIDENCE,
        dataset="OpenStreetMap",
        record_id=record_id,
        address=osm_address(tags),
        websites=[website] if website else [],
        phones=[phone] if phone else [],
    )


# --- IMLS Public Libraries Survey ------------------------------------------


def imls_clean(value: str | None) -> str | None:
    """A PLS cell with the negative sentinels stripped, or None if empty.

    IMLS encodes "not applicable"/"not reported"/"suppressed" as -1/-3/-4
    (and a couple of neighbors) in *both* numeric and text columns, so this
    runs over every field, not just the coordinates."""
    if value is None:
        return None
    value = value.strip()
    if not value or value in IMLS_SENTINELS:
        return None
    return value


def imls_float(value: str | None) -> float | None:
    cleaned = imls_clean(value)
    if cleaned is None:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    # A sentinel that arrived as "-3.0" survives imls_clean's string
    # comparison; no library sits at a negative-integer coordinate either.
    if number in (-1.0, -2.0, -3.0, -4.0, -9.0):
        return None
    return number


def imls_row(record: dict) -> tuple | None:
    """One PLS *outlet* CSV row -> a places row, or None if unusable.

    Note the longitude column is `LONGITUD` — the PLS header is truncated to
    8 characters, and reading `LONGITUDE` silently yields nothing.

    Bookmobiles (`BS`) and books-by-mail (`BM`) outlets are skipped: their
    coordinates are a depot or a mailroom, not somewhere to take a child on
    a Saturday.
    """
    outlet_type = (imls_clean(record.get("C_OUT_TY")) or "").upper()
    if outlet_type not in IMLS_VISITABLE_TYPES:
        return None
    lat = imls_float(record.get("LATITUDE"))
    lon = imls_float(record.get("LONGITUD"))
    if lat is None or lon is None:
        return None
    name = imls_clean(record.get("LIBNAME"))
    if not name:
        return None

    fscskey = imls_clean(record.get("FSCSKEY")) or ""
    seq = imls_clean(record.get("FSCS_SEQ")) or ""
    record_id = f"{fscskey}-{seq}"
    address = {
        "freeform": imls_clean(record.get("ADDRESS")),
        "locality": imls_clean(record.get("CITY")),
        "region": imls_clean(record.get("STABR")),
        "postcode": imls_clean(record.get("ZIP")),
        "country": "US",
    }
    phone = imls_clean(record.get("PHONE"))
    return place_row(
        id=f"imls:{record_id}",
        lat=lat,
        lon=lon,
        name=name,
        taxonomy="library",
        alternates=(),
        confidence=IMLS_CONFIDENCE,
        dataset="IMLS Public Libraries Survey",
        record_id=record_id,
        address=address if any(v for v in address.values() if v != "US") else None,
        phones=[phone] if phone else [],
    )


def read_imls_csv(path: Path, bboxes: list[tuple[float, float, float, float]]) -> list[tuple]:
    """Every visitable outlet in the PLS CSV, filtered to bboxes if given.

    The published files are Latin-1-ish rather than UTF-8, so decoding is
    lenient — a mangled accent in one library's name is not worth failing a
    whole build over.
    """
    rows = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        for record in csv.DictReader(f):
            row = imls_row(record)
            if row is None:
                continue
            if bboxes and not in_any_bbox(row[1]["xmin"], row[1]["ymin"], bboxes):
                continue
            rows.append(row)
    return rows


def in_any_bbox(lon: float, lat: float, bboxes: list[tuple[float, float, float, float]]) -> bool:
    return any(
        xmin <= lon <= xmax and ymin <= lat <= ymax for xmin, ymin, xmax, ymax in bboxes
    )


# --- Overpass ---------------------------------------------------------------


def overpass_query(category: OsmCategory, bbox: tuple[float, float, float, float]) -> str:
    """Overpass QL for one category in one bbox: nodes and ways, `out center`.

    Ways come back with a `center` rather than a geometry — a playground is
    a polygon in OSM and a point here, which is all the query layer's
    point-in-radius math needs.
    """
    xmin, ymin, xmax, ymax = bbox
    area = f"({ymin},{xmin},{ymax},{xmax})"
    clauses = "".join(
        f'  node["{k}"="{v}"]{area};\n  way["{k}"="{v}"]{area};\n' for k, v in category.tags
    )
    return f"[out:json][timeout:{OVERPASS_TIMEOUT_S}];\n(\n{clauses});\nout center tags;\n"


class OverpassClient:
    """Polite, single-threaded Overpass caller: throttled, backing off, named.

    Overpass has no API key precisely because it runs on the honor system.
    THROTTLE_S between requests, exponential backoff on the two statuses
    that mean "you are asking too fast" (429) or "I gave up" (504), and a
    User-Agent that identifies this script and where to complain about it.
    """

    def __init__(self, url: str = DEFAULT_OVERPASS_URL, throttle_s: float = THROTTLE_S):
        self.url = url
        self.throttle_s = throttle_s
        self._last_request_at = 0.0

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.throttle_s:
            time.sleep(self.throttle_s - elapsed)

    def fetch(self, query: str) -> dict:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait()
            request = urllib.request.Request(
                self.url,
                data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=OVERPASS_TIMEOUT_S) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self._last_request_at = time.monotonic()
                return payload
            except urllib.error.HTTPError as e:
                self._last_request_at = time.monotonic()
                if e.code not in (429, 504) or attempt == MAX_ATTEMPTS:
                    raise
                delay = BACKOFF_BASE_S * (2 ** (attempt - 1))
                logger.warning(
                    "Overpass returned %s (attempt %d/%d); backing off %.0fs",
                    e.code, attempt, MAX_ATTEMPTS, delay,
                )
                time.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover


def fetch_osm_rows(
    client: OverpassClient,
    bboxes: list[tuple[float, float, float, float]],
    category_names: list[str],
) -> list[tuple]:
    """One Overpass request per (bbox, category), mapped to places rows.

    Deduplicated by row id: the splash-pad and playground queries overlap by
    construction (a splash pad carries leisure=playground too), and the
    first match wins because CATEGORIES puts the specific rule first.
    """
    by_id: dict[str, tuple] = {}
    for bbox in bboxes:
        for name in category_names:
            category = CATEGORIES_BY_NAME[name]
            logger.info("Overpass: %s in %s", name, ",".join(str(v) for v in bbox))
            payload = client.fetch(overpass_query(category, bbox))
            kept = 0
            for element in payload.get("elements", []):
                row = osm_row(element)
                if row is None or row[0] in by_id:
                    continue
                by_id[row[0]] = row
                kept += 1
            logger.info("  %d elements -> %d new rows", len(payload.get("elements", [])), kept)
    return list(by_id.values())


# --- dedup against Overture -------------------------------------------------

# Apostrophes are deleted rather than spaced out — "St. Mary's" and "St
# Marys" are the same library, and turning the apostrophe into a space would
# make them "mary s" and "marys" instead. Every other punctuation mark
# becomes a space, since it is usually standing in for one ("Park/Beach").
_APOSTROPHES = re.compile(r"['‘’ʼ]")
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_name(name: str | None) -> str:
    """Casefolded, punctuation-free, single-spaced form used to match a
    supplement row against an Overture one. Accents are folded too, so
    "Café Park" and "Cafe Park" are the same place."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    stripped = _PUNCTUATION.sub(" ", _APOSTROPHES.sub("", stripped))
    return _WHITESPACE.sub(" ", stripped).strip().casefold()


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def overture_names_in_bbox(
    con: duckdb.DuckDBPyConnection, glob: str, bbox: tuple[float, float, float, float]
) -> list[tuple[str, float, float]]:
    """(normalized name, lat, lon) for every named Overture place in bbox.

    One query per fetch bbox, not per row: the bbox predicate is what prunes
    row groups on the remote scan, and a per-row query would issue thousands
    of them for the same handful of groups.
    """
    xmin, ymin, xmax, ymax = bbox
    rows = con.execute(
        f"""
        SELECT names.primary, bbox.ymin, bbox.xmin
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE bbox.xmin <= $xmax AND bbox.xmax >= $xmin
          AND bbox.ymin <= $ymax AND bbox.ymax >= $ymin
          AND names.primary IS NOT NULL
        """,
        {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax},
    ).fetchall()
    return [(normalize_name(n), lat, lon) for n, lat, lon in rows]


def dedup_against_overture(
    rows: list[tuple],
    con: duckdb.DuckDBPyConnection,
    glob: str,
    bboxes: list[tuple[float, float, float, float]],
) -> tuple[list[tuple], dict[str, int]]:
    """Drop supplement rows Overture already has under the same name nearby.

    "Same" is a normalized-name match within DEDUP_RADIUS_M. Rows with no
    name are never dropped — there is nothing to match on, and the unnamed
    categories (playgrounds, trailheads, beaches) are precisely the ones
    Overture is thin on. Returns (kept rows, {category slug: dropped count}).
    """
    by_name: dict[str, list[tuple[float, float]]] = {}
    for bbox in bboxes:
        for name, lat, lon in overture_names_in_bbox(con, glob, bbox):
            if name:
                by_name.setdefault(name, []).append((lat, lon))

    kept, dropped = [], {}
    for row in rows:
        name = normalize_name(row[2]["primary"])
        lat, lon = row[1]["ymin"], row[1]["xmin"]
        near = any(
            haversine_m(lat, lon, olat, olon) <= DEDUP_RADIUS_M
            for olat, olon in by_name.get(name, ())
        ) if name else False
        if near:
            slug = row[3]["primary"]
            dropped[slug] = dropped.get(slug, 0) + 1
        else:
            kept.append(row)
    return kept, dropped


# --- CLI --------------------------------------------------------------------


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = value.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"--bbox takes minlon,minlat,maxlon,maxlat; got {value!r}"
        )
    try:
        xmin, ymin, xmax, ymax = (float(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--bbox values must be numbers; got {value!r}") from None
    if xmin >= xmax or ymin >= ymax:
        raise argparse.ArgumentTypeError(
            f"--bbox must be minlon,minlat,maxlon,maxlat with min < max; got {value!r}"
        )
    return xmin, ymin, xmax, ymax


def parse_categories(value: str) -> list[str]:
    names = [n.strip() for n in value.split(",") if n.strip()]
    unknown = [n for n in names if n not in CATEGORIES_BY_NAME]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown categor{'y' if len(unknown) == 1 else 'ies'} {', '.join(unknown)}; "
            f"valid: {', '.join(c.name for c in CATEGORIES)}"
        )
    return names


@dataclass
class BuildSummary:
    """What the run produced — printed, and written to the sidecar JSON."""

    per_source: dict[str, int] = field(default_factory=dict)
    per_category_kept: dict[str, int] = field(default_factory=dict)
    per_category_dropped: dict[str, int] = field(default_factory=dict)


def summarize(rows: list[tuple], dropped: dict[str, int]) -> BuildSummary:
    summary = BuildSummary(per_category_dropped=dict(dropped))
    for row in rows:
        dataset = row[12][0]["dataset"]
        slug = row[3]["primary"]
        summary.per_source[dataset] = summary.per_source.get(dataset, 0) + 1
        summary.per_category_kept[slug] = summary.per_category_kept.get(slug, 0) + 1
    return summary


def write_parquet(rows: list[tuple], out: Path) -> None:
    con = duckdb.connect()
    con.execute(CREATE_TABLE_SQL)
    if rows:
        placeholders = ", ".join("?" * len(COLUMNS))
        con.executemany(f"INSERT INTO supplement VALUES ({placeholders})", rows)
    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY supplement TO '{out}' (FORMAT PARQUET)")
    con.close()


def write_sidecar(
    out: Path,
    summary: BuildSummary,
    bboxes: list[tuple[float, float, float, float]],
    dedup_release: str | None,
    total: int,
) -> Path:
    """`<out>.meta.json` — what the data_version tool reports when this
    supplement is active, so an agent can see how old the layer is and what
    is in it without opening the parquet."""
    path = Path(f"{out}.meta.json")
    path.write_text(
        json.dumps(
            {
                "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "script_version": SCRIPT_VERSION,
                "rows": total,
                "dedup_overture_release": dedup_release,
                "per_source": summary.per_source,
                "per_category": summary.per_category_kept,
                "dropped_as_duplicate": summary.per_category_dropped,
                "bboxes": [list(b) for b in bboxes],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the optional supplemental places layer (docs/SUPPLEMENT.md).",
    )
    parser.add_argument(
        "--bbox", action="append", type=parse_bbox, default=None,
        metavar="minlon,minlat,maxlon,maxlat",
        help="Area to fetch from Overpass. Repeatable.",
    )
    parser.add_argument(
        "--categories", type=parse_categories, default=None,
        help=f"Comma list; default all ({', '.join(c.name for c in CATEGORIES)}).",
    )
    parser.add_argument(
        "--imls-csv", type=Path, default=None,
        help="IMLS Public Libraries Survey *outlet* CSV (optional; public domain).",
    )
    parser.add_argument("--out", type=Path, required=True, help="Output .parquet path.")
    parser.add_argument(
        "--no-dedup", action="store_true",
        help="Skip the pass that drops rows Overture already has.",
    )
    parser.add_argument("--overpass-url", default=DEFAULT_OVERPASS_URL)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)
    bboxes = args.bbox or []
    if not bboxes and not args.imls_csv:
        logger.error("nothing to do: pass --bbox (Overpass) and/or --imls-csv")
        return 2

    rows: list[tuple] = []
    if bboxes:
        category_names = args.categories or [c.name for c in CATEGORIES]
        client = OverpassClient(args.overpass_url)
        rows.extend(fetch_osm_rows(client, bboxes, category_names))
    if args.imls_csv:
        imls = read_imls_csv(args.imls_csv, bboxes)
        logger.info("IMLS: %d visitable outlets", len(imls))
        rows.extend(imls)

    dedup_release = None
    dropped: dict[str, int] = {}
    if rows and not args.no_dedup:
        # Dedup needs bboxes to prune the remote scan; with only --imls-csv
        # and no --bbox there is no box to scan, so derive one per row batch
        # from the rows themselves.
        dedup_bboxes = bboxes or [rows_bbox(rows)]
        dedup_release = release.resolve_release()
        glob = overture.upstream_glob()
        # The runtime's own connection setup: anonymous credentials for the
        # public bucket, region, and httpfs timeouts — the same reasoning as
        # scripts/overture_canary.py's, and the reason this isn't a bare
        # duckdb.connect().
        con = db.new_connection()
        try:
            rows, dropped = dedup_against_overture(rows, con, glob, dedup_bboxes)
        finally:
            con.close()

    summary = summarize(rows, dropped)
    write_parquet(rows, args.out)
    sidecar = write_sidecar(args.out, summary, bboxes, dedup_release, len(rows))

    print(f"\nwrote {len(rows)} rows to {args.out}")
    print(f"sidecar metadata: {sidecar}")
    for source, n in sorted(summary.per_source.items()):
        print(f"  source {source}: {n}")
    for slug in sorted(set(summary.per_category_kept) | set(summary.per_category_dropped)):
        kept = summary.per_category_kept.get(slug, 0)
        gone = summary.per_category_dropped.get(slug, 0)
        print(f"  {slug}: kept {kept}, dropped as duplicate {gone}")
    return 0


def rows_bbox(rows: list[tuple]) -> tuple[float, float, float, float]:
    """Bounding box covering every row — the dedup scan window when the run
    had no --bbox of its own (IMLS-only builds)."""
    lons = [r[1]["xmin"] for r in rows]
    lats = [r[1]["ymin"] for r in rows]
    return min(lons), min(lats), max(lons), max(lats)


if __name__ == "__main__":
    sys.exit(main())
