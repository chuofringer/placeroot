"""#469: the query corpus's answer-location checks.

benchmarks/query_corpus.py is not part of the installed package, so it is
loaded from the repo like test_benchmark_script.py does. Only the check
helpers are exercised here — never the live tools.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _corpus():
    spec = importlib.util.spec_from_file_location(
        "query_corpus", REPO_ROOT / "benchmarks" / "query_corpus.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_near_miss_measures_km_from_the_truth():
    c = _corpus()
    assert c._near_miss({"lat": 35.658, "lon": 139.7016}, (35.658, 139.7016, 2)) is None
    # Snow Peak Land Station, 7 km east of Shibuya Station.
    miss = c._near_miss({"lat": 35.6796, "lon": 139.7652}, (35.658, 139.7016, 2))
    assert miss and miss.startswith("WRONG PLACE")
    assert c._near_miss({"lat": 0, "lon": 0}, None) is None


def test_flow_fails_when_the_anchor_is_in_the_wrong_neighbourhood(monkeypatch):
    c = _corpus()

    class Geo:
        @staticmethod
        def resolve_place(place):
            return [{"name": "Snow Peak Land Station", "lat": 35.6796, "lon": 139.7652}]

    class Overture:
        @staticmethod
        def find_places(lat, lon, radius_m, category, limit):
            return [{}] * 25

    monkeypatch.setattr(c, "_mods", lambda: {"geo": Geo, "overture": Overture})
    ok, detail = c._flow("Shibuya Station Tokyo", "pharmacy")()
    assert ok  # no near= : tolerance passes, as before
    ok, detail = c._flow("Shibuya Station Tokyo", "pharmacy", near=(35.658, 139.7016, 2))()
    assert not ok and detail.startswith("WRONG PLACE")


def test_route_between_bounds_the_confirmed_distance_only(monkeypatch):
    c = _corpus()
    calls = []

    class Server:
        @staticmethod
        def from_to(a, b, mode="walk", confirm=False):
            calls.append(confirm)
            if not confirm:
                return {"error": "needs_confirm", "detail": "First walk builds the graph"}
            return {"distance_m": 8570.0}

    monkeypatch.setattr(c, "_mods", lambda: {"server": Server})
    run = c._route_between("Shibuya Station Tokyo", "Yoyogi Park Tokyo", max_m=3000)
    ok, detail = run()
    assert ok and detail.startswith("ASK")  # the peek leg carries no distance
    ok, detail = run()
    assert not ok and detail.startswith("TOO FAR 8570m")
    assert calls == [False, True]

    Server.from_to = staticmethod(lambda a, b, mode="walk", confirm=False: {"distance_m": 1500.0})
    ok, detail = c._route_between("a", "b", max_m=3000)()
    assert ok and detail == "1500m"
