"""Pocket handoff export on route / optimize_route — issue #312.

Pure string formatting: Google/Apple Maps deep links, GPX 1.1, and a
printable stop list. No network, no Maps API, no keys. Formatting helpers
are tested without the street graph; the tool-boundary cases go through
server.route / server.optimize_route on the committed transportation
fixture so a real result grows an `export` object.
"""

import xml.etree.ElementTree as ET

from placeroot import budget, export, server

from ._routing_fixture import build_routing_fixture as fx

GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}

FROM_LAT, FROM_LON = fx.node_latlon(2, 2)
TO_LAT, TO_LON = fx.node_latlon(2, 5)

# Same shuffled collinear four as test_optimize_route — unique open path
# 1 -> 3 -> 0 -> 2 when starting at input index 1.
LINE_NODES = [(2, 8), (2, 2), (2, 11), (2, 5)]
LINE_STOPS = [fx.node_latlon(*node) for node in LINE_NODES]
LINE_START_INDEX = 1
LINE_EXPECTED_ORDER = [1, 3, 0, 2]


def _url_ll(lat, lon):
    """Maps-URL coordinate pair: same 6 dp fixed-point as export._ll."""
    return f"{lat:.6f},{lon:.6f}"


def _as_dicts(stops, names=None):
    rows = [{"lat": lat, "lon": lon} for lat, lon in stops]
    if names is not None:
        for row, name in zip(rows, names):
            row["name"] = name
    return rows


def _gpx_root(document):
    return ET.fromstring(document)


def _gpx_findall(root, tag):
    return root.findall(f".//gpx:{tag}", GPX_NS)


# --- formatting, no graph ------------------------------------------------


def test_two_point_google_link_contains_both_coords_and_travelmode():
    payload = export.build(
        ((37.7749, -122.4194, "Start"), (37.8049, -122.2711, "End")),
        mode="walk",
    )
    google = payload["maps_link"]["google"]
    assert google.startswith("https://www.google.com/maps/dir/?")
    assert "api=1" in google
    assert f"origin={_url_ll(37.7749, -122.4194)}" in google
    assert f"destination={_url_ll(37.8049, -122.2711)}" in google
    assert "travelmode=walking" in google
    assert "waypoints=" not in google


def test_apple_two_point_link_is_saddr_daddr():
    payload = export.build(
        ((37.7749, -122.4194, None), (37.8049, -122.2711, None)),
        mode="drive",
    )
    apple = payload["maps_link"]["apple"]
    assert apple.startswith("https://maps.apple.com/?")
    assert f"saddr={_url_ll(37.7749, -122.4194)}" in apple
    assert f"daddr={_url_ll(37.8049, -122.2711)}" in apple
    assert "dirflg=d" in apple
    # Documented two-stop scheme: no legacy +to: chain, no raw +.
    assert "+to:" not in apple
    assert "+" not in apple


def test_optimize_multi_stop_google_link_lists_every_waypoint():
    stops = (
        (40.70, -73.90, "A"),
        (40.71, -73.91, "B"),
        (40.72, -73.92, "C"),
        (40.73, -73.93, "D"),
    )
    payload = export.build(stops, mode="cycle", roundtrip=False)
    google = payload["maps_link"]["google"]
    assert f"origin={_url_ll(40.70, -73.90)}" in google
    assert f"destination={_url_ll(40.73, -73.93)}" in google
    assert "waypoints=" in google
    assert _url_ll(40.71, -73.91) in google
    assert _url_ll(40.72, -73.92) in google
    assert "travelmode=bicycling" in google
    # Cycle has no Apple dirflg.
    apple = payload["maps_link"]["apple"]
    assert "dirflg=" not in apple
    assert "+to:" in apple
    daddr = f"{_url_ll(40.71, -73.91)}+to:{_url_ll(40.72, -73.92)}+to:{_url_ll(40.73, -73.93)}"
    assert f"daddr={daddr}" in apple


def test_roundtrip_maps_destination_is_the_start():
    stops = (
        (1.0, 2.0, "Home"),
        (3.0, 4.0, "Shop"),
        (5.0, 6.0, "Park"),
    )
    payload = export.build(stops, mode="drive", roundtrip=True)
    google = payload["maps_link"]["google"]
    assert f"origin={_url_ll(1.0, 2.0)}" in google
    assert f"destination={_url_ll(1.0, 2.0)}" in google
    assert _url_ll(3.0, 4.0) in google
    assert _url_ll(5.0, 6.0) in google
    apple = payload["maps_link"]["apple"]
    assert apple.startswith(f"https://maps.apple.com/?saddr={_url_ll(1.0, 2.0)}")
    closed = apple.rstrip("&dirflg=d")
    assert closed.endswith(_url_ll(1.0, 2.0)) or f"to:{_url_ll(1.0, 2.0)}" in apple


