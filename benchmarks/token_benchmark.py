"""Tokens-per-correct-answer benchmark (issue #26).

GeoBenchX (github.com/Solirinai/GeoBenchX) and GeoAgentBench are agent-with-LLM
benchmarks: they measure an LLM choosing and chaining tools, which needs an
LLM API key this repo doesn't have credentials for. Faking that isn't
honest, so this benchmark measures something narrower but real and runnable
today: for ~30 spatial questions with programmatically checkable answers
(inspired by GeoBenchX's task categories — nearest-POI, area comparison,
point-in-admin, within-distance, isochrone reachability), how many tokens
does placeroot's tool response cost per correct answer, against LIVE
Overture data, compared to the token cost of the equivalent *raw* payload
(unprocessed rows / full geometry) an agent would have had to fetch and
read itself without placeroot's filtering, ranking, and summarizing?

Each task is a Task(name, category, fn) where fn() calls the placeroot
tool(s) that answer the question, runs a checker against the answer, and
also measures the token cost of the raw-payload equivalent over the same
spatial predicate. Both sides use the same len(json)//4 heuristic as
placeroot.budget.estimate_tokens (and examples/site_selection/run_demo.py's
StepLog) — every number here is directly comparable to the server's own
budget accounting.

Honesty rules (documented, not just followed): a task whose checker fails
is counted, not dropped, and shows up in the failure-detail section, not
silently excluded from the aggregate. The raw-payload measurement
methodology is documented per category below rather than left implicit.

Run against live Overture S3 (the only mode this script supports — the
opt-in offline exercise of the harness itself lives in
tests/test_token_benchmark.py against committed fixtures):

    uv run python benchmarks/token_benchmark.py

Each task is 1-3 tool calls; with a warm tile cache this finishes in
minutes. Writes benchmarks/results.md (overwriting the previous run) with
the run date, the active Overture release, the per-task table, and
aggregate accuracy/token-ratio numbers.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from placeroot import divisions, overture, release, routing, server

REPO_ROOT = Path(__file__).resolve().parents[1]


def estimate_tokens(obj) -> int:
    """chars/4 heuristic — identical to placeroot.budget.estimate_tokens."""
    return len(json.dumps(obj, default=str)) // 4


@dataclass
class Outcome:
    correct: bool
    detail: str
    placeroot_tokens: int
    raw_tokens: int
    error: str | None = None

    @property
    def ratio(self) -> float | None:
        """raw_tokens / placeroot_tokens, or None when the task errored
        outright (placeroot_tokens == 0, nothing meaningful to divide by)."""
        if self.error or self.placeroot_tokens == 0:
            return None
        return self.raw_tokens / self.placeroot_tokens


@dataclass
class Task:
    name: str
    category: str
    fn: Callable[[], Outcome]


@dataclass
class TaskRun:
    task: Task
    outcome: Outcome


def run_task(task: Task) -> TaskRun:
    """A raised exception is a scored miss (correct=False), never a crash of
    the whole run — one bad task must not take the rest of the benchmark
    down, and a raised exception is exactly as honest a "wrong answer" as a
    failed checker."""
    try:
        outcome = task.fn()
    except Exception as e:  # noqa: BLE001 - see docstring above
        outcome = Outcome(
            correct=False,
            detail=f"raised {type(e).__name__}: {e}",
            placeroot_tokens=0,
            raw_tokens=0,
            error=str(e),
        )
    return TaskRun(task=task, outcome=outcome)


def run_tasks(tasks: list[Task]) -> list[TaskRun]:
    return [run_task(t) for t in tasks]


# ----------------------------------------------------------------------
# Raw-payload comparator helpers.
#
# For each task category, "raw" is what an agent would have had to fetch
# and read itself without the placeroot tool doing the filtering, ranking,
# point-in-polygon, or graph-building: unprocessed rows or full geometry,
# over the *same* spatial predicate the placeroot tool call used. These
# intentionally reuse overture.py's/divisions.py's/routing.py's private
# query-building helpers (area_geometry, _places_source, _geom_expr, etc.)
# rather than duplicating that SQL, so the raw side is measured against
# exactly the same bbox/radius/predicate the placeroot side answered from.
# ----------------------------------------------------------------------


def _raw_places_rows(
    lat: float, lon: float, radius_m: float, category: str | None = None
) -> list[dict]:
    """Every place in radius_m, every column Overture's places theme carries
    (find_places' raw-rows equivalent: full unprocessed data, not the
    curated id/name/category/distance rows the tool returns)."""
    bbox_filter, distance_filter, params, bbox, _radius_m = overture.area_geometry(
        lat, lon, radius_m
    )
    filters = [bbox_filter, distance_filter]
    if category:
        filters.append("(basic_category ILIKE $category OR taxonomy.primary ILIKE $category)")
        params["category"] = f"%{category}%"
    sql = f"SELECT * FROM {overture._places_source(bbox)[0]} WHERE {' AND '.join(filters)}"
    with overture._conn_lock:
        cur = overture._conn().execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]


def _raw_admin_polygons(lat: float, lon: float) -> list[dict]:
    """Every division polygon containing the point, with full unsimplified
    GeoJSON geometry (admin_lookup's raw equivalent: the full division
    polygon, not the {name, type, id} chain entry the tool returns)."""
    divisions._ensure_spatial()
    upstream = overture._upstream_glob(divisions.THEME, type_="division_area")
    missing = set(overture.missing_columns(upstream, divisions.REQUIRED_COLUMNS))
    geom_expr = divisions._geom_expr(upstream)
    name_expr = "NULL" if "names" in missing else "names.primary"
    subtype_expr = "NULL" if "subtype" in missing else "subtype"
    bbox_prefilter = (
        "bbox.xmin <= $lon AND bbox.xmax >= $lon"
        " AND bbox.ymin <= $lat AND bbox.ymax >= $lat AND "
        if "bbox" not in missing
        else ""
    )
    sql = f"""
        SELECT {name_expr} AS name, {subtype_expr} AS type,
               ST_AsGeoJSON({geom_expr}) AS geometry
        FROM read_parquet('{upstream}', hive_partitioning=1)
        WHERE {bbox_prefilter}ST_Contains({geom_expr}, ST_Point($lon, $lat))
    """
    with overture._conn_lock:
        rows = overture._conn().execute(sql, {"lat": lat, "lon": lon}).fetchall()
    return [{"name": n, "type": t, "geometry": json.loads(g)} for n, t, g in rows]


def _raw_segments(lat: float, lon: float, radius_m: float) -> list[dict]:
    """Every transportation segment in radius_m, full rows plus WKT geometry
    (isochrone's raw equivalent: the street-graph edges it built the answer
    from, not the compact polygon+stats the tool returns). The raw binary
    WKB geometry column is swapped for its WKT text so the row stays
    JSON-serializable without a custom encoder; that's a slight
    *undercount* of the true raw payload, noted here rather than smoothed
    over."""
    xmin, ymin, xmax, ymax = routing._bbox_around(lat, lon, radius_m)
    bbox_filter, params = routing._bbox_filter_sql(xmin, ymin, xmax, ymax)
    upstream = routing._upstream_glob()
    geom_expr = routing._geometry_wkt_expr(upstream)
    sql = f"""
        SELECT * EXCLUDE (geometry), {geom_expr} AS geometry_wkt
        FROM read_parquet('{upstream}', hive_partitioning=1)
        WHERE {bbox_filter}
    """
    with routing._conn_lock:
        cur = routing._conn().execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]


def _isochrone_extraction_radius(
    minutes: float, mode: str, speed_m_s: float | None = None
) -> float:
    """Mirrors routing.isochrone()'s own auto-radius derivation so the raw
    comparator pulls segments from the same extraction radius the tool
    used to build its graph, not an arbitrary one."""
    config = routing.MODE_CONFIG[mode]
    max_seconds = minutes * 60
    const_speed = speed_m_s if speed_m_s is not None else config["default_speed_m_s"]
    buffer_speed = const_speed if const_speed is not None else routing.DRIVE_FASTEST_CLASS_SPEED_M_S
    return min(max_seconds * buffer_speed * routing.RADIUS_BUFFER, config["max_radius_m"])


# ----------------------------------------------------------------------
# Task category: point_in_admin — "what county/region contains this point?"
# Checker: every expected substring appears (case-insensitive) among the
# admin_lookup chain's names. Points are landmark coordinates inside
# official, legally-named counties/regions — stable across Overture
# releases because county boundaries don't move.
# ----------------------------------------------------------------------


def _point_in_admin_task(name: str, lat: float, lon: float, expected: list[str]) -> Task:
    def fn() -> Outcome:
        result = server.admin_lookup(lat, lon)
        if "error" in result:
            return Outcome(False, f"tool error: {result}", 0, 0, error=str(result))
        chain_names = [d["name"] or "" for d in result["chain"]]
        correct = all(any(exp.lower() in n.lower() for n in chain_names) for exp in expected)
        raw = _raw_admin_polygons(lat, lon)
        return Outcome(
            correct=correct,
            detail=f"expected {expected} in chain, got names {chain_names}",
            placeroot_tokens=estimate_tokens(result),
            raw_tokens=estimate_tokens(raw),
        )

    return Task(name=name, category="point_in_admin", fn=fn)


POINT_IN_ADMIN_TASKS = [
    _point_in_admin_task("austin_texas_capitol", 30.2747, -97.7404, ["Travis", "Texas"]),
    _point_in_admin_task("brooklyn_borough_hall", 40.6935, -73.9905, ["Kings", "New York"]),
    _point_in_admin_task("la_city_hall", 34.0537, -118.2428, ["Los Angeles", "California"]),
    _point_in_admin_task("chicago_willis_tower", 41.8789, -87.6359, ["Cook", "Illinois"]),
    _point_in_admin_task("seattle_space_needle", 47.6205, -122.3493, ["King", "Washington"]),
    _point_in_admin_task("miami_downtown", 25.7743, -80.1937, ["Miami-Dade", "Florida"]),
    _point_in_admin_task("denver_state_capitol", 39.7392, -104.9903, ["Denver", "Colorado"]),
]


# ----------------------------------------------------------------------
# Task category: within_distance — "is there a matching place within N
# meters?" True cases are dense downtown cores where a common category is
# effectively guaranteed; False cases are open ocean, desert, and wilderness
# interiors chosen specifically to be far from any settlement.
# ----------------------------------------------------------------------


def _within_distance_task(
    name: str, lat: float, lon: float, max_distance_m: float, category: str | None, expected: bool
) -> Task:
    def fn() -> Outcome:
        result = server.within_distance(lat, lon, max_distance_m, category=category)
        if "error" in result:
            return Outcome(False, f"tool error: {result}", 0, 0, error=str(result))
        correct = result["within"] is expected
        raw = _raw_places_rows(lat, lon, max_distance_m * 2, category=category)
        return Outcome(
            correct=correct,
            detail=(
                f"expected within={expected}, got {result['within']} "
                f"(distance_m={result['distance_m']})"
            ),
            placeroot_tokens=estimate_tokens(result),
            raw_tokens=estimate_tokens(raw),
        )

    return Task(name=name, category="within_distance", fn=fn)


WITHIN_DISTANCE_TASKS = [
    _within_distance_task("times_square_restaurant", 40.7580, -73.9855, 300, "restaurant", True),
    _within_distance_task("chicago_loop_coffee", 41.8827, -87.6233, 400, "coffee_shop", True),
    _within_distance_task(
        "hollywood_highland_restaurant", 34.1016, -118.3269, 300, "restaurant", True
    ),
    _within_distance_task("eiffel_tower_restaurant", 48.8584, 2.2945, 500, "restaurant", True),
    _within_distance_task(
        "yellowstone_lake_grocery", 44.4605, -110.3538, 300, "grocery_store", False
    ),
    _within_distance_task("pacific_ocean_any_place", 5.0, -150.0, 2000, None, False),
    _within_distance_task("sahara_desert_any_place", 23.4162, 11.7000, 2000, None, False),
    _within_distance_task(
        "grand_canyon_remote_restaurant", 36.15, -112.05, 200, "restaurant", False
    ),
]


# ----------------------------------------------------------------------
# Task category: nearest_poi — top-3 coffee_shop results near iconic,
# permanently dense downtown cores should include a Starbucks (a near-
# universal presence in major US downtowns) — a top-3 tolerance band, not
# top-1, specifically to tolerate ordinary data drift between releases.
# ----------------------------------------------------------------------


def _nearest_poi_top3_task(
    name: str, lat: float, lon: float, category: str, expected_names: list[str]
) -> Task:
    def fn() -> Outcome:
        result = server.find_places(lat, lon, radius_m=500, category=category, limit=3)
        if "error" in result:
            return Outcome(False, f"tool error: {result}", 0, 0, error=str(result))
        got_names = [r["name"] or "" for r in result["results"]]
        correct = any(any(exp.lower() in n.lower() for n in got_names) for exp in expected_names)
        raw = _raw_places_rows(lat, lon, 500, category=category)
        return Outcome(
            correct=correct,
            detail=f"expected one of {expected_names} in top-3, got {got_names}",
            placeroot_tokens=estimate_tokens(result),
            raw_tokens=estimate_tokens(raw),
        )

    return Task(name=name, category="nearest_poi", fn=fn)


NEAREST_POI_TASKS = [
    _nearest_poi_top3_task("times_square_coffee", 40.7580, -73.9855, "coffee_shop", ["Starbucks"]),
    _nearest_poi_top3_task("chicago_loop_coffee", 41.8827, -87.6233, "coffee_shop", ["Starbucks"]),
    _nearest_poi_top3_task(
        "seattle_downtown_coffee", 47.6101, -122.3344, "coffee_shop", ["Starbucks"]
    ),
    _nearest_poi_top3_task(
        "sf_union_square_coffee", 37.7879, -122.4075, "coffee_shop", ["Starbucks"]
    ),
    _nearest_poi_top3_task("la_downtown_coffee", 34.0505, -118.2551, "coffee_shop", ["Starbucks"]),
    _nearest_poi_top3_task(
        "boston_downtown_coffee", 42.3555, -71.0601, "coffee_shop", ["Starbucks"]
    ),
]


# ----------------------------------------------------------------------
# Task category: area_comparison — a permanently dense downtown core has
# more total places than a permanently rural/wilderness point at the same
# radius. A coarse, durable fact, not a fragile exact-count check.
# ----------------------------------------------------------------------


def _area_comparison_task(
    name: str,
    label_a: str,
    coord_a: tuple[float, float],
    label_b: str,
    coord_b: tuple[float, float],
    radius_m: float = 800,
) -> Task:
    def fn() -> Outcome:
        areas = [{"lat": coord_a[0], "lon": coord_a[1]}, {"lat": coord_b[0], "lon": coord_b[1]}]
        result = server.compare_areas(areas, radius_m=radius_m)
        if "error" in result:
            return Outcome(False, f"tool error: {result}", 0, 0, error=str(result))
        total_a = result["areas"][0]["total_places"]
        total_b = result["areas"][1]["total_places"]
        correct = total_a > total_b
        raw_a = _raw_places_rows(*coord_a, radius_m)
        raw_b = _raw_places_rows(*coord_b, radius_m)
        return Outcome(
            correct=correct,
            detail=f"{label_a}={total_a} places vs {label_b}={total_b} places",
            placeroot_tokens=estimate_tokens(result),
            raw_tokens=estimate_tokens(raw_a) + estimate_tokens(raw_b),
        )

    return Task(name=name, category="area_comparison", fn=fn)


AREA_COMPARISON_TASKS = [
    _area_comparison_task(
        "manhattan_vs_rural_upstate_ny",
        "Midtown Manhattan", (40.7549, -73.9840),
        "rural upstate NY", (42.6526, -74.9481),
    ),
    _area_comparison_task(
        "chicago_loop_vs_rural_il",
        "Chicago Loop", (41.8827, -87.6233),
        "rural downstate IL", (39.0997, -89.4085),
    ),
    _area_comparison_task(
        "sf_union_sq_vs_sierra_backcountry",
        "SF Union Square", (37.7879, -122.4075),
        "Sierra Nevada backcountry", (37.8651, -119.5383),
    ),
    _area_comparison_task(
        "austin_6th_st_vs_hill_country",
        "Austin 6th Street", (30.2669, -97.7428),
        "Texas Hill Country", (30.2500, -98.8700),
    ),
]


# ----------------------------------------------------------------------
# Task category: isochrone — sanity checks (a well-connected urban point
# reaches a nonzero area) plus two durable physical invariants: a longer
# time budget can only reach an equal or larger area than a shorter one at
# the same mode/point, and a faster mode (drive/cycle) can only reach an
# equal or larger area than walking in the same time at the same point.
# ----------------------------------------------------------------------


def _isochrone_sanity_task(name: str, lat: float, lon: float, minutes: float, mode: str) -> Task:
    def fn() -> Outcome:
        result = server.isochrone(lat, lon, minutes=minutes, mode=mode)
        if "error" in result:
            return Outcome(False, f"tool error: {result}", 0, 0, error=str(result))
        stats = result["stats"]
        correct = stats["reachable_nodes"] > 0 and stats["area_km2"] > 0
        radius = _isochrone_extraction_radius(minutes, mode)
        raw = _raw_segments(lat, lon, radius)
        return Outcome(
            correct=correct,
            detail=f"reachable_nodes={stats['reachable_nodes']}, area_km2={stats['area_km2']}",
            placeroot_tokens=estimate_tokens(result),
            raw_tokens=estimate_tokens(raw),
        )

    return Task(name=name, category="isochrone", fn=fn)


def _isochrone_area_grows_with_time_task(
    name: str, lat: float, lon: float, mode: str, minutes_short: float, minutes_long: float
) -> Task:
    def fn() -> Outcome:
        short = server.isochrone(lat, lon, minutes=minutes_short, mode=mode)
        long_ = server.isochrone(lat, lon, minutes=minutes_long, mode=mode)
        for r in (short, long_):
            if "error" in r:
                return Outcome(False, f"tool error: {r}", 0, 0, error=str(r))
        correct = long_["stats"]["area_km2"] >= short["stats"]["area_km2"]
        radius = _isochrone_extraction_radius(minutes_long, mode)
        raw = _raw_segments(lat, lon, radius)
        return Outcome(
            correct=correct,
            detail=(
                f"{minutes_short}min area_km2={short['stats']['area_km2']} vs "
                f"{minutes_long}min area_km2={long_['stats']['area_km2']}"
            ),
            placeroot_tokens=estimate_tokens(short) + estimate_tokens(long_),
            raw_tokens=estimate_tokens(raw),
        )

    return Task(name=name, category="isochrone", fn=fn)


def _isochrone_mode_faster_task(
    name: str, lat: float, lon: float, minutes: float, mode_slow: str, mode_fast: str
) -> Task:
    def fn() -> Outcome:
        slow = server.isochrone(lat, lon, minutes=minutes, mode=mode_slow)
        fast = server.isochrone(lat, lon, minutes=minutes, mode=mode_fast)
        for r in (slow, fast):
            if "error" in r:
                return Outcome(False, f"tool error: {r}", 0, 0, error=str(r))
        correct = fast["stats"]["area_km2"] >= slow["stats"]["area_km2"]
        radius = _isochrone_extraction_radius(minutes, mode_fast)
        raw = _raw_segments(lat, lon, radius)
        return Outcome(
            correct=correct,
            detail=(
                f"{mode_slow} area_km2={slow['stats']['area_km2']} vs "
                f"{mode_fast} area_km2={fast['stats']['area_km2']}"
            ),
            placeroot_tokens=estimate_tokens(slow) + estimate_tokens(fast),
            raw_tokens=estimate_tokens(raw),
        )

    return Task(name=name, category="isochrone", fn=fn)


ISOCHRONE_TASKS = [
    _isochrone_sanity_task("times_square_walk_15min", 40.7580, -73.9855, 15, "walk"),
    _isochrone_mode_faster_task(
        "times_square_drive_ge_walk_15min", 40.7580, -73.9855, 15, "walk", "drive"
    ),
    _isochrone_area_grows_with_time_task(
        "chicago_loop_walk_10min_vs_20min", 41.8827, -87.6233, "walk", 10, 20
    ),
    _isochrone_mode_faster_task(
        "sf_union_sq_cycle_ge_walk_15min", 37.7879, -122.4075, 15, "walk", "cycle"
    ),
    _isochrone_sanity_task("austin_downtown_drive_15min", 30.2669, -97.7428, 15, "drive"),
]


ALL_TASKS: list[Task] = [
    *POINT_IN_ADMIN_TASKS,
    *WITHIN_DISTANCE_TASKS,
    *NEAREST_POI_TASKS,
    *AREA_COMPARISON_TASKS,
    *ISOCHRONE_TASKS,
]


def summarize(runs: list[TaskRun]) -> dict:
    n = len(runs)
    n_correct = sum(1 for r in runs if r.outcome.correct)
    ratios = [r.outcome.ratio for r in runs if r.outcome.ratio is not None]
    return {
        "n_tasks": n,
        "n_correct": n_correct,
        "accuracy": (n_correct / n) if n else 0.0,
        "n_ratios": len(ratios),
        "median_ratio": statistics.median(ratios) if ratios else None,
        "mean_ratio": statistics.mean(ratios) if ratios else None,
    }


def _fmt_ratio(r: float | None) -> str:
    return f"{r:.1f}x" if r is not None else "n/a"


def render_table(runs: list[TaskRun]) -> str:
    lines = [
        "| task | category | correct | placeroot tokens | raw tokens | ratio |",
        "|---|---|---|---:|---:|---:|",
    ]
    for r in runs:
        o = r.outcome
        mark = "yes" if o.correct else "**NO**"
        lines.append(
            f"| {r.task.name} | {r.task.category} | {mark} | {o.placeroot_tokens} | "
            f"{o.raw_tokens} | {_fmt_ratio(o.ratio)} |"
        )
    return "\n".join(lines)


def render_report(runs: list[TaskRun], release_str: str) -> str:
    summary = summarize(runs)
    lines = [
        "# Token benchmark results (issue #26)",
        "",
        f"- Run date: {time.strftime('%Y-%m-%d')}",
        f"- Overture release: {release_str}",
        f"- Tasks: {summary['n_tasks']}",
        f"- Accuracy: {summary['n_correct']}/{summary['n_tasks']} "
        f"({100 * summary['accuracy']:.1f}%)",
        f"- Median tokens ratio (raw / placeroot), over {summary['n_ratios']} scored tasks: "
        f"{_fmt_ratio(summary['median_ratio'])}",
        f"- Mean tokens ratio: {_fmt_ratio(summary['mean_ratio'])}",
        "",
        "Every task that ran is in the table below, including failures — "
        "nothing is dropped from the aggregate.",
        "",
        "## Per-task results",
        "",
        render_table(runs),
        "",
        "## Failure detail",
        "",
    ]
    failures = [r for r in runs if not r.outcome.correct]
    if not failures:
        lines.append("None — every task's checker passed.")
    else:
        for r in failures:
            lines.append(f"- **{r.task.name}** ({r.task.category}): {r.outcome.detail}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    overture.conn()  # warm the shared connection, same as scripts/geocode_benchmark.py
    t0 = time.time()
    runs = run_tasks(ALL_TASKS)
    elapsed = time.time() - t0
    active_release = release.resolve_release()
    report = render_report(runs, active_release)
    print(report)
    print(f"\n(elapsed: {elapsed:.1f}s)")
    results_path = REPO_ROOT / "benchmarks" / "results.md"
    results_path.write_text(report)
    print(f"\nWrote {results_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
