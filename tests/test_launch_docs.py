"""Guardrails for the launch content in docs/launch/ (issue #21).

Checks, deliberately conservative:
  1. The required launch docs exist and are non-empty.
  2. None of them contain a forbidden marketing superlative.
  3. None of them contain emoji.
  4. Every "metric-shaped" number in them (a number immediately followed by
     a unit like "tokens", "%", "ms", "places", "queries", "calls", or the
     phrasing "N-call"/"N tool calls") is drawn from a whitelist of values
     that trace back to README.md, ROADMAP.md, docs/METRICS.md, the internal
     product plan (kept outside the repo), or
     site/index.html — the repo's actual measured numbers. This is not a
     check on every digit in the files (dates, JSON schema versions, HN
     title numbering, and step markers are not "claims" and are exempt by
     construction of the regex below); it targets exactly the shape of
     number that would carry an invented statistic.

Whitelist derivation (each value traced to its source file/line):
  1944    internal plan: "summarize_area — category mix (1,944 places ~= 320 tokens)"
  320     internal plan: same line — token cost for that summarize_area call
  113     README.md / ROADMAP.md / internal plan: "113 queries" geocode benchmark
  100     README.md / ROADMAP.md / internal plan: hit@1 100% (113 queries, saturated set)
  35.4    initial benchmark run (progression cited in the launch essay)
  54.9    initial benchmark run, same measurement
  21      internal plan: "Local tile cache -- warm ~21ms"
  17      internal plan: "~5.9K tokens for a 17-call analysis"
          (also examples/site_selection/README.md: "17 tool calls total")
  5.9K    internal plan: "~5.9K tokens for a 17-call analysis"
  501     site/index.html: "PlaceRoot <span class=\"token-count placeroot\">501 tokens</span>"
  45000   site/index.html: "Raw GeoJSON <span class=\"token-count raw\">~45,000 tokens</span>"
  291     site/index.html: "Each feature above runs ~291 tokens (measured...)"
  25      internal plan: Geoawesome essay "45K-token GeoJSON collapsing to a 25-token reference"
  2000    README.md / internal plan / ROADMAP.md: "every answer fits in ~2K tokens" design rule
"""

import re
from pathlib import Path

LAUNCH_DIR = Path(__file__).resolve().parent.parent / "docs" / "launch"

REQUIRED_FILES = [
    "post-why-agents-are-bad-at-maps.md",
    "registry-submissions.md",
]

FORBIDDEN_WORDS = [
    "revolutionary",
    "blazing",
    "game-changing",
    "game changer",
    "cutting-edge",
    "cutting edge",
    "world-class",
    "best-in-class",
    "unparalleled",
    "groundbreaking",
    "next-generation",
    "next generation",
    "seamless",
    "effortless",
    "magical",
    "disruptive",
    "10x",
]

EMOJI_PATTERN = re.compile(
    "["
    "\U0001f300-\U0001faff"  # symbols, pictographs, supplemental symbols
    "\U00002600-\U000027bf"  # misc symbols and dingbats
    "\U0001f1e6-\U0001f1ff"  # regional indicators (flag emoji)
    "\U0001f000-\U0001f0ff"  # mahjong/playing cards (rare but emoji-adjacent)
    "]",
    flags=re.UNICODE,
)

# A number immediately followed (allowing "K" for thousands) by a metric
# unit, or written as "N-call"/"N tool calls" — the shape a factual claim
# takes in this content, as opposed to a date, version, or list index.
NUMBER_CLAIM_PATTERN = re.compile(
    r"(\d[\d,]*\.?\d*K?)(?:-call\b|\s+tool\s+calls?|\s*(?:tokens?|%|ms|places?|quer(?:y|ies)|calls?))",
    re.IGNORECASE,
)

NUMBER_WHITELIST = {
    "1944",
    "320",
    "113",
    "100",
    "35.4",
    "54.9",
    "21",
    "17",
    "5.9K",
    "501",
    "45000",
    "291",
    "25",
    "2000",
}


def _normalize(number: str) -> str:
    """Strip thousands separators; preserve a trailing K."""
    return number.replace(",", "")


def test_all_required_launch_docs_exist_and_are_nonempty():
    for name in REQUIRED_FILES:
        path = LAUNCH_DIR / name
        assert path.is_file(), f"missing required launch doc: {path}"
        assert path.stat().st_size > 0, f"launch doc is empty: {path}"


def test_no_forbidden_marketing_words():
    for name in REQUIRED_FILES:
        text = (LAUNCH_DIR / name).read_text(encoding="utf-8").lower()
        for word in FORBIDDEN_WORDS:
            assert word not in text, f"forbidden word {word!r} found in {name}"


def test_no_emoji():
    for name in REQUIRED_FILES:
        text = (LAUNCH_DIR / name).read_text(encoding="utf-8")
        found = EMOJI_PATTERN.findall(text)
        assert not found, f"emoji {found!r} found in {name}"


def test_metric_numbers_are_repo_sourced():
    for name in REQUIRED_FILES:
        text = (LAUNCH_DIR / name).read_text(encoding="utf-8")
        for raw in NUMBER_CLAIM_PATTERN.findall(text):
            normalized = _normalize(raw)
            assert normalized in NUMBER_WHITELIST, (
                f"unrecognized metric number {raw!r} (normalized {normalized!r}) "
                f"in {name} — not in the repo-sourced whitelist; either it's a "
                f"typo/invented stat, or the whitelist needs a documented addition"
            )