def test_gpx_is_well_formed_with_rtept_and_trkpt():
    payload = export.build(
        ((10.0, 20.0, "Alpha"), (11.0, 21.0, "Beta")),
        mode="walk",
    )
    root = _gpx_root(payload["gpx"])
    assert root.tag == "{http://www.topografix.com/GPX/1/1}gpx"
    assert root.attrib["version"] == "1.1"
    assert root.attrib["creator"] == "placeroot"
    rtepts = _gpx_findall(root, "rtept")
    trkpts = _gpx_findall(root, "trkpt")
    wpts = _gpx_findall(root, "wpt")
    assert len(rtepts) == 2
    assert len(trkpts) == 2
    assert len(wpts) == 2
    assert {pt.attrib["lat"] for pt in rtepts} == {"10.0000000", "11.0000000"}
    assert {pt.attrib["lon"] for pt in rtepts} == {"20.0000000", "21.0000000"}


def test_gpx_uses_path_geometry_as_track_when_present():
    payload = export.build(
        ((0.0, 0.0, "A"), (1.0, 1.0, "B")),
        mode="walk",
        path=((0.0, 0.0), (0.5, 0.5), (1.0, 1.0)),
    )
    root = _gpx_root(payload["gpx"])
    trkpts = _gpx_findall(root, "trkpt")
    assert len(trkpts) == 3
    assert trkpts[1].attrib["lat"] == "0.5000000"
    # Stops still become waypoints / route points even with a track.
    assert len(_gpx_findall(root, "wpt")) == 2
    assert len(_gpx_findall(root, "rtept")) == 2


def test_gpx_escapes_stop_names():
    payload = export.build(
        ((0.0, 0.0, "Cafe & Bar <main>"), (1.0, 1.0, 'Say "hi"')),
        mode="walk",
    )
    gpx = payload["gpx"]
    assert "Cafe &amp; Bar &lt;main&gt;" in gpx
    assert "&lt;" in gpx
    ET.fromstring(gpx)  # still well-formed after escaping


def test_text_list_has_every_stop_name_coord_and_leg_time():
    payload = export.build(
        ((37.1, -122.1, "Bakery"), (37.2, -122.2, "Library"), (37.3, -122.3, "Park")),
        mode="walk",
        legs=(
            {"distance_m": 300.0, "duration_s": 214.0},
            {"distance_m": 450.0, "duration_s": 321.0},
        ),
        total_distance_m=750.0,
        total_duration_s=535.0,
    )
    text = payload["text"]
    assert "Bakery" in text
    assert "Library" in text
    assert "Park" in text
    assert "37.100000" in text
    assert "-122.100000" in text
    assert "37.200000" in text
    assert "37.300000" in text
    assert "300 m" in text
    assert "3 min 34 s" in text
    assert "450 m" in text
    assert "5 min 21 s" in text
    assert "Total:" in text


def test_text_roundtrip_repeats_the_start():
    payload = export.build(
        ((1.5, 2.5, "Home"), (3.5, 4.5, "Shop")),
        mode="drive",
        roundtrip=True,
        legs=(
            {"distance_m": 100.0, "duration_s": 12.0},
            {"distance_m": 100.0, "duration_s": 12.0},
        ),
        total_distance_m=200.0,
        total_duration_s=24.0,
    )
    assert "Home (return)" in payload["text"]
    assert payload["text"].count("1.500000") == 2


def test_from_route_result_reads_geojson_path_as_lat_lon():
    result = {
        "from": {"lat": 1.0, "lon": 2.0},
        "to": {"lat": 3.0, "lon": 4.0},
        "mode": "walk",
        "distance_m": 10.0,
        "duration_s": 7.0,
        "path": {"type": "LineString", "coordinates": [[2.0, 1.0], [3.0, 2.0], [4.0, 3.0]]},
    }
    payload = export.from_route_result(result)
    root = _gpx_root(payload["gpx"])
    trkpts = _gpx_findall(root, "trkpt")
    assert [(pt.attrib["lat"], pt.attrib["lon"]) for pt in trkpts] == [
        ("1.0000000", "2.0000000"),
        ("2.0000000", "3.0000000"),
        ("3.0000000", "4.0000000"),
    ]


def test_from_optimize_result_follows_order_and_keeps_names():
    stops = [
        {"lat": 0.0, "lon": 0.0, "name": "Zero"},
        {"lat": 1.0, "lon": 1.0, "name": "One"},
        {"lat": 2.0, "lon": 2.0, "name": "Two"},
    ]
    result = {
        "order": [1, 0, 2],
        "legs": [
            {"from_idx": 1, "to_idx": 0, "distance_m": 10.0, "duration_s": 8.0},
            {"from_idx": 0, "to_idx": 2, "distance_m": 20.0, "duration_s": 16.0},
        ],
        "total_distance_m": 30.0,
        "total_duration_s": 24.0,
        "mode": "walk",
        "roundtrip": False,
    }
    payload = export.from_optimize_result(stops, result)
    text = payload["text"]
    # Visit order, not input order.
    assert text.index("One") < text.index("Zero") < text.index("Two")
    google = payload["maps_link"]["google"]
    assert f"origin={_url_ll(1.0, 1.0)}" in google
    assert f"destination={_url_ll(2.0, 2.0)}" in google
    assert f"waypoints={_url_ll(0.0, 0.0)}" in google


