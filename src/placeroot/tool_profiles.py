"""PLACEROOT_TOOLS: load only the tools an install actually uses (issue #182).

The whole 29-tool surface costs ~12.7k estimated tokens of JSON schema in
every conversation, paid before the agent asks anything. Most installs use
a slice of it. This module is the single registry mapping a profile name to
its tools, plus the parser for the `PLACEROOT_TOOLS` env var; server.py
applies the result at registration time, so tools outside the selection are
never registered and never reach `tools/list`.

Grammar: a comma-separated list whose entries are profile names, tool
names, or the special name `all` — the union of everything named. Unset,
empty, or `all` means the full surface (the default; identical to the
behavior before this existed). The one name that is not part of that union
is `progressive` (issue #210), which must stand alone: see PROGRESSIVE.
"""

# Profile -> the tools it registers. Profiles may overlap, and the union of
# all of them plus ALWAYS_INCLUDED is the complete surface (guarded by a
# test, so a new tool can't quietly belong to no profile).
PROFILES: dict[str, frozenset[str]] = {
    # The single-purpose tools that answer the majority of spatial
    # questions: find something, name<->coordinate in both directions, get
    # an id, get the detail behind an id, characterize an area, get from A
    # to B. Deliberately excludes every batch sibling (a round-trip
    # optimization, not a capability), the buildings/land-use themes, and
    # the geometry/rendering tools.
    #
    # search_categories is here despite being a lookup rather than an
    # answer: find_places' and places_along_route's `category` filter takes
    # Overture taxonomy slugs, and a wrong slug returns zero results with a
    # note telling the agent to check the slug with search_categories. Under
    # a profile that dropped it, core's likeliest failure mode would end at
    # a hint pointing to a tool the agent cannot call.
    "core": frozenset({
        "find_places",
        "geocode",
        "reverse_geocode",
        "place_details",
        "resolve_place",
        "search_categories",
        "summarize_area",
        "route",
        # Composed of two tools core already carries (route + find_places),
        # and its description names both — so this is the profile where
        # those references resolve.
        "places_along_route",
    }),
    # Find/name/identify, including the batch siblings and the category
    # lookup that makes find_places' category filter usable.
    "search": frozenset({
        "find_places",
        "place_details",
        "geocode",
        "geocode_batch",
        "resolve_place",
        "resolve_place_batch",
        "reverse_geocode",
        "reverse_geocode_batch",
        # The address-level half of reverse lookup: reverse_geocode names a
        # point, address_at lists the doorways around it.
        "address_at",
        # ...and its forward twin: an address string back to a coordinate,
        # which geocode cannot answer at doorway granularity.
        "geocode_address",
        "search_categories",
        # Identify: any GERS id back to the entity it names.
        "gers_lookup",
    }),
    # Getting between points, and how far apart things are.
    "routing": frozenset({
        "route",
        "isochrone",
        "distance_matrix",
        "within_distance",
        # Multi-stop ordering over the same street graph route() uses.
        "optimize_route",
    }),
    # Characterizing an area rather than locating a thing in it.
    "analysis": frozenset({
        "summarize_area",
        "summarize_buildings",
        "compare_areas",
        "buildings_at",
        "land_use_at",
        "infrastructure_at",
        # Hydrology is a characterize-the-surroundings question of the same
        # shape as infrastructure_at ("is this parcel waterfront / how far
        # to the nearest canal"), not a find-a-named-thing one, so it lands
        # in analysis rather than search.
        "water_near",
        "admin_lookup",
    }),
    # Working on geometry the caller already has, and turning results into
    # something a human can look at.
    "geometry": frozenset({
        "simplify_geometry",
        "render_map",
    }),
}

# Registered under every profile. data_version is ~180 tokens of schema and
# it is the only way an agent can tell which Overture release backs the
# answers it is being given — orientation that every other answer depends
# on, at a cost too small to be worth making optional.
ALWAYS_INCLUDED: frozenset[str] = frozenset({"data_version"})

# Selects the full surface; also what unset/empty means.
ALL = "all"

# Progressive disclosure (issue #210): instead of a slice of the surface,
# a *meta* surface — a catalog tool plus a dispatcher — that keeps all 29
# tools reachable at the standing cost of three schemas. For the install
# that wants everything available but can't pay 13.2k tokens for it in
# every conversation; profiles need you to know up front which tools you
# want, this doesn't.
PROGRESSIVE = "progressive"

