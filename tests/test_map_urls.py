"""Pure parser tests for placeroot.map_urls (#461): no network, no server import.

Every URL family the module claims to read is exercised here, plus the
redirect follower against a scripted fake fetch — the real `_fetch` is never
called.
"""

import urllib.error

import pytest

from placeroot import map_urls

GOOGLE_PLACE = (
    "https://www.google.com/maps/place/Ferry+Building/@37.7955,-122.3937,17z/"
    "data=!3m1!4b1!4m6!3m5!1s0x8085806285ddc389:0x7b5b5e6b2c7a6c5!8m2"
    "!3d37.7955177!4d-122.3937109"
)


def _no_fetch(url, timeout_s):  # pragma: no cover — the assertion is the point
    raise AssertionError(f"network fetch attempted for {url!r}")


# --- normalization -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  https://maps.apple.com/?ll=1,2  ", "https://maps.apple.com/?ll=1,2"),
        ("<https://maps.apple.com/?ll=1,2>", "https://maps.apple.com/?ll=1,2"),
        ("https://maps.apple.com/?ll=1,2.", "https://maps.apple.com/?ll=1,2"),
        ("https://maps.apple.com/?ll=1,2),", "https://maps.apple.com/?ll=1,2"),
        ("maps.app.goo.gl/abc", "https://maps.app.goo.gl/abc"),
        ("geo:1,2?z=3", "geo:1,2?z=3"),
    ],
)
def test_normalize_url_trims_wrappers_and_assumes_https(raw, expected):
    assert map_urls.normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "<>", None, 42, ["x"]])
def test_normalize_url_rejects_empty_and_non_strings(raw):
    assert map_urls.normalize_url(raw) is None


@pytest.mark.parametrize(
    "host,provider",
    [
        ("google.com", "google"),
        ("www.google.com", "google"),
        ("maps.google.com", "google"),
        ("google.co.uk", "google"),
        ("www.google.de", "google"),
        ("google.com.au", "google"),
        ("maps.app.goo.gl", "google"),
        ("maps.apple.com", "apple"),
        ("openstreetmap.org", "osm"),
        ("www.openstreetmap.org", "osm"),
        ("osm.org", "osm"),
        ("example.com", None),
        ("notgoogle.com", None),
        ("google.evil.example", None),
    ],
)
def test_provider_for_hosts(host, provider):
    assert map_urls.provider_for(f"https://{host}/maps") == provider


@pytest.mark.parametrize(
    "url,short",
    [
        ("https://maps.app.goo.gl/abc", True),
        ("https://g.co/kgs/abc", True),
        ("https://goo.gl/maps/abc", True),
        ("https://goo.gl/other", False),
        ("https://www.google.com/maps/@1,2,3z", False),
        ("https://maps.apple.com/?ll=1,2", False),
    ],
)
def test_is_short_link(url, short):
    assert map_urls.is_short_link(url) is short


# --- Google --------------------------------------------------------------------


def test_google_place_url_with_at_viewport():
    res = map_urls.parse_map_url(
        "https://www.google.com/maps/place/Ferry+Building/@37.7955,-122.3937,17z"
    )
    assert res == {
        "lat": 37.7955,
        "lon": -122.3937,
        "zoom": 17,
        "label": "Ferry Building",
        "provider": "google",
    }


def test_google_pin_beats_viewport_centre():
    res = map_urls.parse_map_url(GOOGLE_PLACE)
    assert (res["lat"], res["lon"]) == (37.7955177, -122.3937109)
    assert res["zoom"] == 17
    assert res["label"] == "Ferry Building"


def test_google_bare_viewport_and_meters_is_not_a_zoom():
    res = map_urls.parse_map_url("https://www.google.com/maps/@37.79,-122.40,1234m/data=!3m1!1e3")
    assert res == {"lat": 37.79, "lon": -122.4, "provider": "google"}
    assert "zoom" not in res


def test_google_fractional_zoom_is_kept_as_float():
    res = map_urls.parse_map_url("https://www.google.com/maps/@37.79,-122.40,15.5z")
    assert res["zoom"] == 15.5


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/maps?q=48.8584,2.2945",
        "https://google.com/maps?ll=48.8584,2.2945",
        "https://www.google.co.uk/maps?q=48.8584,2.2945",
        "https://maps.google.com/?q=48.8584,2.2945",
        "https://www.google.com/maps/search/?api=1&query=48.8584,2.2945",
        "https://www.google.com/maps?q=loc:48.8584,2.2945",
    ],
)
def test_google_query_coordinates(url):
    res = map_urls.parse_map_url(url)
    assert (res["lat"], res["lon"], res["provider"]) == (48.8584, 2.2945, "google")


def test_google_z_param_is_zoom():
    res = map_urls.parse_map_url("https://www.google.co.uk/maps?ll=51.5,-0.12&z=14")
    assert res["zoom"] == 14


def test_google_q_text_is_a_label_without_coordinates():
    res = map_urls.parse_map_url("google.com/maps?q=Ferry+Building")
    assert res["error"] == "no_location"
    assert res["label"] == "Ferry Building"
    assert res["provider"] == "google"


