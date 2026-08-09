"""Pull the vendors' *documented example responses* out of their API docs.

Network-using capture step. Run by hand when refreshing the snapshots; never
run by the test suite. Writes `../upstream_examples/*.json`, which
`capture_answers.mjs` then feeds to the real competitor MCP servers through a
local stub endpoint.

    python benchmarks/competitors/capture/extract_doc_examples.py

Extraction is mechanical and self-verifying: every `<pre>` block on the page
is stripped of markup, HTML-unescaped, and *parsed as JSON*; blocks that do
not parse are discarded. A block is selected by its index among the parsing
blocks together with the nearest preceding heading, and the heading is
recorded in the output so a refresh that shifts the page layout is visible
rather than silent.
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "upstream_examples"

# name -> (doc url, index among the JSON-parsing <pre> blocks, expected heading)
SPEC: dict[str, tuple[str, int, str]] = {
    "mb_forward_geocode": (
        "https://docs.mapbox.com/api/search/geocoding/",
        4,
        "Example response: Forward geocoding",
    ),
    "mb_searchbox_forward": (
        "https://docs.mapbox.com/api/search/search-box/",
        2,
        "Example responses: Search request",
    ),
    "mb_searchbox_category": (
        "https://docs.mapbox.com/api/search/search-box/",
        4,
        "Example response: Search for POIs by category",
    ),
    "mb_directions": (
        "https://docs.mapbox.com/api/navigation/directions/",
        0,
        "Example EV Alternative route response with EV charging stops",
    ),
    "mb_matrix": (
        "https://docs.mapbox.com/api/navigation/matrix/",
        0,
        "Example response: Retrieve a matrix",
    ),
    "mb_isochrone": (
        "https://docs.mapbox.com/api/navigation/isochrone/",
        0,
        "Example response: Retrieve isochrones around a location",
    ),
    "g_geocode": (
        "https://developers.google.com/maps/documentation/geocoding/start",
        0,
        "Geocoding request and response (latitude/longitude lookup)",
    ),
}


def json_blocks(page: str) -> list[tuple[str, str]]:
    """(nearest preceding heading, JSON text) for every parseable <pre>."""
    found = []
    for match in re.finditer(r"<pre[^>]*>(.*?)</pre>", page, re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", match.group(1)))
        try:
            json.loads(text)
        except ValueError:
            continue
        headings = re.findall(r"<h[23][^>]*>(.*?)</h[23]>", page[: match.start()], re.S)
        heading = (
            html.unescape(re.sub(r"<[^>]+>", "", headings[-1])).strip().rstrip("​")
            if headings
            else ""
        )
        found.append((heading, text))
    return found


def main() -> int:
    pages: dict[str, str] = {}
    for name, (url, index, expected_heading) in SPEC.items():
        if url not in pages:
            with urllib.request.urlopen(url) as response:  # noqa: S310 - fixed doc URLs
                pages[url] = response.read().decode("utf-8", "replace")
        heading, text = json_blocks(pages[url])[index]
        assert heading == expected_heading, f"{name}: heading moved to {heading!r}"
        (OUT / f"{name}.json").write_text(json.dumps(json.loads(text), indent=2) + "\n")
        print(f"{name}: {len(text)} chars from {url} ({heading})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
