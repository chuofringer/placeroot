"""Travel-time-fair meeting point search (issue #306).

Fairness objective, stated plainly here so any agent surfacing this to a
user can explain it in one sentence: a fair meeting point minimizes the
MAXIMUM per-person travel time to it ("no one gets screwed"), tie-broken
by the smaller spread (max - min) across participants, then by the
smaller total of everyone's travel time. It is deliberately not the point
that minimizes the *average* or *total* travel time — that objective can
happily strand one person with a 40-minute trip so two others get a
5-minute one, which is not what "fair" means to the people asking.

This module holds the two pieces of that logic that don't need the
server's request/response layer and so are cheaply unit-testable:
find_center (a seed point for the venue search) and fairness_key (the
ranking rule). server.py's meeting_point tool is the thin validated
wrapper: it parses origins, calls find_center to seed a find_places
search, calls routing.route() once per (origin, candidate) pair for the
real routed numbers, and sorts the results with fairness_key.
"""

from placeroot import geo, routing

# Iterations for find_center's worst-offender walk. A seed for venue
# search, not the final ranked answer (which uses routing.route()'s real
# per-edge times) — a few dozen iterations of a simple heuristic is
# plenty of precision for "search near here."
FIND_CENTER_ITERATIONS = 20

# routing.MODE_CONFIG["drive"]["default_speed_m_s"] is None: drive's real
# cost model is per-edge (posted speed limits / class defaults), not one
# constant. find_center only needs an approximate speed to bias the seed
# point toward whichever participant is slower, so a single representative
# constant stands in for drive here.
_DRIVE_SEED_SPEED_M_S = routing.DRIVE_DEFAULT_CLASS_SPEED_M_S


def _seed_speed_m_s(mode: str) -> float:
    speed = routing.MODE_CONFIG[mode]["default_speed_m_s"]
    return speed if speed is not None else _DRIVE_SEED_SPEED_M_S


def find_center(origins: list[tuple[float, float, str]]) -> tuple[float, float]:
    """A seed point minimizing the max implied travel TIME across origins.

    origins is a list of (lat, lon, mode) tuples. Each origin's "time" to
    a candidate center is approximated as haversine_m / that mode's speed
    — a straight-line stand-in, never a routed number. This point only
    seeds the venue search (find_places around it); the real ranking that
    meeting_point returns uses routing.route()'s exact routed times.

    Starts at the plain (lat, lon) centroid, then FIND_CENTER_ITERATIONS
    times finds the currently worst-off (highest implied-time) origin and
    moves the point a shrinking fraction of the remaining distance toward
    it — a simple subgradient walk toward a time-weighted center. Because
    a slower mode (walk) implies a larger time at the same distance than a
    faster one (drive), this pulls the point closer to the walking
    participant than to a driving one at the same distance — matching the
    fairness objective's "no one gets screwed" framing before routing ever
    runs.
    """
    if not origins:
        raise ValueError("find_center requires at least one origin")
    lat = sum(o[0] for o in origins) / len(origins)
    lon = sum(o[1] for o in origins) / len(origins)
    for i in range(FIND_CENTER_ITERATIONS):
        worst_lat, worst_lon, worst_time = lat, lon, -1.0
        for olat, olon, mode in origins:
            speed = _seed_speed_m_s(mode)
            implied_time = geo.haversine_m(lat, lon, olat, olon) / speed
            if implied_time > worst_time:
                worst_time, worst_lat, worst_lon = implied_time, olat, olon
        step = 0.5 * (0.85**i)
        lat += (worst_lat - lat) * step
        lon += (worst_lon - lon) * step
    return lat, lon


def fairness_key(times_min: list[float]) -> tuple[float, float, float]:
    """(max, spread, total) minutes: the sort key meeting_point ranks candidates by.

    Smaller sorts first, i.e. "more fair": minimize the worst-off
    participant's travel time, tie-break by the smaller max-min spread
    across everyone, then by the smaller sum of everyone's time. See this
    module's docstring for the objective in prose.
    """
    if not times_min:
        raise ValueError("fairness_key requires at least one travel time")
    return (max(times_min), max(times_min) - min(times_min), sum(times_min))
