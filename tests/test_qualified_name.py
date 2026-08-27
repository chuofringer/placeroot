"""Comma-qualified LocationRef names (#427).

"Le Marais, Paris" used to reach the shared resolver as one opaque string:
no literal tier matched it, the #215 typo tier then fuzzed across the comma,
and find_near answered a Paris query with three villages named "Le Mauvais
Pas". These tests pin the qualifier being honored — the tier ladder runs on
the head alone, inside the anchor the qualifier names — and pin the two
families that must not move: unqualified names, and the "City, Region"
suffix form geocode() has resolved on its own since #46.

Fixture-level cases use the committed divisions fixture (Hilltop exists both
as an Illinois locality and as a Brooklyn neighborhood — a same-named
division inside and outside an anchor, no fixture extension needed).
Mechanism-level cases patch geocode.geocode, the resolver's only source of
candidates, so a "Le Mauvais Pas" match can be offered and shown to be
unreachable.
"""

import pytest

from placeroot import errors, geocode, server

from ._routing_fixture import build_routing_fixture as fx

BROOKLYN_NBHD_ID = "gers-div-hilltop-nbhd"
ILLINOIS_LOC_ID = "gers-div-zz-hilltop-loc"

FROM_LAT, FROM_LON = fx.node_latlon(2, 2)
TO_LAT, TO_LON = fx.node_latlon(2, 5)


def _row(name, lat, lon, *, id_, type_="locality", context=(), rank=1.0, matched_by=None):
    row = {
        "name": name, "lat": lat, "lon": lon, "id": id_, "type": type_,
        "admin_context": list(context), "rank_score": rank,
    }
    if matched_by:
        row["matched_by"] = matched_by
    return row


def _patch_geocode(monkeypatch, rows_by_query, seen=None):
    """geocode.geocode -> canned rows, recording every query it is asked for."""

    def fake(query, limit=None, lang=None):
        if seen is not None:
            seen.append(query)
        return rows_by_query.get(query, [])

    monkeypatch.setattr(geocode, "geocode", fake)


# --------------------------------------------------------------------
# the qualifier is honored
# --------------------------------------------------------------------


def test_qualifier_picks_the_same_named_division_inside_it():
    # Two divisions named Hilltop: an Illinois locality and a Brooklyn
    # neighborhood. Unqualified, prominence picks Illinois; qualified, the
    # anchor decides — and nothing but the anchor could have.
    assert geocode.resolve_named_place("Hilltop")["id"] == ILLINOIS_LOC_ID
    assert geocode.resolve_named_place("Hilltop, Brooklyn")["id"] == BROOKLYN_NBHD_ID


def test_qualifier_reaches_a_division_the_whole_string_never_found():
    resolved = geocode.resolve_named_place("Riverside, Springfield")
    assert resolved["id"] == "gers-div-riverside"
    assert resolved["admin_context"][-1] == "Springfield"


def test_nothing_inside_a_resolved_qualifier_is_an_honest_not_found():
    with pytest.raises(errors.AnchoredNotFound) as excinfo:
        geocode.resolve_named_place("Nonesuch, Brooklyn")
    assert "Nonesuch" in excinfo.value.detail
    assert "Brooklyn" in excinfo.value.detail


def test_ambiguity_inside_the_qualifier_still_raises_with_candidates(monkeypatch):
    _patch_geocode(monkeypatch, {
        "Brooklyn": [_row("Brooklyn", 40.70, -73.90, id_="anchor")],
        "Elm Park": [
            _row("Elm Park", 40.71, -73.91, id_="a", context=("Brooklyn",)),
            _row("Elm Park", 40.72, -73.92, id_="b", context=("Brooklyn",)),
        ],
    })
    with pytest.raises(errors.AmbiguousPlace) as excinfo:
        geocode.resolve_named_place("Elm Park, Brooklyn")
    assert {c["id"] for c in excinfo.value.candidates} == {"a", "b"}


