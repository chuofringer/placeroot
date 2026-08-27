"""resolve_named_place's places leg (issue #429).

The shared LocationRef resolver had no place search of its own: it read
geocode(), whose places half is a supplement to the divisions search and is
skipped outright when the query yields no anchor to bound it by. So a name
the places theme carries — "Louvre Museum" — resolved to nothing, and every
compose built on the resolver (from_to's ends, meeting_point's origins,
optimize_route's stops, find_near's near) answered "no place matched".

The tests below pin the precondition (geocode alone is blind here), the
invariant the fix establishes (what resolve_place resolves as a place, the
named-place resolver resolves too), and the match grading that stops a
landmark's other spelling from losing the tier sort to a business whose name
merely starts with the query word.

_skip_unanchored_places_scan is monkeypatched on where the offline fixtures
would otherwise hide the defect: it is True against a remote dataset (every
production install) and False against a local one (the fixtures), so the
fixtures alone would run the very scan production skips.
"""

import pytest

from placeroot import geocode, overture, server

# A fixture place with an ordinary business name and no location word in it —
# the shape that derives no anchor and so never reached the places theme.
PLACE_NAME = "Blue Bottle Roastery"
PLACE_LAT, PLACE_LON = 40.698378, -73.900621

# Somewhere routable-looking near the fixture cluster, for the compose calls
# that need a second point. The routing fixture's street graph is elsewhere,
# so these calls cannot produce a route — what they can show is that they no
# longer fail at resolution, which is the defect.
NEAR_PLACE = {"lat": 40.7038, "lon": -73.8950}


@pytest.fixture
def remote_dataset(monkeypatch):
    """Places-half gating as production sees it (see the module docstring)."""
    monkeypatch.setattr(geocode, "_skip_unanchored_places_scan", lambda: True)


@pytest.fixture
def session_city(remote_dataset):
    """A session that has already resolved a place in Brooklyn.

    #329 remembers the last good city and reuses it as the implicit hint for
    the next POI-shaped resolve — which is what a compose call following a
    city query looks like, and the session shape the defect was reported
    from. The hint bounds where the search looks; the row still comes from
    the data.
    """
    geocode.clear_resolve_session()
    assert geocode.resolve_place(PLACE_NAME, city="Brooklyn")


def test_geocode_alone_cannot_see_the_place(remote_dataset):
    """The precondition: this name derives no anchor, so geocode's places
    half never runs and the whole search comes back empty."""
    assert geocode.geocode(PLACE_NAME) == []


def test_exact_place_name_resolves_through_the_named_place_resolver(session_city):
    resolved = geocode.resolve_named_place(PLACE_NAME)
    assert resolved is not None
    assert resolved["name"] == PLACE_NAME
    assert resolved["type"] == "place"
    assert (resolved["lat"], resolved["lon"]) == (PLACE_LAT, PLACE_LON)


def test_shared_location_ref_resolver_agrees(session_city):
    """server._resolve_named_place is the one path every compose tool's
    free-text end goes through."""
    assert server._resolve_named_place(PLACE_NAME)["name"] == PLACE_NAME


def test_from_to_end_resolves(session_city):
    # The reported shape: an unresolvable end fails at resolution and names
    # the field. This one gets past it (the fixtures' street graph is
    # elsewhere, so the walk itself has nothing to route over).
    missed = server.from_to(from_="Nowhere At All", to=NEAR_PLACE, confirm=True)
    assert (missed["error"], missed["field"]) == ("not_found", "from")
    result = server.from_to(from_=PLACE_NAME, to=NEAR_PLACE, confirm=True)
    assert result.get("error") != "not_found"


def test_meeting_point_origin_resolves(session_city):
    result = server.meeting_point(origins=[PLACE_NAME, "Corner Test Place"], confirm=True)
    assert result.get("error") != "not_found"
    assert [r["name"] for r in result["resolved"]] == [PLACE_NAME, "Corner Test Place"]


def test_optimize_route_stop_resolves(session_city):
    result = server.optimize_route(
        stops=[PLACE_NAME, "Corner Test Place", "Edge Test Place"],
        mode="walk",
    )
    assert result.get("error") != "not_found"


def test_a_division_answer_is_left_alone(remote_dataset):
    """The places leg is a fallback, not a second opinion: a query geocode
    already answers with a division is never re-litigated."""
    assert geocode.resolve_named_place("Brooklyn")["id"] == "gers-div-brooklyn"


# --- match grading: a landmark's other spellings are readings of the query --


def test_best_place_label_takes_the_strongest_reading():
    alternates = ["musee du louvre"]
    # The literal reading is a bare substring; the alias spelling is what the
    # name actually is. Taking the first non-None label kept the substring.
    assert geocode._best_place_label("Musée du Louvre", "Louvre", alternates) == "exact"
    # An unrelated name earns nothing from either reading.
    assert geocode._best_place_label("Gare du Nord", "Louvre", alternates) is None


def test_alias_spelling_outranks_a_bare_prefix_match(monkeypatch):
    """The #429 secondary defect, on fixture data: "Louvre" ranked "Louvre
    Luxury Apartment & SPA" above the Musée du Louvre because prefix beats
    substring and the museum's name only *contains* the word."""
    pin = {"city": "Brooklyn", "lat": 40.700, "lon": -73.900}
    monkeypatch.setattr(geocode, "_POI_ALIASES", {"cluster": pin, "cluster place 060": pin})
    geocode.clear_resolve_session()
    top = geocode.resolve_place("Cluster", limit=4)[0]
    assert top["name"] == "Cluster Place 060"
    assert top["match"] == "exact"


def test_same_tier_candidates_order_by_prominence():
    """Within one match tier and one rounded kilometre, confidence decides."""
    hits = geocode.resolve_place("Cluster Place", near_lat=40.70, near_lon=-73.90, limit=6)
    assert {h["match"] for h in hits} == {"prefix"}
    confidences = [overture.place_details(id=h["id"])["confidence"] for h in hits]
    assert confidences == sorted(confidences, reverse=True)