def test_google_search_path_carries_label_and_viewport():
    res = map_urls.parse_map_url(
        "https://www.google.com/maps/search/Ferry+Building/@37.79,-122.39,15z"
    )
    assert res == {
        "lat": 37.79,
        "lon": -122.39,
        "zoom": 15,
        "label": "Ferry Building",
        "provider": "google",
    }


def test_google_dir_path_is_a_route_viewport_with_no_label():
    res = map_urls.parse_map_url(
        "https://www.google.com/maps/dir/San+Francisco/Oakland/@37.79,-122.3,12z"
    )
    assert res == {
        "lat": 37.79,
        "lon": -122.3,
        "zoom": 12,
        "note": "route viewport centre",
        "provider": "google",
    }
    assert "label" not in res


def test_google_cid_is_not_resolvable():
    res = map_urls.parse_map_url("https://maps.google.com/?cid=12345")
    assert res["error"] == "no_location"
    assert "cid" in res["detail"]
    assert "label" not in res


def test_google_label_is_url_decoded():
    res = map_urls.parse_map_url(
        "https://www.google.com/maps/place/Caf%C3%A9+de+Flore/@48.854,2.3325,17z"
    )
    assert res["label"] == "Café de Flore"


# --- Apple ---------------------------------------------------------------------


def test_apple_ll_with_name_and_zoom():
    res = map_urls.parse_map_url("https://maps.apple.com/?ll=37.33,-122.03&q=Apple+Park&z=16")
    assert res == {
        "lat": 37.33,
        "lon": -122.03,
        "zoom": 16,
        "label": "Apple Park",
        "provider": "apple",
    }


def test_apple_q_text_only_is_a_label():
    res = map_urls.parse_map_url("https://maps.apple.com/?q=Ferry+Building")
    assert res["error"] == "no_location"
    assert res["label"] == "Ferry Building"
    assert res["provider"] == "apple"


def test_apple_address_is_a_label():
    res = map_urls.parse_map_url("https://maps.apple.com/?address=1+Infinite+Loop,+Cupertino")
    assert res["error"] == "no_location"
    assert res["label"] == "1 Infinite Loop, Cupertino"


def test_apple_sll_is_a_coordinate_fallback():
    res = map_urls.parse_map_url("https://maps.apple.com/?sll=37.33,-122.03&q=cafe")
    assert (res["lat"], res["lon"], res["label"]) == (37.33, -122.03, "cafe")


def test_apple_place_coordinate_form():
    res = map_urls.parse_map_url(
        "https://maps.apple.com/place?coordinate=37.33,-122.03&name=Apple+Park"
    )
    assert (res["lat"], res["lon"], res["label"]) == (37.33, -122.03, "Apple Park")


# --- OpenStreetMap -------------------------------------------------------------


def test_osm_fragment_map():
    res = map_urls.parse_map_url("https://www.openstreetmap.org/#map=17/51.5/-0.12")
    assert res == {"lat": 51.5, "lon": -0.12, "zoom": 17, "provider": "osm"}


def test_osm_marker_beats_fragment_map():
    res = map_urls.parse_map_url(
        "https://www.openstreetmap.org/?mlat=51.51&mlon=-0.13#map=17/51.5/-0.12"
    )
    assert (res["lat"], res["lon"], res["zoom"]) == (51.51, -0.13, 17)


def test_osm_org_short_host():
    res = map_urls.parse_map_url("https://osm.org/#map=12/40.7/-74.0")
    assert (res["lat"], res["lon"], res["provider"]) == (40.7, -74.0, "osm")


def test_osm_element_without_coordinates_is_no_location():
    res = map_urls.parse_map_url("https://www.openstreetmap.org/node/123")
    assert res["error"] == "no_location"
    assert "node/123" in res["detail"]


# --- geo: ------------------------------------------------------------------------


def test_geo_uri_with_zoom():
    assert map_urls.parse_map_url("geo:37.7,-122.4?z=15") == {
        "lat": 37.7,
        "lon": -122.4,
        "zoom": 15,
        "provider": "geo",
    }


def test_geo_uri_without_zoom_and_with_altitude():
    assert map_urls.parse_map_url("geo:37.7,-122.4,10") == {
        "lat": 37.7,
        "lon": -122.4,
        "provider": "geo",
    }


def test_geo_uri_malformed():
    assert map_urls.parse_map_url("geo:somewhere")["error"] == "no_location"


# --- validation and errors -------------------------------------------------------


def test_out_of_range_latitude_is_no_location():
    res = map_urls.parse_map_url("https://www.google.com/maps?q=95,2.2945")
    assert res["error"] == "no_location"
    assert "out of range" in res["detail"]


def test_out_of_range_longitude_is_no_location():
    res = map_urls.parse_map_url("https://www.openstreetmap.org/#map=5/10/181")
    assert res["error"] == "no_location"


def test_unsupported_host():
    res = map_urls.parse_map_url("https://example.com/")
    assert res["error"] == "unsupported_url"
    assert res["supported"] == ["google", "apple", "osm", "geo"]


