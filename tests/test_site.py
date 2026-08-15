"""Offline guardrails for the marketing site (site/, issue #84).

The three-page marketing site (index, how-it-works, add-to-your-ai) is a
static, no-build-step site recreated from the design handoff. These tests
keep it from silently rotting: every page must exist and parse, the only
permitted external references are Google Fonts (the design's typefaces) and
known link destinations, the onboarding config must stay present, and the
verbatim install commands on the installer page must not drift.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

SITE_DIR = Path(__file__).parent.parent / "site"
PAGES = [
    "index.html", "how-it-works.html", "add-to-your-ai.html", "why-placeroot.html",
    "privacy.html",
]

# External hosts the design legitimately references: Google Fonts for the
# typefaces, and documentation/repo links. Anything else fetched over the
# network (a CDN script, an analytics beacon, an external image host) is a
# regression — the design ships no external images.
_ALLOWED_EXTERNAL_HOSTS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "github.com",
    "pypi.org",
    "www.npmjs.com",
    "docs.astral.sh",
    "overturemaps.org",  # privacy.html: link to the data source it queries
    "placeroot.dev",  # og:image / canonical absolute URLs in meta tags
    # Developer-credit links (footer): the maintainer's site and siblings.
    "vibemapper.dev",
    "funradar.app",
    "flood.live",
)

# src=/href= values pointing at an external origin.
_EXTERNAL_REF_RE = re.compile(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)["\']', re.IGNORECASE)
_PROTOCOL_RELATIVE_RE = re.compile(r'(?:src|href)\s*=\s*["\']//', re.IGNORECASE)

CONFIG_SNIPPET_FRAGMENTS = ['"mcpServers"', '"placeroot"', '"command": "uvx"', '["placeroot"]']

# Verified install instructions that must stay verbatim on the installer page.
VERBATIM_INSTALL = [
    "claude mcp add placeroot -- uvx placeroot",
    "uvx placeroot",
    "claude_desktop_config.json",
    "~/.gemini/settings.json",
    ".cursor/mcp.json",
    "codex mcp add placeroot -- uvx placeroot",
]


class _StructureParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags_seen = []

    def handle_starttag(self, tag, attrs):
        self.tags_seen.append(tag)


def _parse(doc: str) -> _StructureParser:
    p = _StructureParser()
    p.feed(doc)
    p.close()
    return p


def test_all_pages_exist():
    for name in PAGES:
        assert (SITE_DIR / name).is_file(), f"missing site/{name}"


def test_all_pages_parse_as_html():
    for name in PAGES:
        parser = _parse((SITE_DIR / name).read_text(encoding="utf-8"))
        for tag in ("html", "head", "body", "title"):
            assert tag in parser.tags_seen, f"{name}: missing <{tag}>"


def test_pages_declare_title_and_description():
    for name in PAGES:
        doc = (SITE_DIR / name).read_text(encoding="utf-8")
        assert "<title>" in doc, f"{name}: no <title>"
        assert 'name="description"' in doc, f"{name}: no meta description"
        assert 'property="og:image"' in doc, f"{name}: no og:image"
        assert 'name="twitter:card"' in doc, f"{name}: no twitter:card"


def test_no_unexpected_external_references():
    for name in PAGES:
        doc = (SITE_DIR / name).read_text(encoding="utf-8")
        assert not _PROTOCOL_RELATIVE_RE.search(doc), f"{name}: protocol-relative ref"
        for url in _EXTERNAL_REF_RE.findall(doc):
            host = url.split("/")[2]
            assert host in _ALLOWED_EXTERNAL_HOSTS, f"{name}: unexpected external ref {url}"


def test_pages_use_local_favicon_and_assets():
    for name in PAGES:
        doc = (SITE_DIR / name).read_text(encoding="utf-8")
        assert 'href="logo-mark.svg"' in doc, f"{name}: favicon not the local logo mark"
    for asset in ("logo-mark.svg", "logo-lockup.png", "og-image.png"):
        assert (SITE_DIR / asset).is_file(), f"missing asset site/{asset}"


def test_index_has_onboarding_config_and_copy_button():
    doc = (SITE_DIR / "index.html").read_text(encoding="utf-8")
    for fragment in CONFIG_SNIPPET_FRAGMENTS:
        assert fragment in doc, f"index.html: missing config fragment {fragment}"
    assert 'id="copyBtn"' in doc and "clipboard" in doc


def test_installer_page_keeps_verbatim_install_commands():
    doc = (SITE_DIR / "add-to-your-ai.html").read_text(encoding="utf-8")
    for cmd in VERBATIM_INSTALL:
        assert cmd in doc, f"add-to-your-ai.html: install instruction drifted: {cmd!r}"
    # All six tools/tabs present.
    tabs = (
        "Claude Desktop", "Claude Code", "ChatGPT Desktop",
        "Gemini CLI", "Cursor", "any MCP agent",
    )
    for tool in tabs:
        assert tool in doc, f"add-to-your-ai.html: missing tab {tool}"


def test_pages_cross_link_and_point_at_the_package():
    for name in PAGES:
        doc = (SITE_DIR / name).read_text(encoding="utf-8")
        assert "pypi.org/project/placeroot" in doc, f"{name}: no package link"
        assert "github.com/chuofringer/placeroot" in doc, f"{name}: no source link"
    for name in ("how-it-works.html", "add-to-your-ai.html", "why-placeroot.html"):
        doc = (SITE_DIR / name).read_text(encoding="utf-8")
        assert 'href="index.html"' in doc, f"{name}: no link back to landing"


def test_no_forbidden_marketing_words():
    for name in PAGES:
        doc = (SITE_DIR / name).read_text(encoding="utf-8").lower()
        for word in ("revolutionary", "blazingly", "game-changing", "game changing"):
            assert word not in doc, f"{name}: forbidden word {word!r}"


def test_pages_have_canonical_urls():
    canon = {
        "index.html": 'href="https://placeroot.dev/"',
        "how-it-works.html": 'href="https://placeroot.dev/how-it-works.html"',
        "add-to-your-ai.html": 'href="https://placeroot.dev/add-to-your-ai.html"',
        "why-placeroot.html": 'href="https://placeroot.dev/why-placeroot.html"',
        "privacy.html": 'href="https://placeroot.dev/privacy.html"',
    }
    for name, href in canon.items():
        doc = (SITE_DIR / name).read_text(encoding="utf-8")
        assert 'rel="canonical"' in doc and href in doc, f"{name}: missing/wrong canonical"


def test_robots_and_sitemap_present():
    assert (SITE_DIR / "robots.txt").is_file()
    sm = SITE_DIR / "sitemap.xml"
    assert sm.is_file()
    doc = sm.read_text(encoding="utf-8")
    pages = (
        "placeroot.dev/", "how-it-works.html", "add-to-your-ai.html",
        "why-placeroot.html", "privacy.html",
    )
    for page in pages:
        assert page in doc, f"sitemap missing {page}"
