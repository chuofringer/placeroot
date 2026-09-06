"""Tool-level tests for resolve_map_url (#461).

The parser is covered in test_map_urls.py; here the wrapper's seams are
faked — server.reverse_geocode, server.resolve_place and the short-link
fetch — so nothing touches DuckDB, S3 or the network.
"""

import asyncio

import pytest

from placeroot import map_urls, output_schemas, server, tool_profiles

GOOGLE_PLACE = (
    "https://www.google.com/maps/place/Ferry+Building/@37.7955,-122.3937,17z/"
    "data=!3m1!4b1!4m6!3m5!1s0x8085806285ddc389:0x7b5b5e6b2c7a6c5!8m2"
    "!3d37.7955177!4d-122.3937109"
)
PLACE_ROW = {
    "address": {"street": "The Embarcadero", "number": "1"},
    "admin_context": ["San Francisco", "California", "United States"],
    "country": "US",
}


@pytest.fixture
def fake_reverse(monkeypatch):
    calls: list[tuple[float, float]] = []

    def reverse_geocode(lat, lon):
        calls.append((lat, lon))
        return dict(PLACE_ROW)

    monkeypatch.setattr(server, "reverse_geocode", reverse_geocode)
    return calls


@pytest.fixture
def no_network(monkeypatch):
    def fetch(url, timeout_s):  # pragma: no cover — the assertion is the point
        raise AssertionError(f"network fetch attempted for {url!r}")

    monkeypatch.setattr(map_urls, "_fetch", fetch)


@pytest.fixture
def fake_resolve_place(monkeypatch):
    calls: list[tuple] = []
    answer: dict = {
        "results": [
            {
                "id": "08f2830828c1c8ff0399e2db3a2b3c4d",
                "kind": "place",
                "name": "Ferry Building",
                "lat": 37.7955,
                "lon": -122.3937,
            }
        ]
    }

    def resolve_place(query, *args, **kwargs):
        calls.append((query, args, kwargs))
        return answer

    monkeypatch.setattr(server, "resolve_place", resolve_place)
    resolve_place.calls = calls
    resolve_place.answer = answer
    return resolve_place


# --- direct parse + place ----------------------------------------------------------


def test_google_place_link_with_place(fake_reverse, no_network):
    res = server.resolve_map_url(GOOGLE_PLACE)
    assert res["lat"] == 37.7955177
    assert res["lon"] == -122.3937109
    assert res["zoom"] == 17
    assert res["label"] == "Ferry Building"
    assert res["provider"] == "google"
    assert res["resolved_via"] == "url"
    assert "final_url" not in res
    assert res["place"] == PLACE_ROW
    assert fake_reverse == [(37.7955177, -122.3937109)]


def test_include_place_false_never_reverse_geocodes(fake_reverse, no_network):
    res = server.resolve_map_url(GOOGLE_PLACE, include_place=False)
    assert "place" not in res
    assert fake_reverse == []


def test_reverse_geocode_error_is_embedded_not_raised(monkeypatch, no_network):
    monkeypatch.setattr(
        server,
        "reverse_geocode",
        lambda lat, lon: {"error": "upstream_unavailable", "detail": "s3 down"},
    )
    res = server.resolve_map_url("geo:37.7,-122.4?z=15")
    assert (res["lat"], res["lon"], res["zoom"]) == (37.7, -122.4, 15)
    assert res["place"]["error"] == "upstream_unavailable"


def test_reverse_geocode_exception_is_embedded_not_raised(monkeypatch, no_network):
    def boom(lat, lon):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(server, "reverse_geocode", boom)
    res = server.resolve_map_url("https://maps.apple.com/?ll=37.33,-122.03")
    assert res["place"]["error"] == "lookup_failed"
    assert "kaboom" in res["place"]["detail"]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.google.com/maps?q=48.8584,2.2945", (48.8584, 2.2945, "google")),
        ("https://www.google.co.uk/maps?ll=48.8584,2.2945", (48.8584, 2.2945, "google")),
        ("https://maps.apple.com/?ll=37.33,-122.03&q=Apple+Park", (37.33, -122.03, "apple")),
        ("https://www.openstreetmap.org/#map=17/51.5/-0.12", (51.5, -0.12, "osm")),
        (
            "https://www.openstreetmap.org/?mlat=51.51&mlon=-0.13#map=17/51.5/-0.12",
            (51.51, -0.13, "osm"),
        ),
        ("geo:37.7,-122.4?z=15", (37.7, -122.4, "geo")),
    ],
)
def test_every_provider_family_resolves(url, expected, fake_reverse, no_network):
    res = server.resolve_map_url(url, include_place=False)
    assert (res["lat"], res["lon"], res["provider"]) == expected
    assert res["resolved_via"] == "url"


def test_dir_link_has_note_and_no_label(fake_reverse, no_network):
    res = server.resolve_map_url(
        "https://www.google.com/maps/dir/San+Francisco/Oakland/@37.79,-122.3,12z",
        include_place=False,
    )
    assert res["note"] == "route viewport centre"
    assert "label" not in res


# --- name path ---------------------------------------------------------------------