# The meta-tools `progressive` registers. They live in server.py's separate
# meta registry rather than in _TOOL_FUNCS, so they never widen the real
# surface, never belong to a profile, and can't be named individually in
# PLACEROOT_TOOLS — `progressive` is the only way to get them, because a
# dispatcher without its catalog (or vice versa) is not a usable surface.
PROGRESSIVE_TOOLS: frozenset[str] = frozenset({"placeroot_capabilities", "placeroot_call"})


class InvalidToolSelection(ValueError):
    """PLACEROOT_TOOLS named something that is neither a profile nor a tool."""


class InvalidProfileDefinition(ValueError):
    """A PROFILES entry names a tool that does not exist."""


def _check_profile_definitions(known_tools: set[str]) -> None:
    """Every name in every profile must be a real tool.

    A typo inside PROFILES would otherwise just drop that tool from the
    profile: the selection is a union, so the bad name contributes nothing
    and nothing complains. Checked on every resolve(), including the
    default full-surface path, so the failure lands at startup on every
    install rather than only on the ones that select the broken profile.
    """
    bad = {
        name: sorted(set(members) - known_tools)
        for name, members in PROFILES.items()
        if not set(members) <= known_tools
    }
    if bad:
        detail = "; ".join(f"{name}: {', '.join(missing)}" for name, missing in sorted(bad.items()))
        raise InvalidProfileDefinition(
            f"PROFILES names tool(s) that do not exist ({detail}). "
            f"Known tools: {', '.join(sorted(known_tools))}."
        )


def resolve(spec: str | None, known_tools: set[str]) -> set[str]:
    """Tool names to register for `spec`, given the full set of known tools.

    An unset, empty, or all-whitespace spec (and any spec containing `all`)
    resolves to every known tool. Otherwise each comma-separated entry is a
    profile name or a tool name, matched case-insensitively, and the result
    is their union plus ALWAYS_INCLUDED.

    Raises InvalidToolSelection on an entry that matches neither, and
    InvalidProfileDefinition if a profile itself names a tool that doesn't
    exist. Failing loudly is the point: a typo that quietly fell back to
    the full surface would leave the operator paying for the tools they
    meant to drop, with nothing to notice.
    """
    _check_profile_definitions(known_tools)
    if spec is None:
        return set(known_tools)
    entries = [part.strip().lower() for part in spec.split(",")]
    entries = [e for e in entries if e]
    if not entries:
        return set(known_tools)

    # `progressive` replaces the surface rather than adding to it. Honoring
    # a mix would register the meta-tools *on top of* whatever else was
    # named — paying both costs, when the whole point is paying neither the
    # full 13.2k nor a guess at which tools this install needs. So it fails
    # like a typo does, rather than quietly producing a surface nobody asked
    # for.
    if PROGRESSIVE in entries:
        if set(entries) != {PROGRESSIVE}:
            raise InvalidToolSelection(
                f"PLACEROOT_TOOLS={spec.strip()!r} mixes '{PROGRESSIVE}' with other "
                f"name(s): {', '.join(sorted(set(entries) - {PROGRESSIVE}))}. "
                f"'{PROGRESSIVE}' replaces the whole tool surface with the meta-tools "
                f"({', '.join(sorted(PROGRESSIVE_TOOLS))}) and cannot be combined; "
                f"use it on its own, or drop it and name profiles/tools."
            )
        return set(PROGRESSIVE_TOOLS) | (ALWAYS_INCLUDED & known_tools)

    # Validate every entry before honoring `all`: "all,typo" must fail the
    # same way "typo" does, not silently load the full surface — the loud
    # failure on typos is this module's whole contract.
    selected: set[str] = set()
    unknown: list[str] = []
    for entry in entries:
        if entry in PROFILES:
            selected |= PROFILES[entry]
        elif entry in known_tools or entry == ALL:
            selected.add(entry)
        else:
            unknown.append(entry)
    if unknown:
        raise InvalidToolSelection(
            f"PLACEROOT_TOOLS contains unknown name(s): {', '.join(sorted(unknown))}. "
            f"Valid profiles: {', '.join(sorted(PROFILES) + [ALL, PROGRESSIVE])}. "
            f"Valid tool names: {', '.join(sorted(known_tools))}."
        )
    if ALL in selected:
        return set(known_tools)
    return selected | (ALWAYS_INCLUDED & known_tools)
