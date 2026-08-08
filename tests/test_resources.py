"""MCP resources: placeroot://data-version and placeroot://categories (issue #195).

Four things are worth testing about a resource, and they are all here:

- it is listed with a URI, description and MIME type, and reads back as the
  JSON its MIME type claims;
- the data-version resource and the `data_version` tool return the *same*
  values, because they share one code path — a resource that drifted from
  its tool would be worse than no resource at all;
- the categories resource stays a summary: its served text is measured with
  the repo's own chars/4 estimator and asserted under the 1.5k budget, so
  an edit that turns it back into the 2,100-slug CSV dump fails loudly;
- registering both changed `tools/list` by exactly nothing, and both are
  present under every PLACEROOT_TOOLS subset — the same zero-cost premise
  the prompts in #194 rest on.
"""

import asyncio
import json

import pytest

from placeroot import budget, categories, release, resources, server, tool_profiles


@pytest.fixture(autouse=True)
def _offline_release(monkeypatch):
    """No network in these tests: pin discovery to the baked-in fallback.

    resolve_release_info() is a process-lifetime lru_cache, so it is reset
    on both sides — otherwise a value resolved here leaks into whichever
    test runs next.
    """
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "_discover", lambda: None)
    release.reset_cache()
    yield
    release.reset_cache()


def _server(spec=None):
    return server.build_server(spec)


def _resources(spec=None):
    return {str(r.uri): r for r in asyncio.run(_server(spec).list_resources())}


def _read(uri: str, spec=None) -> tuple[str, str | None]:
    """(text, mime type) as a client would get them from resources/read."""
    contents = list(asyncio.run(_server(spec).read_resource(uri)))
    assert len(contents) == 1, f"{uri} returned {len(contents)} content blocks"
    return contents[0].content, contents[0].mime_type


def _read_json(uri: str, spec=None) -> dict:
    text, _mime = _read(uri, spec)
    return json.loads(text)


ALL_URIS = [resources.DATA_VERSION_URI, resources.CATEGORIES_URI]


# --- resources/list -------------------------------------------------------


def test_resources_list_returns_both_uris():
    assert set(_resources()) == set(ALL_URIS)


@pytest.mark.parametrize("uri", ALL_URIS)
def test_each_resource_has_a_name_description_and_mime_type(uri):
    resource = _resources()[uri]
    assert resource.name
    assert resource.description and resource.description.strip()
    assert resource.mime_type == "application/json"


def test_descriptions_point_at_the_tools_that_do_the_rest():
    """A resource is a summary; the description has to say where the rest is."""
    assert "data_version" in _resources()[resources.DATA_VERSION_URI].description
    assert "search_categories" in _resources()[resources.CATEGORIES_URI].description


# --- resources/read -------------------------------------------------------


@pytest.mark.parametrize("uri", ALL_URIS)
def test_each_resource_reads_back_as_json_with_its_declared_mime_type(uri):
    text, mime = _read(uri)
    assert mime == "application/json"
    assert isinstance(json.loads(text), dict)


def test_unknown_uri_is_an_error():
    with pytest.raises(Exception):
        _read("placeroot://not-a-resource")


# --- data-version: one code path with the tool ----------------------------


def test_data_version_resource_matches_the_tool_exactly():
    """The point of the shared code path: the two surfaces cannot drift."""
    assert _read_json(resources.DATA_VERSION_URI) == server.data_version()


@pytest.mark.parametrize(
    ("env", "discovered", "expected_release", "expected_source"),
    [
        ("2099-01-01.0", None, "2099-01-01.0", "env-override"),
        (None, "2030-05-20.2", "2030-05-20.2", "discovered"),
        (None, None, release.PINNED_RELEASE, "pinned-fallback"),
    ],
)
def test_data_version_resource_tracks_every_resolution_source(
    monkeypatch, env, discovered, expected_release, expected_source
):
    if env:
        monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", env)
    monkeypatch.setattr(release, "_discover", lambda: discovered)
    release.reset_cache()

    payload = _read_json(resources.DATA_VERSION_URI)

    assert payload["release"] == expected_release
    assert payload["source"] == expected_source
    assert payload["release_date"] == expected_release.rsplit(".", 1)[0]
    # Still equal to the tool under every source, not just the default one.
    assert payload == server.data_version()


def test_data_version_resource_reads_the_cache_without_hitting_the_network(monkeypatch):
    """Reading the resource must never trigger a fresh discovery call."""
    release.reset_cache()
    calls = []

    def counting_discover():
        calls.append(1)
        return None

    monkeypatch.setattr(release, "_discover", counting_discover)
    for _ in range(3):
        _read_json(resources.DATA_VERSION_URI)
    assert len(calls) == 1


