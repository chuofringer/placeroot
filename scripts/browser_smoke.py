"""Headless-browser smoke test for the mapview HTML artifact (issue #33).

Everything else in this repo's default `pytest` run is offline and never
opens a real browser — mapview.py's guarantee ("zero network requests when
opened, renders with plain SVG/JS, no framework") is otherwise only checked
by string/regex assertions against the generated HTML (tests/test_mapview.py),
never by actually loading it and watching what the DOM/JS do. This script
closes that gap by driving headless Chromium (via Playwright) against two
freshly generated artifacts:

  1. a points map (find_places-shaped rows)
  2. a polygon/isochrone map (routing.isochrone()'s result shape, #34)

and asserting:
  - markers / polygon shapes actually exist in the rendered DOM
  - clicking a marker opens a popup
  - the page logs zero console errors and zero uncaught exceptions
  - the wheel event changes the pan/zoom transform (#viewport's `transform`
    attribute differs before/after)

Deliberately NOT a pytest test_*.py file and NOT part of the default
`uv run pytest` — this needs a real browser binary (`playwright install
chromium`) that the offline test job doesn't have and shouldn't need. CI
runs it as a separate `browser-smoke` job (see .github/workflows/ci.yml)
after `uv sync --group browser` + `uv run playwright install chromium
--with-deps`. Run it locally the same way CI does:

    uv run --group browser python scripts/browser_smoke.py

The playwright import is deferred into main() (rather than done at module
scope) so that merely importing this module — e.g. an accidental `import
scripts.browser_smoke` from somewhere pytest *does* collect — never requires
playwright to be installed.
"""

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

CENTER_LAT = 40.700000
CENTER_LON = -73.900000


def _points_payload() -> dict:
    return {
        "results": [
            {
                "name": f"Place {i}",
                "category": "coffee_shop",
                "basic_category": "coffee_shop",
                "operating_status": "open",
                "confidence": 0.9,
                "lat": CENTER_LAT + i * 0.001,
                "lon": CENTER_LON + i * 0.001,
                "distance_m": i * 10,
            }
            for i in range(6)
        ]
    }


def _isochrone_payload() -> dict:
    ring = [
        [CENTER_LON - 0.01, CENTER_LAT - 0.01],
        [CENTER_LON + 0.01, CENTER_LAT - 0.01],
        [CENTER_LON + 0.01, CENTER_LAT + 0.01],
        [CENTER_LON - 0.01, CENTER_LAT + 0.01],
        [CENTER_LON - 0.01, CENTER_LAT - 0.01],
    ]
    return {
        "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
        "minutes": 15,
        "mode": "walk",
        "speed_m_s": 1.4,
        "polygon": {"type": "Polygon", "coordinates": [ring]},
        "polygon_method": "convex_hull",
        "stats": {"reachable_nodes": 128, "max_radius_m": 940.2, "area_km2": 0.62},
    }


class SmokeFailure(RuntimeError):
    pass


def _check_points_map(playwright, path: Path) -> None:
    browser = playwright.chromium.launch()
    try:
        page = browser.new_page()
        console_errors = []
        page_errors = []
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        page.goto(path.as_uri())
        page.wait_for_selector("#map", state="attached")

        markers = page.query_selector_all(".marker")
        if len(markers) != 6:
            raise SmokeFailure(f"points map: expected 6 markers in DOM, found {len(markers)}")

        transform_before = page.eval_on_selector("#viewport", "el => el.getAttribute('transform')")

        # Marker click opens a popup. Click the <circle> (the actual clickable
        # geometry) rather than its parent <g>, whose own bounding box is
        # sometimes reported as unclickable/occluded by browsers.
        marker_circles = page.query_selector_all(".marker circle")
        marker_circles[0].click()
        page.wait_for_function(
            "document.getElementById('popup').style.display === 'block'", timeout=2000
        )
        popup_text = page.eval_on_selector("#popup", "el => el.textContent")
        if not popup_text or "Place 0" not in popup_text:
            raise SmokeFailure(f"points map: popup did not show marker details, got {popup_text!r}")

        # Wheel zoom changes the pan/zoom transform.
        box = page.eval_on_selector(
            "#map",
            "el => { var r = el.getBoundingClientRect(); "
            "return {x: r.x + r.width/2, y: r.y + r.height/2}; }",
        )
        page.mouse.move(box["x"], box["y"])
        page.mouse.wheel(0, -200)
        page.wait_for_timeout(100)
        transform_after = page.eval_on_selector("#viewport", "el => el.getAttribute('transform')")
        if transform_after == transform_before:
            raise SmokeFailure(
                "points map: #viewport transform did not change after a wheel event "
                f"(before={transform_before!r}, after={transform_after!r})"
            )

        if console_errors:
            raise SmokeFailure(f"points map: console errors logged: {console_errors}")
        if page_errors:
            raise SmokeFailure(f"points map: uncaught page errors: {page_errors}")

        print(f"[points map] {len(markers)} markers, popup + pan/zoom OK, no console errors")
    finally:
        browser.close()


def _check_polygon_map(playwright, path: Path) -> None:
    browser = playwright.chromium.launch()
    try:
        page = browser.new_page()
        console_errors = []
        page_errors = []
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        page.goto(path.as_uri())
        page.wait_for_selector("#map", state="attached")

        shapes = page.query_selector_all(".shape-polygon")
        if len(shapes) != 1:
            raise SmokeFailure(f"polygon map: expected 1 polygon shape, found {len(shapes)}")

        d_attr = page.eval_on_selector(".shape-polygon", "el => el.getAttribute('d')")
        if not d_attr or "M" not in d_attr:
            raise SmokeFailure(f"polygon map: polygon has no path data, got {d_attr!r}")

        # Click the polygon: popup should carry the isochrone stats.
        shapes[0].click()
        page.wait_for_function(
            "document.getElementById('popup').style.display === 'block'", timeout=2000
        )
        popup_text = page.eval_on_selector("#popup", "el => el.textContent")
        if not popup_text or "reachable_nodes" not in popup_text:
            raise SmokeFailure(
                f"polygon map: popup did not show isochrone stats, got {popup_text!r}"
            )

        if console_errors:
            raise SmokeFailure(f"polygon map: console errors logged: {console_errors}")
        if page_errors:
            raise SmokeFailure(f"polygon map: uncaught page errors: {page_errors}")

        print("[polygon map] 1 polygon shape, popup with stats OK, no console errors")
    finally:
        browser.close()


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "browser_smoke.py requires the 'browser' dependency group:\n"
            "  uv sync --group browser\n"
            "  uv run playwright install chromium --with-deps\n"
            "  uv run --group browser python scripts/browser_smoke.py",
            file=sys.stderr,
        )
        return 2

    from placeroot import mapview

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        points_result = mapview.write_artifact(
            _points_payload(), title="Smoke Points", out_dir=tmp_path
        )
        polygon_result = mapview.write_artifact(
            _isochrone_payload(), title="Smoke Isochrone", out_dir=tmp_path
        )

        with sync_playwright() as playwright:
            try:
                _check_points_map(playwright, Path(points_result["path"]))
                _check_polygon_map(playwright, Path(polygon_result["path"]))
            except SmokeFailure as e:
                print(f"FAIL: {e}", file=sys.stderr)
                return 1

    print("browser_smoke: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
