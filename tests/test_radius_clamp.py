"""Issue #131: area_geometry() clamps radius_m to geo.MAX_QUERY_RADIUS_M but
used to silently drop the clamped value on the floor — callers kept using
the caller-supplied (unclamped) radius for logic/reporting, producing wrong
answers. These tests pin the fix: area_geometry's effective radius must
propagate to within_distance's inconclusiveness signal, summarize_area's
reported radius_m, and compare_areas' density calculation.
"""

import math

from placeroot import geo, overture

from .conftest import CENTER_LAT, CENTER_LON


def test_within_distance_notes_inconclusive_beyond_max_searchable_radius():
    """max_distance_m (600km) exceeds geo.MAX_QUERY_RADIUS_M (500km): the
    search physically cannot reach 600km, so a "no match" outcome cannot be
    reported as a confident within: False without a caveat."""
    result = overture.within_distance(
        0.0, 0.0, max_distance_m=600_000, name="Nonexistent Place XYZ"
    )
    assert result["within"] is False
    assert result["nearest"] is None
    assert "note" in result
    assert str(geo.MAX_QUERY_RADIUS_M) in result["note"] or "500000" in result["note"]


def test_within_distance_no_note_when_max_distance_within_searchable_radius():
    """max_distance_m (400km) is within the max searchable radius, so a "no
    match" outcome is a normal, confident within: False with no caveat."""
    result = overture.within_distance(
        0.0, 0.0, max_distance_m=400_000, name="Nonexistent Place XYZ"
    )
    assert result == {"within": False, "nearest": None, "distance_m": None}
    assert "note" not in result


def test_within_distance_true_beyond_cap_needs_no_note():
    """A real match found within max_distance_m is reported as within: True
    with no caveat, even if max_distance_m itself is above the cap."""
    result = overture.within_distance(CENTER_LAT, CENTER_LON, max_distance_m=600_000)
    assert result["within"] is True
    assert "note" not in result


def test_summarize_area_reports_effective_clamped_radius():
    """A radius_m far beyond geo.MAX_QUERY_RADIUS_M must be reported back as
    the clamped value that was actually searched, not the raw input."""
    result = overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m=5_000_000)
    assert result["radius_m"] == geo.MAX_QUERY_RADIUS_M


def test_summarize_area_reports_unchanged_radius_when_not_clamped():
    result = overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)
    assert result["radius_m"] == 1000


def test_compare_areas_density_uses_effective_clamped_radius():
    """compare_areas must compute area_km2/density from the clamped radius
    that was actually searched (<=500km), not the raw 5000km input — else
    density_per_km2 is understated by a factor of 100."""
    areas = [(CENTER_LAT, CENTER_LON), (0.0, 0.0)]
    result = overture.compare_areas(areas, radius_m=5_000_000)
    effective_radius_km = geo.MAX_QUERY_RADIUS_M / 1000
    expected_area_km2 = math.pi * effective_radius_km ** 2
    for area in result["areas"]:
        expected_density = round(area["total_places"] / expected_area_km2, 2)
        assert area["density_per_km2"] == expected_density


def test_compare_areas_density_unchanged_for_normal_radius():
    areas = [(CENTER_LAT, CENTER_LON), (0.0, 0.0)]
    result = overture.compare_areas(areas, radius_m=1000)
    expected_area_km2 = math.pi * 1 ** 2
    for area in result["areas"]:
        expected_density = round(area["total_places"] / expected_area_km2, 2)
        assert area["density_per_km2"] == expected_density
