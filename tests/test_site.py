"""Offline guardrails for the static landing page (issue #28).

Keeps site/index.html from silently rotting or growing a CDN/analytics/font
dependency: it must exist, parse as HTML, carry zero external src/href
references, and still show the copy-paste onboarding config from README.md.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

SITE_DIR = Path(__file__).parent.parent / "site"
INDEX_PATH = SITE_DIR / "index.html"

# An actual src="..."/href="..." attribute value the browser would fetch
# over the network — not bare "https://" text sitting in a code sample or
# attribution copy. Same pattern tests/test_mapview.py uses for the map
# artifact itself.
_EXTERNAL_REF_RE = re.compile(r'(?:src|href)\s*=\s*["\']https?://', re.IGNORECASE)
_PROTOCOL_RELATIVE_RE = re.compile(r'(?:src|href)\s*=\s*["\']//', re.IGNORECASE)

CONFIG_SNIPPET_FRAGMENTS = [
    '"mcpServers"',
    '"placeroot"',
    '"command": "uvx"',
    '"args": ["placeroot"]',
]


class _StructureCheckingParser(HTMLParser):
    """Confirms the document parses as a sane HTML skeleton."""

    def __init__(self):
        super().__init__()
        self.tags_seen = []

    def handle_starttag(self, tag, attrs):
        self.tags_seen.append(tag)


def _parse_ok(doc: str) -> _StructureCheckingParser:
    parser = _StructureCheckingParser()
    parser.feed(doc)
    parser.close()
    return parser


def test_index_html_exists():
    assert INDEX_PATH.is_file()


def test_index_html_parses_as_html():
    doc = INDEX_PATH.read_text(encoding="utf-8")
    parser = _parse_ok(doc)
    for tag in ("html", "head", "body", "title"):
        assert tag in parser.tags_seen, f"missing <{tag}>"


def test_index_html_has_no_external_references():
    doc = INDEX_PATH.read_text(encoding="utf-8")
    assert not _EXTERNAL_REF_RE.search(doc), "found an external http(s) src/href"
    assert not _PROTOCOL_RELATIVE_RE.search(doc), "found a protocol-relative src/href"


def test_index_html_includes_config_snippet():
    doc = INDEX_PATH.read_text(encoding="utf-8")
    for fragment in CONFIG_SNIPPET_FRAGMENTS:
        assert fragment in doc, f"missing onboarding config fragment: {fragment}"


def test_index_html_has_copy_button():
    doc = INDEX_PATH.read_text(encoding="utf-8")
    assert 'id="copy-config"' in doc
    assert "clipboard" in doc


def test_no_forbidden_words_or_emoji():
    doc = INDEX_PATH.read_text(encoding="utf-8").lower()
    for word in ("revolutionary", "blazingly", "game-changing", "game changing"):
        assert word not in doc, f"forbidden word present: {word}"
    # Common emoji ranges; a plain engineering landing page shouldn't need any.
    assert not re.search(
        "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]", doc
    )


def test_demo_map_html_exists_and_is_self_contained():
    demo_path = SITE_DIR / "demo-map.html"
    assert demo_path.is_file(), "site/demo-map.html must be a generated map artifact"
    doc = demo_path.read_text(encoding="utf-8")
    assert not _EXTERNAL_REF_RE.search(doc)
    assert not _PROTOCOL_RELATIVE_RE.search(doc)


def test_style_css_exists_and_is_referenced_locally():
    css_path = SITE_DIR / "style.css"
    assert css_path.is_file()
    doc = INDEX_PATH.read_text(encoding="utf-8")
    assert 'href="style.css"' in doc