@pytest.mark.parametrize("raw", ["", None, 12])
def test_bad_request(raw):
    assert map_urls.parse_map_url(raw)["error"] == "bad_request"


def test_wrapped_and_punctuated_link_is_parsed():
    res = map_urls.parse_map_url("<https://www.google.com/maps?q=48.8584,2.2945>.")
    assert (res["lat"], res["lon"]) == (48.8584, 2.2945)


# --- redirects ---------------------------------------------------------------------


def _scripted(chain):
    """fetch fake: pops (status, location) per call, recording the URLs asked."""
    calls: list[str] = []
    steps = list(chain)

    def fetch(url, timeout_s):
        calls.append(url)
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    fetch.calls = calls
    return fetch


def test_follow_redirects_stops_when_the_chain_leaves_the_short_host():
    fetch = _scripted([(302, GOOGLE_PLACE)])
    res = map_urls.follow_redirects("https://maps.app.goo.gl/abc", fetch=fetch)
    assert res == {"final_url": GOOGLE_PLACE, "hops": 1}
    assert fetch.calls == ["https://maps.app.goo.gl/abc"]


def test_follow_redirects_resolves_relative_locations_and_multi_hop():
    fetch = _scripted([(301, "/maps/xyz"), (302, GOOGLE_PLACE)])
    res = map_urls.follow_redirects("https://goo.gl/maps/abc", fetch=fetch)
    assert res["final_url"] == GOOGLE_PLACE
    assert fetch.calls == ["https://goo.gl/maps/abc", "https://goo.gl/maps/xyz"]


def test_follow_redirects_hop_cap():
    fetch = _scripted([(302, "https://maps.app.goo.gl/again")] * 10)
    res = map_urls.follow_redirects("https://maps.app.goo.gl/abc", max_hops=3, fetch=fetch)
    assert res["error"] == "redirect_failed"
    assert res["detail"] == "too many redirects"
    assert len(fetch.calls) == 3


def test_follow_redirects_urlerror():
    fetch = _scripted([urllib.error.URLError("timed out")])
    res = map_urls.follow_redirects("https://maps.app.goo.gl/abc", fetch=fetch)
    assert res["error"] == "redirect_failed"
    assert "timed out" in res["detail"]
    assert "status" not in res


def test_follow_redirects_httperror_carries_status():
    err = urllib.error.HTTPError("https://maps.app.goo.gl/abc", 404, "Not Found", {}, None)
    fetch = _scripted([err])
    res = map_urls.follow_redirects("https://maps.app.goo.gl/abc", fetch=fetch)
    assert res["error"] == "redirect_failed"
    assert res["status"] == 404


def test_follow_redirects_non_redirect_status_from_fetch():
    fetch = _scripted([(500, None)])
    res = map_urls.follow_redirects("https://maps.app.goo.gl/abc", fetch=fetch)
    assert res == {
        "error": "redirect_failed",
        "detail": "HTTP 500 while expanding the short link",
        "status": 500,
    }


def test_follow_redirects_never_fetches_a_non_short_host():
    res = map_urls.follow_redirects(GOOGLE_PLACE, fetch=_no_fetch)
    assert res == {"final_url": GOOGLE_PLACE, "hops": 0}


# --- resolve orchestration -------------------------------------------------------


def test_resolve_marks_direct_parse_as_url():
    res = map_urls.resolve(GOOGLE_PLACE, fetch=_no_fetch)
    assert res["resolved_via"] == "url"
    assert "final_url" not in res


def test_resolve_follows_short_link_and_reports_final_url():
    fetch = _scripted([(302, GOOGLE_PLACE)])
    res = map_urls.resolve("maps.app.goo.gl/abc", fetch=fetch)
    assert res["resolved_via"] == "redirect"
    assert res["final_url"] == GOOGLE_PLACE
    assert (res["lat"], res["lon"]) == (37.7955177, -122.3937109)


def test_resolve_short_link_failure_is_structured():
    fetch = _scripted([urllib.error.URLError("no route to host")])
    res = map_urls.resolve("https://maps.app.goo.gl/abc", fetch=fetch)
    assert res["error"] == "redirect_failed"
    assert res["provider"] == "google"


def test_resolve_short_link_landing_on_a_name_only_page_keeps_the_label():
    fetch = _scripted([(302, "https://www.google.com/maps?q=Ferry+Building")])
    res = map_urls.resolve("https://maps.app.goo.gl/abc", fetch=fetch)
    assert res["error"] == "no_location"
    assert res["label"] == "Ferry Building"
    assert res["final_url"] == "https://www.google.com/maps?q=Ferry+Building"


@pytest.mark.parametrize(
    "url",
    [
        "https://maps.apple.com/?ll=1,2",
        "https://www.openstreetmap.org/#map=5/1/2",
        "geo:1,2",
        "https://example.com/",
    ],
)
def test_resolve_never_fetches_for_non_short_hosts(url):
    map_urls.resolve(url, fetch=_no_fetch)


def test_default_fetch_refuses_to_follow_redirects_itself():
    """The opener's redirect handler must hand 3xx back, not chase it."""
    handler = map_urls._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://x") is None
