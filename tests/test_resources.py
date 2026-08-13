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
import inspect
import json
import re

import pytest

from placeroot import budget, categories, release, resources, server, tool_profiles


@pytest.fixture(autouse=True)
def _offline_release(monkeypatch):
    """No network in these tests: pin discovery to the baked-in fallback.

    resolve_release_info() is TTL-cached (#219), and 6h outlasts any test
    run, so it is reset on both sides — otherwise a value resolved here
    leaks into whichever test runs next.
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
        # #269: a newer release discovered while the bundled artifacts are
        # still current is reported, not adopted.
        (None, "2030-05-20.2", release.PINNED_RELEASE, "artifact-pinned"),
    ],
)
def test_data_version_resource_tracks_every_resolution_source(
    monkeypatch, env, discovered, expected_release, expected_source
):
    if expected_source != "artifact-pinned":
        # Every other case is about the resolution source itself; keep the
        # artifact rule out of the way by making its release stale.
        monkeypatch.setattr(
            release, "bundled_artifact_release", lambda: "2000-01-01.0"
        )
    else:
        monkeypatch.setattr(
            release, "bundled_artifact_release", lambda: release.PINNED_RELEASE
        )
    if env:
        monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", env)
    monkeypatch.setattr(release, "_discover", lambda: discovered)
    release.reset_cache()
    if not env:
        # Pin-first: the first resolve answers the pin and discovers in the
        # background; settle it so the payload reflects the tracked source.
        release.resolve_release()
        assert release._first_discovery_done.wait(2)

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
    _read_json(resources.DATA_VERSION_URI)  # pin-first; one background discovery
    assert release._first_discovery_done.wait(2)
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


# --- the guard ------------------------------------------------------------
#
# The categories resource's how_to_use tells the agent which tools take a
# `category` argument. MCP argument validation silently drops unknown
# kwargs, so naming a tool that has no such parameter does not fail — it
# returns a 200 OK *unfiltered* answer the agent believes was filtered.
# These tests check the prose against the live tool signatures, the same
# shape as the prompt guard in test_prompts.py.

# Tool mentions in resource prose are written `name()`, backticked with
# empty parentheses; `_bare_mentions` below enforces that nothing escapes
# this by dropping the parentheses.
_TOOL_REF = re.compile(r"`([a-z_][a-z0-9_]*)\(\)`")

# "the `category` filter of `find_places()` or `places_along_route()`" —
# the parameter claim, and every tool it is claimed for.
_PARAM_CLAIM = re.compile(
    r"`([a-z_][a-z0-9_]*)` filter of ((?:`[a-z_][a-z0-9_]*\(\)`(?:,| or|\s)*)+)"
)


def _prose(payload: dict) -> dict[str, str]:
    """Every free-text field of a resource payload, keyed by field name."""
    return {k: v for k, v in payload.items() if isinstance(v, str)}


def _all_prose() -> dict[str, str]:
    out = {}
    for uri in ALL_URIS:
        for field, text in _prose(_read_json(uri)).items():
            out[f"{uri}:{field}"] = text
    return out


def test_every_tool_reference_in_every_resource_is_a_registered_tool():
    """A renamed or deleted tool must not survive as prose in a resource."""
    registered = {t.name for t in asyncio.run(_server().list_tools())}
    referenced = {
        name for text in _all_prose().values() for name in _TOOL_REF.findall(text)
    }
    assert referenced, "no resource prose names any tool at all"
    dangling = sorted(referenced - registered)
    assert not dangling, (
        f"resource prose tells the agent to call {dangling}, which is not "
        f"registered. Known tools: {', '.join(sorted(registered))}."
    )


def test_no_registered_tool_name_appears_outside_the_guarded_form():
    """A bare mention would slip past `_TOOL_REF` and past the claim check."""
    registered = {t.name for t in asyncio.run(_server().list_tools())}
    for where, text in _all_prose().items():
        stripped = _TOOL_REF.sub("", text)
        bare = sorted(
            tool
            for tool in registered
            if re.search(rf"(?<![a-z0-9_]){re.escape(tool)}(?![a-z0-9_])", stripped)
        )
        assert not bare, (
            f"{where} mentions {bare} without the `name()` form the guard "
            f"below checks against the live signatures."
        )


def test_every_claimed_filter_argument_exists_on_the_tool_it_is_claimed_for():
    """The #198 sweep bug: how_to_use named tools that have no `category`.

    MCP drops unknown kwargs silently, so an agent following that sentence
    got unfiltered results with no error. Check the claim against the real
    signature of the real registered function.
    """
    claims = []
    for where, text in _all_prose().items():
        for param, tools_blob in _PARAM_CLAIM.findall(text):
            for tool in _TOOL_REF.findall(tools_blob):
                claims.append((where, param, tool))
    assert claims, "no `x` filter of `tool()` claim found to check"
    for where, param, tool in claims:
        fn = server._TOOL_FUNCS[tool]
        params = inspect.signature(fn).parameters
        assert param in params, (
            f"{where} says {tool}() takes a `{param}` filter, but its "
            f"signature is ({', '.join(params)}). MCP would silently drop "
            f"the argument and return an unfiltered answer."
        )


def test_the_claim_regex_actually_matches_the_shipped_sentence():
    """Guard the guard: a reworded sentence must not silently disarm it."""
    how_to_use = resources.categories_payload()["how_to_use"]
    matches = _PARAM_CLAIM.findall(how_to_use)
    assert matches, f"claim regex no longer matches how_to_use: {how_to_use!r}"
    param, tools_blob = matches[0]
    assert param == "category"
    assert set(_TOOL_REF.findall(tools_blob)) == {"find_places", "places_along_route"}
