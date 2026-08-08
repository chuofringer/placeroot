"""MCP prompt templates (issue #194).

Three things are worth testing about a prompt, and they are all here:

- it renders at all, with typed arguments a client can fill in;
- every tool it tells the agent to call still exists (the guard, the same
  shape as the PLACEROOT_TOOLS profile guard in test_tool_profiles.py — a
  rename must not leave prose pointing at a name nothing answers to);
- registering it changed `tools/list` by exactly nothing, which is the
  entire premise of shipping workflows as prompts rather than as tools.
"""

import asyncio
import json
import re

import pytest

from placeroot import prompts, server, tool_profiles

# Tool mentions in prompt text are written `name()`, backticked with empty
# parentheses. This finds them; `_bare_mentions` below enforces that nothing
# escapes it by dropping the parentheses.
_TOOL_REF = re.compile(r"`([a-z_][a-z0-9_]*)\(\)`")

# Sample arguments every prompt renders with. Keyed by prompt name; the
# optional arguments are exercised too, in the second sample.
SAMPLES: dict[str, list[dict[str, str]]] = {
    "site_selection": [{"business_type": "bike repair shop", "area": "Portland, Oregon"}],
    "compare_neighborhoods": [{"area_a": "Shoreditch, London", "area_b": "Peckham, London"}],
    "plan_errands": [
        {"stops": "pharmacy, hardware store, post office"},
        {"stops": "pharmacy, hardware store", "start": "Union Square, San Francisco"},
    ],
}


def _server(spec=None):
    return server.build_server(spec)


def _prompts(spec=None):
    return {p.name: p for p in asyncio.run(_server(spec).list_prompts())}


def _tool_names(spec=None) -> set[str]:
    return {t.name for t in asyncio.run(_server(spec).list_tools())}


def _render(name: str, args: dict, spec=None) -> str:
    result = asyncio.run(_server(spec).get_prompt(name, args))
    return "\n\n".join(m.content.text for m in result.messages)


def _all_renderings(spec=None) -> dict[str, str]:
    return {
        name: _render(name, args, spec)
        for name, samples in SAMPLES.items()
        for args in samples
    }


def test_prompts_list_returns_every_declared_prompt():
    assert set(_prompts()) == set(prompts.PROMPTS)


def test_samples_cover_every_prompt():
    """A prompt added without a sample would skip every render assertion below."""
    assert set(SAMPLES) == set(prompts.PROMPTS)


@pytest.mark.parametrize("name", sorted(prompts.PROMPTS))
def test_each_prompt_has_a_description(name):
    assert _prompts()[name].description.strip()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("site_selection", {"business_type": True, "area": True}),
        ("compare_neighborhoods", {"area_a": True, "area_b": True}),
        ("plan_errands", {"stops": True, "start": False}),
    ],
)
def test_argument_schemas(name, expected):
    """prompts/list advertises the arguments, and which of them are required."""
    arguments = _prompts()[name].arguments or []
    assert {a.name: bool(a.required) for a in arguments} == expected


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_each_prompt_renders_with_sample_arguments(name):
    for args in SAMPLES[name]:
        text = _render(name, args)
        assert len(text) > 200
        # The arguments actually reach the text — a template that ignored
        # them would still "render".
        for value in args.values():
            if value:
                assert value in text


def test_missing_required_argument_is_an_error():
    with pytest.raises(Exception):
        _render("site_selection", {"business_type": "bakery"})


# --- the guard ------------------------------------------------------------


def test_every_tool_reference_in_every_prompt_is_a_registered_tool():
    """A renamed or deleted tool must not survive as prose in a prompt."""
    registered = _tool_names()
    for name, text in _all_renderings().items():
        referenced = set(_TOOL_REF.findall(text))
        assert referenced, f"prompt {name!r} names no tools at all"
        dangling = sorted(referenced - registered)
        assert not dangling, (
            f"prompt {name!r} tells the agent to call {dangling}, which "
            f"is not registered. Known tools: {', '.join(sorted(registered))}."
        )


def test_no_registered_tool_name_appears_outside_the_guarded_form():
    """Whole-word check: a bare mention would slip past `_TOOL_REF`.

    `find_places` written without the `()` still reads to an agent as an
    instruction to call it, but the guard above would never see it, so a
    rename could leave it dangling. Require the one form.
    """
    registered = _tool_names()
    for name, text in _all_renderings().items():
        stripped = _TOOL_REF.sub("", text)
        bare = sorted(
            tool
            for tool in registered
            if re.search(rf"(?<![a-z0-9_]){re.escape(tool)}(?![a-z0-9_])", stripped)
        )
        assert not bare, (
            f"prompt {name!r} mentions {bare} without the `name()` form the "
            "guard matches on; write tool names as `find_places()`."
        )


def test_declared_tool_tuples_match_what_the_prompts_actually_reference():
    """PROMPTS' third element drives the subset note, so it must not drift."""
    for name, (_fn, _description, declared) in prompts.PROMPTS.items():
        referenced = set(_TOOL_REF.findall(_render(name, SAMPLES[name][0])))
        assert set(declared) == referenced, (
            f"prompt {name!r} declares {sorted(declared)} but its text "
            f"references {sorted(referenced)}."
        )


# --- zero token cost ------------------------------------------------------


def _tools_list_json(register_prompts: bool) -> str:
    """`tools/list` as a client would see it, with prompts registered or not."""
    original = prompts.register
    if not register_prompts:
        prompts.register = lambda *_args, **_kwargs: None
    try:
        tools = asyncio.run(server.build_server(None).list_tools())
    finally:
        prompts.register = original
    return json.dumps(
        [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools],
        sort_keys=True,
    )


def test_tools_list_is_byte_identical_with_and_without_prompts():
    """The premise of the feature: prompts add nothing to every conversation."""
    assert _tools_list_json(True) == _tools_list_json(False)


# --- subset profiles ------------------------------------------------------


@pytest.mark.parametrize("profile", sorted(tool_profiles.PROFILES))
def test_every_prompt_is_registered_under_every_profile(profile):
    """Prompts are always-on: they are not in tools/list, so they cost nothing."""
    assert set(_prompts(profile)) == set(prompts.PROMPTS)


@pytest.mark.parametrize("profile", sorted(tool_profiles.PROFILES))
def test_prompts_still_render_under_a_subset_profile(profile):
    for name, samples in SAMPLES.items():
        assert len(_render(name, samples[0], profile)) > 200


def test_subset_profile_renders_a_note_naming_the_missing_tools():
    text = _render("plan_errands", SAMPLES["plan_errands"][0], "geometry")
    assert "PLACEROOT_TOOLS" in text
    for tool in prompts.PLAN_ERRANDS_TOOLS:
        assert tool in text


def test_full_surface_renders_no_note():
    for text in _all_renderings().values():
        assert "PLACEROOT_TOOLS" not in text


def test_a_profile_that_covers_a_prompt_renders_no_note():
    """analysis covers compare_neighborhoods' tools except the geocoders."""
    note = prompts._profile_note(
        prompts.COMPARE_NEIGHBORHOODS_TOOLS,
        set(prompts.COMPARE_NEIGHBORHOODS_TOOLS),
    )
    assert note == ""
