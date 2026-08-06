"""Issue #9: one place in full, resolved by GERS id or by name + point.

Issue #41: place_details(id=...) also accepts an optional near_lat/near_lon
location hint, and checks the local tile cache before ever touching
upstream — see the id-lookup tests below.
"""

import duckdb
import pytest

from placeroot import cache, overture, release, server

from .conftest import CENTER_LAT, CENTER_LON, FIXTURE_PATH, raw_rows


def _roastery_id() -> str:
    (row,) = [r for r in raw_rows() if r["name"] == "Blue Bottle Roastery"]
    return row["id"]


def _arctic_place_0():
    (row,) = [r for r in raw_rows() if r["name"] == "Arctic Place 0"]
    return row


def test_resolve_by_id():
    result = overture.place_details(id=_roastery_id())
    assert result is not None
    assert result["name"] == "Blue Bottle Roastery"
    assert result["brand"] == "Blue Bottle Coffee"
    assert result["addresses"] == [
        {
            "freeform": "123 Main St", "locality": "Metropolis",
            "region": "NY", "postcode": "10001", "country": "US",
        },
    ]
    assert result["websites"] == ["https://bluebottleroastery.example"]
    assert result["phones"] == ["+1-555-0100"]
    assert result["socials"] == ["https://instagram.example/bluebottleroastery"]
    assert result["sources"] == [{"dataset": "meta", "record_id": "meta-001"}]
    assert "confidence" in result
    assert "operating_status" in result


def test_resolve_by_name_and_point():
    result = overture.place_details(name="Roastery", lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000)
    assert result is not None
    assert result["id"] == _roastery_id()


def test_resolve_by_name_and_point_nearest_wins():
    """Several "Cluster Place NNN" names exist; a substring match near a
    specific point must resolve to the nearest one, not an arbitrary one."""
    result = overture.place_details(
        name="Cluster Place", lat=CENTER_LAT, lon=CENTER_LON, radius_m=50
    )
    assert result is not None
    nearby_ids = {
        r["id"] for r in overture.find_places(
            CENTER_LAT, CENTER_LON, radius_m=50, name="Cluster Place", limit=25
        )
    }
    assert result["id"] in nearby_ids


def test_requires_id_or_name_and_point():
    with pytest.raises(ValueError):
        overture.place_details()
    with pytest.raises(ValueError):
        overture.place_details(name="Roastery")


def test_not_found_by_id_returns_none():
    assert overture.place_details(id="does-not-exist") is None


def test_not_found_by_name_and_point_returns_none():
    assert overture.place_details(name="Nonexistent Place XYZ", lat=0.0, lon=0.0) is None


def test_long_arrays_truncate_with_a_count_never_silently():
    """Regression for #9: the "Cluster Place 010" fixture row has 8
    addresses and 8 sources — more than the truncation cap."""
    result = overture.place_details(
        name="Cluster Place 010", lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000
    )
    assert result is not None
    assert len(result["addresses"]) < 8
    assert result["addresses_omitted_count"] == 8 - len(result["addresses"])
    assert len(result["sources"]) < 8
    assert result["sources_omitted_count"] == 8 - len(result["sources"])


def test_confidence_missing_degrades_gracefully(tmp_path):
    out = tmp_path / "missing_confidence.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (confidence) FROM read_parquet("
        f"'{FIXTURE_PATH}')) TO '{out}' (FORMAT PARQUET)"
    )
    overture.set_data_path(str(out))
    result = overture.place_details(id=_roastery_id())
    assert result is not None
    assert result["confidence"] is None


def test_server_wraps_not_found():
    result = server.place_details(id="does-not-exist")
    assert result == {
        "error": "not_found",
        "detail": "no place matched id, or name near lat/lon",
    }


def test_server_wraps_bad_request():
    result = server.place_details()
    assert result["error"] == "bad_request"


def test_server_happy_path_includes_id():
    result = server.place_details(id=_roastery_id())
    assert "error" not in result
    assert result["id"] == _roastery_id()


def test_server_structured_error_on_unreachable_upstream(tmp_path):
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"))
    result = server.place_details(id="whatever")
    assert result["error"] == "upstream_unavailable"


# --- Issue #41: id lookups check the local tile cache / a location hint --- #


@pytest.fixture
def cached_id_lookup(tmp_path, monkeypatch):
    """Cache on, sync materialization (deterministic), isolated cache dir."""
    monkeypatch.setenv("PLACEROOT_CACHE", "on")
    monkeypatch.setenv("PLACEROOT_CACHE_SYNC", "1")
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "placeroot-cache"))


def test_id_in_cached_tile_short_circuits_upstream(cached_id_lookup, tmp_path):
    # Copy the fixture somewhere deletable, standing in for "upstream".
    upstream = tmp_path / "upstream.parquet"
    upstream.write_bytes(FIXTURE_PATH.read_bytes())
    overture.set_data_path(str(upstream))

    # Warm the tile covering the roastery (find_places is the common
    # real-world path that would have already done this) — a whole tile is
    # materialized regardless of which rows a particular query's LIMIT
    # keeps, so the roastery is cached even though it won't be among the
    # nearest 25 places to CENTER_LAT/CENTER_LON.
    overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    assert cache.cached_tile_paths(release.resolve_release(), overture.THEME) != []

    upstream.unlink()  # upstream is now gone; only the cached tile remains

    result = overture.place_details(id=_roastery_id())
    assert result is not None
    assert result["name"] == "Blue Bottle Roastery"


def test_id_hint_constrained_lookup_finds_place_outside_cache(cached_id_lookup, tmp_path):
    overture.set_data_path(str(FIXTURE_PATH))
    arctic = _arctic_place_0()

    # No tile cached for the arctic region yet.
    assert cache.cached_tile_paths(release.resolve_release(), overture.THEME) == []

    # A correct hint should still resolve it via the bbox-constrained
    # upstream path (not a full scan).
    result = overture.place_details(
        id=arctic["id"], near_lat=arctic["lat"], near_lon=arctic["lon"]
    )
    assert result is not None
    assert result["name"] == "Arctic Place 0"

    # And it must have materialized the matching cache tile as a side
    # effect of going through _from_source, ready for the next lookup.
    assert cache.cached_tile_paths(release.resolve_release(), overture.THEME) != []


def test_wrong_hint_falls_back_to_full_scan_and_still_finds_it(cached_id_lookup):
    """A hint that misses (wrong location) must not make the lookup fail —
    it falls back to the full-dataset scan, same as no hint at all."""
    arctic = _arctic_place_0()
    result = overture.place_details(
        id=arctic["id"], near_lat=CENTER_LAT, near_lon=CENTER_LON
    )
    assert result is not None
    assert result["name"] == "Arctic Place 0"


def test_bare_id_lookup_unchanged_with_cache_enabled(cached_id_lookup):
    """No hint at all: still resolves correctly (falls straight through
    cache-miss to a full scan), matching pre-#41 behavior."""
    result = overture.place_details(id=_roastery_id())
    assert result is not None
    assert result["name"] == "Blue Bottle Roastery"


def test_not_found_by_id_with_hint_returns_none(cached_id_lookup):
    assert overture.place_details(
        id="does-not-exist", near_lat=CENTER_LAT, near_lon=CENTER_LON
    ) is None


def test_server_not_found_shape_unchanged_with_hint(cached_id_lookup):
    result = server.place_details(id="does-not-exist", near_lat=CENTER_LAT, near_lon=CENTER_LON)
    assert result == {
        "error": "not_found",
        "detail": "no place matched id, or name near lat/lon",
    }