def test_anchored_place_search_answers_when_no_division_matches(monkeypatch):
    # The "Le Marais, Paris" shape: the neighborhood exists in the places
    # theme, not the divisions one, so the anchored places search is the
    # answer rather than a consolation prize.
    _patch_geocode(monkeypatch, {
        "Paris": [_row("Paris", 48.8566, 2.3522, id_="anchor-paris")],
        "Le Marais": [],
    })
    monkeypatch.setattr(
        geocode, "resolve_place",
        lambda query, near_lat=None, near_lon=None, limit=None, city=None, lang=None: [
            {"id": "p1", "kind": "place", "name": "Le Marais Paris",
             "lat": 48.8598, "lon": 2.3517, "match": "prefix"},
        ],
    )
    resolved = geocode.resolve_named_place("Le Marais, Paris")
    assert resolved == {
        "name": "Le Marais Paris", "lat": 48.8598, "lon": 2.3517,
        "id": "p1", "type": "place",
    }


# --------------------------------------------------------------------
# cross-comma fuzzing, impossible by construction
# --------------------------------------------------------------------


def test_typo_tier_never_fuzzes_across_the_comma(monkeypatch):
    # "Le Mauvais Pas" is offered for the whole string and only for the
    # whole string. The resolver never asks for that string once the
    # qualifier resolves, so no ranking or filtering decision stands
    # between the caller and the wrong answer — it is unreachable.
    seen = []
    _patch_geocode(monkeypatch, {
        "Le Marais, Paris": [
            _row("Le Mauvais Pas", 46.63, 4.03, id_="mp1", matched_by="fuzzy"),
            _row("Le Mauvais Pas", 47.71, 1.27, id_="mp2", matched_by="fuzzy"),
        ],
        "Paris": [_row("Paris", 48.8566, 2.3522, id_="anchor-paris")],
        "Le Marais": [],
    }, seen=seen)
    monkeypatch.setattr(
        geocode, "resolve_place",
        lambda query, near_lat=None, near_lon=None, limit=None, city=None, lang=None: [],
    )
    with pytest.raises(errors.AnchoredNotFound):
        geocode.resolve_named_place("Le Marais, Paris")
    assert "Le Marais, Paris" not in seen
    assert seen == ["Paris", "Le Marais"]


def test_a_division_inside_the_anchor_beats_a_nearer_one_outside_it(monkeypatch):
    # Le Marais the Essonne locality sits 45 km from Paris — inside any
    # city-scale radius, and administratively nowhere near. Answering with
    # it is the same wrong answer in a nearer village, so admin containment
    # decides for any row that carries a chain.
    _patch_geocode(monkeypatch, {
        "Paris": [_row("Paris", 48.8566, 2.3522, id_="anchor-paris")],
        "Le Marais": [
            _row("Le Marais", 48.576, 2.100, id_="essonne", context=("France", "Essonne")),
        ],
    })
    monkeypatch.setattr(
        geocode, "resolve_place",
        lambda query, near_lat=None, near_lon=None, limit=None, city=None, lang=None: [],
    )
    with pytest.raises(errors.AnchoredNotFound):
        geocode.resolve_named_place("Le Marais, Paris")


def test_a_qualifier_only_anchors_on_an_exact_name(monkeypatch):
    # geocode ranks a city above a country, and "France" is a prefix of
    # Franceville: anchoring on it would bound a Paris query on Gabon.
    _patch_geocode(monkeypatch, {
        "Paris, France": [_row("Paris", 48.8566, 2.3522, id_="paris", context=("France",))],
        "France": [_row("Franceville", -1.63, 13.58, id_="franceville")],
    })
    resolved = geocode.resolve_named_place("Paris, France")
    assert resolved["id"] == "paris"
    assert "France" in resolved["note"]


# --------------------------------------------------------------------
# what must not move
# --------------------------------------------------------------------


def test_unqualified_names_are_unchanged():
    assert geocode.resolve_named_place("Hilltop")["id"] == ILLINOIS_LOC_ID
    assert geocode.resolve_named_place("Brooklyn")["id"] == "gers-div-brooklyn"
    assert geocode.resolve_named_place("Nowhere At All") is None
    with pytest.raises(errors.AmbiguousPlace):
        geocode.resolve_named_place("London")


