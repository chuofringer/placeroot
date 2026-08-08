"""PLACEROOT_TOOLS: load only the tools an install actually uses (issue #182).

The whole 22-tool surface costs ~7.4k estimated tokens of JSON schema in
every conversation, paid before the agent asks anything. Most installs use
a slice of it. This module is the single registry mapping a profile name to
its tools, plus the parser for the `PLACEROOT_TOOLS` env var; server.py
applies the result at registration time, so tools outside the selection are
never registered and never reach `tools/list`.

Grammar: a comma-separated list whose entries are profile names, tool
names, or the special name `all` — the union of everything named. Unset,
empty, or `all` means the full surface (the default; identical to the
behavior before this existed).
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
    "core": frozenset({
        "find_places",
        "geocode",
        "reverse_geocode",
        "place_details",
        "resolve_place",
        "summarize_area",
        "route",
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
        "search_categories",
    }),
    # Getting between points, and how far apart things are.
    "routing": frozenset({
        "route",
        "isochrone",
        "distance_matrix",
        "within_distance",
    }),
    # Characterizing an area rather than locating a thing in it.
    "analysis": frozenset({
        "summarize_area",
        "summarize_buildings",
        "compare_areas",
        "buildings_at",
        "land_use_at",
        "admin_lookup",
    }),
    # Working on geometry the caller already has, and turning results into
    # something a human can look at.
    "geometry": frozenset({
        "simplify_geometry",
        "render_map",
    }),
}

# Registered under every profile. data_version is ~120 tokens of schema and
# it is the only way an agent can tell which Overture release backs the
# answers it is being given — orientation that every other answer depends
# on, at a cost too small to be worth making optional.
ALWAYS_INCLUDED: frozenset[str] = frozenset({"data_version"})

# Selects the full surface; also what unset/empty means.
ALL = "all"


class InvalidToolSelection(ValueError):
    """PLACEROOT_TOOLS named something that is neither a profile nor a tool."""


def resolve(spec: str | None, known_tools: set[str]) -> set[str]:
    """Tool names to register for `spec`, given the full set of known tools.

    An unset, empty, or all-whitespace spec (and any spec containing `all`)
    resolves to every known tool. Otherwise each comma-separated entry is a
    profile name or a tool name, matched case-insensitively, and the result
    is their union plus ALWAYS_INCLUDED.

    Raises InvalidToolSelection on an entry that matches neither. Failing
    loudly is the point: a typo that quietly fell back to the full surface
    would leave the operator paying for the tools they meant to drop, with
    nothing to notice.
    """
    if spec is None:
        return set(known_tools)
    entries = [part.strip().lower() for part in spec.split(",")]
    entries = [e for e in entries if e]
    if not entries or ALL in entries:
        return set(known_tools)

    selected: set[str] = set()
    unknown: list[str] = []
    for entry in entries:
        if entry in PROFILES:
            selected |= PROFILES[entry]
        elif entry in known_tools:
            selected.add(entry)
        else:
            unknown.append(entry)
    if unknown:
        raise InvalidToolSelection(
            f"PLACEROOT_TOOLS contains unknown name(s): {', '.join(sorted(unknown))}. "
            f"Valid profiles: {', '.join(sorted(PROFILES) + [ALL])}. "
            f"Valid tool names: {', '.join(sorted(known_tools))}."
        )
    return selected | (ALWAYS_INCLUDED & known_tools)