def test_maps_urls_use_fixed_point_for_tiny_coordinates():
    # str(5e-5) is "5e-05"; Maps will not parse that as a latitude.
    payload = export.build(
        ((5e-5, -0.00004, "Near equator"), (1.0, 2.0, "Elsewhere")),
        mode="walk",
    )
    google = payload["maps_link"]["google"]
    apple = payload["maps_link"]["apple"]
    for url in (google, apple):
        assert "5e-05" not in url
        assert "e-" not in url.lower()
        assert "0.000050" in url
        assert "-0.000040" in url
        assert _url_ll(1.0, 2.0) in url


def test_text_list_marks_estimated_legs():
    payload = export.build(
        ((37.1, -122.1, "A"), (37.2, -122.2, "B"), (37.3, -122.3, "C")),
        mode="drive",
        legs=(
            {"distance_m": 300.0, "duration_s": 214.0},
            {"distance_m": 450.0, "duration_s": 321.0, "estimated": True},
        ),
        total_distance_m=750.0,
        total_duration_s=535.0,
    )
    text = payload["text"]
    # Routed A→B is unmarked; estimated B→C is flagged.
    assert "300 m, 3 min 34 s" in text
    assert "450 m, 5 min 21 s (estimated)" in text
    routed = [line for line in text.splitlines() if "300 m" in line]
    assert routed and "(estimated)" not in routed[0]


# --- tool boundary, fixture graph ----------------------------------------


def test_server_route_export_maps_link_contains_both_coords():
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")
    assert "error" not in result
    exp = result["export"]
    google = exp["maps_link"]["google"]
    apple = exp["maps_link"]["apple"]
    assert _url_ll(FROM_LAT, FROM_LON) in google
    assert _url_ll(TO_LAT, TO_LON) in google
    assert _url_ll(FROM_LAT, FROM_LON) in apple
    assert _url_ll(TO_LAT, TO_LON) in apple
    root = _gpx_root(exp["gpx"])
    assert _gpx_findall(root, "rtept") or _gpx_findall(root, "trkpt")
    text = exp["text"]
    assert f"{FROM_LAT:.6f}" in text
    assert f"{TO_LAT:.6f}" in text
    assert f"{FROM_LON:.6f}" in text
    assert f"{TO_LON:.6f}" in text


def test_server_route_error_has_no_export():
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="teleport")
    assert result["error"] == "unsupported_mode"
    assert "export" not in result


def test_server_optimize_route_multi_stop_link_and_every_named_stop():
    names = ["Alpha", "Bravo", "Charlie", "Delta"]
    result = server.optimize_route(
        _as_dicts(LINE_STOPS, names),
        mode="walk",
        roundtrip=False,
        start_index=LINE_START_INDEX,
    )
    assert "error" not in result
    assert result["order"] == LINE_EXPECTED_ORDER
    exp = result["export"]
    google = exp["maps_link"]["google"]
    # Every input coordinate appears in the multi-stop link.
    for lat, lon in LINE_STOPS:
        assert _url_ll(lat, lon) in google
    assert "waypoints=" in google
    text = exp["text"]
    for name in names:
        assert name in text
    for lat, lon in LINE_STOPS:
        assert f"{lat:.6f}" in text
        assert f"{lon:.6f}" in text
    root = _gpx_root(exp["gpx"])
    assert len(_gpx_findall(root, "wpt")) == 4
    assert len(_gpx_findall(root, "rtept")) == 4
    # Names survived into the GPX waypoints.
    wpt_names = [pt.find("gpx:name", GPX_NS).text for pt in _gpx_findall(root, "wpt")]
    assert set(wpt_names) == set(names)


def test_server_optimize_route_error_has_no_export():
    result = server.optimize_route(_as_dicts(LINE_STOPS), mode="teleport")
    assert result["error"] == "unsupported_mode"
    assert "export" not in result


def test_server_optimize_export_fits_the_default_token_budget():
    """Handoff is cheap: 10 stops + export still fits the conversational budget."""
    stops = _as_dicts([fx.node_latlon(2, j) for j in range(0, 20, 2)])
    result = server.optimize_route(stops, mode="walk", roundtrip=True)
    assert "export" in result
    assert budget.estimate_tokens(result) <= budget.token_budget()