def test_region_suffix_form_stays_on_geocodes_own_path(monkeypatch):
    # "City, Region" is a name plus a region suffix _parse_region_suffix has
    # recognized since #46: the name half is searched alone, constrained to
    # the region, so the comma was never crossed and nothing here applies.
    # Pinned by the anchor lookup never running.
    calls = []
    real_anchor = geocode._resolve_qualifier_anchor
    monkeypatch.setattr(
        geocode, "_resolve_qualifier_anchor",
        lambda text: calls.append(text) or real_anchor(text),
    )
    assert geocode.resolve_named_place("Hilltop, Illinois")["id"] == ILLINOIS_LOC_ID
    assert geocode.resolve_named_place("Fairview, Illinois")["id"] == "gers-div-zz-fairview-il"
    assert calls == []
    # "London, Ontario" is the same form, but recognizing a non-US region
    # needs the local divisions table (#46), which the fixture run has no
    # equivalent of — so this one does take the anchored path here. Same
    # answer either way, which is the point.
    assert geocode.resolve_named_place("London, Ontario")["id"] == "gers-div-london-on"


def test_a_qualifier_that_resolves_to_nothing_degrades_with_a_note(monkeypatch):
    _patch_geocode(monkeypatch, {
        "Elm Park, Atlantis": [_row("Elm Park", 40.71, -73.91, id_="elm")],
        "Atlantis": [],
    })
    resolved = geocode.resolve_named_place("Elm Park, Atlantis")
    assert resolved["id"] == "elm"
    assert resolved["note"] == (
        "'Atlantis' did not resolve as a place or region; searched the full text"
    )


# --------------------------------------------------------------------
# through the shared LocationRef path: find_near, from_to, optimize_route
# --------------------------------------------------------------------


def test_find_near_searches_around_the_qualified_pin():
    result = server.find_near("coffee_shop", "Hilltop, Brooklyn")
    assert "error" not in result
    assert result["near"]["id"] == BROOKLYN_NBHD_ID
    assert result["results"]


def test_find_near_reports_the_qualifier_it_searched():
    result = server.find_near("coffee_shop", "Nonesuch, Brooklyn")
    assert result["error"] == "not_found"
    assert result["field"] == "near"
    assert "Brooklyn" in result["detail"]
    assert result["try"]


def test_from_to_resolves_a_qualified_end(monkeypatch):
    _patch_geocode(monkeypatch, {
        "Brooklyn": [_row("Brooklyn", FROM_LAT, FROM_LON, id_="anchor-brooklyn")],
        "Hilltop": [
            _row("Hilltop", 39.6, -89.2, id_=ILLINOIS_LOC_ID, context=("Illinois",), rank=1.2),
            _row("Hilltop", FROM_LAT, FROM_LON, id_=BROOKLYN_NBHD_ID,
                 type_="neighborhood", context=("Brooklyn",), rank=1.0),
        ],
        "Yoyogi Park": [_row("Yoyogi Park", TO_LAT, TO_LON, id_="gers-b", type_="neighborhood")],
    })
    result = server.from_to("Hilltop, Brooklyn", "Yoyogi Park", mode="walk", confirm=True)
    assert "error" not in result
    assert result["from"]["id"] == BROOKLYN_NBHD_ID
    assert result["to"]["id"] == "gers-b"


def test_optimize_route_resolves_a_qualified_stop(monkeypatch):
    _patch_geocode(monkeypatch, {
        "Brooklyn": [_row("Brooklyn", TO_LAT, TO_LON, id_="anchor-brooklyn")],
        "Hilltop": [
            _row("Hilltop", 39.6, -89.2, id_=ILLINOIS_LOC_ID, context=("Illinois",), rank=1.2),
            _row("Hilltop", TO_LAT, TO_LON, id_=BROOKLYN_NBHD_ID,
                 type_="neighborhood", context=("Brooklyn",), rank=1.0),
        ],
    })
    result = server.optimize_route(
        [{"lat": FROM_LAT, "lon": FROM_LON}, "Hilltop, Brooklyn"], mode="walk",
    )
    assert "error" not in result
    assert result["resolved"] == [
        {
            "stop": 1, "name": "Hilltop", "id": BROOKLYN_NBHD_ID,
            "lat": TO_LAT, "lon": TO_LON, "matched_by": "name",
        },
    ]


def test_a_failed_qualifier_note_reaches_the_locationref_echo(monkeypatch):
    _patch_geocode(monkeypatch, {
        "Hilltop, Atlantis": [_row("Hilltop", TO_LAT, TO_LON, id_=BROOKLYN_NBHD_ID)],
        "Atlantis": [],
    })
    item, err = server._resolve_location_ref("Hilltop, Atlantis")
    assert err is None
    assert "Atlantis" in item["note"]
    assert "Atlantis" in server._location_ref_echo(item)["note"]