# --- categories: a summary, not the CSV -----------------------------------


def test_categories_resource_summarizes_the_taxonomy():
    payload = _read_json(resources.CATEGORIES_URI)
    summary = categories.taxonomy_summary()

    assert payload["total_categories"] == summary["total"] == 2117
    assert payload["schema_version"] == categories.SCHEMA_VERSION
    listed = [row["category"] for row in payload["top_level_categories"]]
    assert listed == [name for name, _count in summary["top_level"]]
    assert "eat_and_drink" in listed and "retail" in listed


def test_top_level_counts_sum_to_the_total():
    """Every slug sits under exactly one top-level branch, so the counts tile."""
    payload = _read_json(resources.CATEGORIES_URI)
    assert (
        sum(row["slugs"] for row in payload["top_level_categories"])
        == (payload["total_categories"])
    )


def test_top_level_categories_are_ordered_by_descending_count():
    rows = _read_json(resources.CATEGORIES_URI)["top_level_categories"]
    keys = [(-row["slugs"], row["category"]) for row in rows]
    assert keys == sorted(keys)


def test_categories_resource_points_at_search_categories_for_the_rest():
    payload = _read_json(resources.CATEGORIES_URI)
    assert "search_categories" in payload["how_to_use"]
    assert "summary" in payload["note"]


def test_categories_resource_is_not_the_full_slug_dump():
    """The whole reason it is a summary: no leaf slugs in the payload."""
    text, _mime = _read(resources.CATEGORIES_URI)
    leaves = {row["slug"] for row in categories._load_categories()}
    top_level = {name for name, _ in categories.taxonomy_summary()["top_level"]}
    # Quoted form only: leaf names may appear inside the prose as examples
    # ("e.g. coffee_shop under eat_and_drink"), which is a sentence, not an
    # enumeration. A dumped taxonomy would put them in as JSON strings.
    listed = [slug for slug in leaves - top_level if f'"{slug}"' in text]
    assert not listed, f"leaf slugs leaked into the summary: {sorted(listed)[:10]}"


def test_categories_resource_fits_the_token_budget():
    """Measured with the repo's own chars/4 estimator, on the served text."""
    text, _mime = _read(resources.CATEGORIES_URI)
    estimate = len(text) // budget.CHARS_PER_TOKEN
    assert estimate == resources.categories_token_estimate()
    assert estimate <= resources.CATEGORIES_TOKEN_BUDGET, (
        f"placeroot://categories is ~{estimate} tokens, over the "
        f"{resources.CATEGORIES_TOKEN_BUDGET} budget — keep it a summary."
    )


def test_categories_payload_is_deterministic():
    assert resources.categories_payload() == resources.categories_payload()
    assert _read(resources.CATEGORIES_URI) == _read(resources.CATEGORIES_URI)


# --- zero token cost ------------------------------------------------------


def _tools_list_json(register_resources: bool) -> str:
    """`tools/list` as a client would see it, with resources registered or not."""
    original = resources.register
    if not register_resources:
        resources.register = lambda *_args, **_kwargs: None
    try:
        tools = asyncio.run(server.build_server(None).list_tools())
    finally:
        resources.register = original
    return json.dumps(
        [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools],
        sort_keys=True,
    )


def test_tools_list_is_byte_identical_with_and_without_resources():
    """The premise of the feature: resources add nothing to every conversation."""
    assert _tools_list_json(True) == _tools_list_json(False)


# --- subset profiles ------------------------------------------------------


@pytest.mark.parametrize("profile", sorted(tool_profiles.PROFILES))
def test_both_resources_are_registered_under_every_profile(profile):
    """Resources are always-on: they are not in tools/list, so they cost nothing."""
    assert set(_resources(profile)) == set(ALL_URIS)


@pytest.mark.parametrize("profile", sorted(tool_profiles.PROFILES))
def test_resources_read_the_same_under_every_profile(profile):
    for uri in ALL_URIS:
        assert _read(uri, profile) == _read(uri, None)


def test_both_resources_survive_the_narrowest_possible_selection():
    """A one-tool install still gets them — registration ignores the selection.

    `data_version` is in tool_profiles.ALWAYS_INCLUDED, so the tool is never
    actually dropped; the resource is always-on for its own reason (it is
    not in tools/list, so gating it would save nobody anything) and this
    pins that it does not quietly become selection-dependent.
    """
    spec = "geocode"
    tools = {t.name for t in asyncio.run(_server(spec).list_tools())}
    assert tools == {"geocode", "data_version"}
    assert set(_resources(spec)) == set(ALL_URIS)
    assert _read_json(resources.DATA_VERSION_URI, spec) == server.data_version()