def test_name_only_link_resolves_through_resolve_place(
    fake_resolve_place, fake_reverse, no_network
):
    res = server.resolve_map_url("google.com/maps?q=Ferry+Building")
    assert res["resolved_via"] == "name"
    assert res["label"] == "Ferry Building"
    assert (res["lat"], res["lon"]) == (37.7955, -122.3937)
    assert res["match"] == {"name": "Ferry Building", "id": "08f2830828c1c8ff0399e2db3a2b3c4d"}
    assert res["provider"] == "google"
    assert res["place"] == PLACE_ROW
    assert len(fake_resolve_place.calls) == 1
    query, _args, kwargs = fake_resolve_place.calls[0]
    assert query == "Ferry Building"
    assert kwargs.get("limit") == 1


def test_apple_address_link_takes_the_name_path(fake_resolve_place, fake_reverse, no_network):
    res = server.resolve_map_url("https://maps.apple.com/?address=1+Infinite+Loop,+Cupertino")
    assert res["resolved_via"] == "name"
    assert res["label"] == "1 Infinite Loop, Cupertino"
    assert fake_resolve_place.calls[0][0] == "1 Infinite Loop, Cupertino"


def test_name_path_no_hits_is_no_location_with_label(fake_resolve_place, fake_reverse, no_network):
    fake_resolve_place.answer.clear()
    fake_resolve_place.answer["results"] = []
    res = server.resolve_map_url("google.com/maps?q=Ferry+Building")
    assert res["error"] == "no_location"
    assert res["label"] == "Ferry Building"
    assert fake_reverse == []


def test_name_path_error_is_no_location_with_label(fake_resolve_place, fake_reverse, no_network):
    fake_resolve_place.answer.clear()
    fake_resolve_place.answer.update({"error": "upstream_unavailable", "detail": "s3 down"})
    res = server.resolve_map_url("google.com/maps?q=Ferry+Building")
    assert res["error"] == "no_location"
    assert res["label"] == "Ferry Building"
    assert res["lookup_error"] == "upstream_unavailable"


def test_cid_link_does_not_take_the_name_path(fake_resolve_place, fake_reverse, no_network):
    res = server.resolve_map_url("https://maps.google.com/?cid=12345")
    assert res["error"] == "no_location"
    assert fake_resolve_place.calls == []


# --- short links -------------------------------------------------------------------


def test_short_link_is_followed_and_reports_final_url(monkeypatch, fake_reverse):
    calls: list[str] = []

    def fetch(url, timeout_s):
        calls.append(url)
        return 302, GOOGLE_PLACE

    monkeypatch.setattr(map_urls, "_fetch", fetch)
    res = server.resolve_map_url("https://maps.app.goo.gl/abc123")
    assert res["resolved_via"] == "redirect"
    assert res["final_url"] == GOOGLE_PLACE
    assert (res["lat"], res["lon"]) == (37.7955177, -122.3937109)
    assert res["label"] == "Ferry Building"
    assert res["place"] == PLACE_ROW
    assert calls == ["https://maps.app.goo.gl/abc123"]


def test_short_link_hop_cap_is_redirect_failed(monkeypatch, fake_reverse):
    monkeypatch.setattr(map_urls, "_fetch", lambda url, t: (302, "https://maps.app.goo.gl/loop"))
    res = server.resolve_map_url("https://maps.app.goo.gl/abc123")
    assert res["error"] == "redirect_failed"
    assert res["detail"] == "too many redirects"
    assert fake_reverse == []


def test_short_link_landing_on_a_name_only_page_uses_the_name_path(
    monkeypatch, fake_resolve_place, fake_reverse
):
    monkeypatch.setattr(
        map_urls, "_fetch", lambda url, t: (302, "https://www.google.com/maps?q=Ferry+Building")
    )
    res = server.resolve_map_url("https://maps.app.goo.gl/abc123")
    assert res["resolved_via"] == "name"
    assert res["final_url"] == "https://www.google.com/maps?q=Ferry+Building"


# --- errors --------------------------------------------------------------------------


def test_invalid_coordinates(fake_reverse, no_network):
    res = server.resolve_map_url("https://www.google.com/maps?q=95,2.2945")
    assert res["error"] == "no_location"
    assert fake_reverse == []


def test_unsupported_url(fake_reverse, no_network):
    res = server.resolve_map_url("https://example.com/")
    assert res["error"] == "unsupported_url"
    assert res["supported"] == ["google", "apple", "osm", "geo"]


@pytest.mark.parametrize("raw", ["", None])
def test_bad_request(raw, fake_reverse, no_network):
    assert server.resolve_map_url(raw)["error"] == "bad_request"


def test_wrapped_link_with_trailing_period(fake_reverse, no_network):
    res = server.resolve_map_url(
        "<https://www.google.com/maps?q=48.8584,2.2945>.", include_place=False
    )
    assert (res["lat"], res["lon"]) == (48.8584, 2.2945)


# --- registration --------------------------------------------------------------------


def test_tool_is_listed_with_url_required_and_include_place_optional():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    tool = tools["resolve_map_url"]
    schema = tool.input_schema
    assert schema["required"] == ["url"]
    assert schema["properties"]["url"]["type"] == "string"
    assert schema["properties"]["include_place"]["type"] == "boolean"
    assert schema["properties"]["include_place"]["default"] is True
    assert tool.title == "Resolve a pasted map link"
    assert tool.annotations.read_only_hint is True


def test_registered_in_search_and_core_profiles():
    assert "resolve_map_url" in tool_profiles.PROFILES["search"]
    assert "resolve_map_url" in tool_profiles.PROFILES["core"]


def test_has_an_output_schema():
    assert "resolve_map_url" in output_schemas.OUTPUT_SCHEMAS
