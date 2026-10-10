"""Local materialised Overture name tables (divisions, alt and lang names) and builds."""

import os
import sys as _sys
import tempfile
import threading
import time
from functools import lru_cache
from importlib import resources
from pathlib import Path

import duckdb

from placeroot import progress, release

_pkg = _sys.modules["placeroot.geocode"]


# --- #43: local divisions name table -----------------------------------


def _local_divisions_table_path(active_release: str) -> Path:
    return (
        _pkg.cache.cache_dir()
        / active_release
        / _pkg._DIVISIONS_TABLE_SUBDIR
        / _pkg._DIVISIONS_TABLE_FILENAME
    )  # noqa: E501


def _unique_tmp_path(path: Path) -> Path:
    """A fresh, uniquely named `.tmp` sibling of `path` for a COPY to write.

    Never the same name twice in one directory (tempfile.mkstemp), so two
    builds that overlap — a background build or stage-2 upgrade beside a
    foreground #224 rebuild — write separate files and meet only at the
    atomic os.replace, where the later one wins with identical contents.
    The shared `table.parquet.tmp` name they used before meant one COPY
    could overwrite the other's half-written file mid-stream.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    os.close(fd)
    return Path(name)


def _copy_and_publish(con: duckdb.DuckDBPyConnection, sql: str, tmp_path: Path, path: Path) -> None:
    """Run the COPY in `sql` (which writes `tmp_path`) and publish it as
    `path`; a failed COPY leaves no orphaned temp file behind."""
    try:
        con.execute(sql)
        _publish_copied_parquet(con, tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _clear_table_derived_caches() -> None:
    """Drop every in-process memo derived from a local table's contents:
    called when a table is (re)published and from clear_resolve_session."""
    _pkg._region_population_lookup_cached.cache_clear()
    _pkg._division_named_exactly_cached.cache_clear()
    with _pkg._anchor_memo_lock:
        _pkg._anchor_memo.clear()


def _publish_copied_parquet(con: duckdb.DuckDBPyConnection, tmp_path: Path, path: Path) -> None:
    """Close the COPY writer, publish `tmp_path` as `path`, drop stale cache.

    `_new_connection()` is a cursor of the shared DuckDB instance.
    COPY (FORMAT PARQUET, COMPRESSION ZSTD) can leave that cursor's
    output handle open until close, so we close and fsync before the
    rename. That is not enough on its own: DuckDB 1.5's external file
    cache (``enable_object_cache`` is a no-op placeholder) keys pages
    by path, and a same-path replace after a prior ``read_parquet`` —
    the #214 rebuild, or a stage-2 upgrade — can leave VALIDATE_ALL
    serving old zstd frames. The next ``read_parquet`` on
    ``overture.conn()`` then fails with ZSTD Decompression failure
    even though a fresh connection can read the new file. Toggling
    ``enable_external_file_cache`` empties the cache; DuckDB's own
    tests use the same toggle.
    """
    con.close()
    # Durability before the rename: flush the COPY's bytes to disk. Windows'
    # FlushFileBuffers needs a writable handle (a read-only one fails with
    # EBADF), and fsync is best-effort there anyway, so open read-write and
    # treat a flush failure as non-fatal — the rename below is what publishes.
    fd = os.open(tmp_path, os.O_RDWR if os.name == "nt" else os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        if os.name != "nt":
            raise
    finally:
        os.close(fd)
    os.replace(tmp_path, path)
    with _pkg.overture._conn_lock:
        shared = _pkg.overture.conn()
        shared.execute("SET enable_external_file_cache=false")
        shared.execute("SET enable_external_file_cache=true")
    # The table at `path` just changed: anything memoized from its rows
    # (region populations, exact-name probes, anchor derivations) is stale.
    _clear_table_derived_caches()


def _materialize_alt_names_table(path: Path, glob: str) -> None:
    """COPY the #214 alternate-name table — one row per (division id, folded
    `names.common` spelling) — into a local parquet at `path`.

    Shape is deliberately narrow: `id`, the folded name the ILIKE runs
    against, and one display spelling for `matched_name`. Everything else the
    result row needs comes from joining `id` back to the primary divisions
    table, so this stays the small side of the join.

    Three reductions keep it that way, all done here rather than at query
    time. GROUP BY (id, folded) collapses the many languages that agree on a
    spelling — Overture stores "Munich" separately for en/fr/it/… — into one
    row. Alternates that fold to the same string as the primary name are
    dropped: the literal search already finds those, and a duplicate row
    would only have to be de-duplicated again per query. And the fold itself
    (lower + strip_accents + _UNFOLDED_LETTERS) is applied once per alternate
    here instead of once per alternate per query.

    Measured live on release 2026-07-22.0: 4.29M raw alternates fold down to
    2.49M rows across 1.39M divisions; 15.6s added to the one-time build,
    95.3MB ZSTD on disk against the primary table's 197.1MB, and 0.17-0.42s
    per join lookup ("Vienna" .. "Tokyo"). Dropping alt_display would make it
    71.6MB at the same build time — 24MB is what naming the matched spelling
    in the answer costs, and it is worth it: without it `matched_name` can
    only echo the folded lowercase form.

    Raises duckdb.Error if names.common isn't there or isn't a map — the
    caller treats that as "no alt table" and searches primary names only.
    """
    tmp_path = _pkg._unique_tmp_path(path)
    con = _pkg.overture._new_connection()
    sql = f"""
        COPY (
            SELECT id, alt_name, min(alt) AS alt_display
            FROM (
                SELECT id, alt, primary_folded, {_pkg._fold_alt_name_sql("alt")} AS alt_name
                FROM (
                    SELECT id,
                           {_pkg._fold_alt_name_sql("names.primary")} AS primary_folded,
                           unnest(map_values(names.common)) AS alt
                    FROM read_parquet('{glob}', hive_partitioning=1)
                    WHERE names.common IS NOT NULL
                )
            )
            WHERE alt_name IS NOT NULL AND alt_name <> ''
              AND alt_name IS DISTINCT FROM primary_folded
            GROUP BY id, alt_name
        ) TO '{tmp_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """
    _copy_and_publish(con, sql, tmp_path, path)


def _materialize_lang_names_table(path: Path, glob: str) -> None:
    """COPY the #410 language-tagged name table — one row per (division id,
    names.common language key) — into a local parquet at `path`.

    Reads the same `names.common` map the #214 alt-name table (above) reads,
    but keeps the map key (the language code) instead of discarding it, and
    does not fold/dedupe by spelling — a lang lookup wants "the row for this
    exact id and this exact language", not "some spelling near this string".
    One extra pass over names.common at materialization time (a one-time
    cost, alongside the alt-name table's own pass); the per-request cost
    this buys is a single indexed join by (id, lang) against a small local
    parquet — see _lang_variants_for, not a second scan of Overture data.

    Raises duckdb.Error if names.common isn't there or isn't a map — same
    convention as _materialize_alt_names_table; the caller treats that as
    "no lang table" and geocode answers with primary names only.
    """
    tmp_path = _pkg._unique_tmp_path(path)
    con = _pkg.overture._new_connection()
    sql = f"""
        COPY (
            SELECT id, lower(entry.key) AS lang, entry.value AS name
            FROM (
                SELECT id, unnest(map_entries(names.common)) AS entry
                FROM read_parquet('{glob}', hive_partitioning=1)
                WHERE names.common IS NOT NULL
            )
            WHERE entry.value IS NOT NULL AND entry.value <> ''
        ) TO '{tmp_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """
    _copy_and_publish(con, sql, tmp_path, path)


def _is_remote_glob(glob: str) -> bool:
    return glob.startswith(("s3://", "http://", "https://"))


def _stage1_sentinel(path: Path) -> Path:
    return path.with_suffix(".stage1")


def _materialize_divisions_table(path: Path, glob: str) -> None:
    """COPY just the columns geocode.py needs, for every type=division row, into
    a local parquet file at `path`. Raises UpstreamUnavailable/duckdb.Error on
    failure — callers treat that as "materialization failed" and fall back to
    direct upstream scans, same as a missing/incompatible schema.

    Remote datasets build in two stages. The blocking first-call build
    skips `hierarchies` (145MB of a ~394MB read — and DuckDB's parquet
    reader cannot prune a struct subfield through list_transform/unnest,
    measured, so referencing only x.name still downloads all of it) and
    the alternate-name table, cutting the first name search's cold cost
    roughly in half. A background upgrade then rebuilds the full table —
    admin chains and alt names — and atomically replaces it; until it
    lands, rows carry admin_chain NULL and geocode answers with
    admin_context []. A .stage1 sentinel beside the table marks the
    pending upgrade so a process that dies mid-upgrade resumes it on the
    next geocode. Local datasets (pinned fixtures, mirrors on disk) build
    the full table in one pass exactly as before — staging exists for the
    network, not for them.

    The #214 alternate-name half is best-effort either way: `names.common`
    is an optional nested field this module can't probe for (probe_schema
    only sees top-level columns), and losing alternate-name search is a
    much smaller loss than losing the local table entirely — so a failure
    there is logged and swallowed, leaving the install on primary names
    only.

    #224 adds the four bbox corners as their own columns. They are internal
    (nothing in a tool response reads them) and, on today's data, degenerate —
    see the module docstring's #224 section and _division_bbox. `bbox` is
    required by the theme, so unlike `population`/`hierarchies` they need no
    probe_schema guard: a dataset without it fails the existing `names` check
    or the COPY itself, both of which the caller already handles.
    """
    if _pkg._is_remote_glob(glob):
        _materialize_divisions_pass(path, glob, with_hierarchies=False)
        _pkg._stage1_sentinel(path).touch()
        _pkg._spawn_divisions_upgrade(path, glob)
    else:
        _materialize_divisions_pass(path, glob, with_hierarchies=True)
        _pkg._try_materialize_alt_names_table(path.with_name(_pkg._ALT_NAMES_TABLE_FILENAME), glob)
        _pkg._try_materialize_lang_names_table(
            path.with_name(_pkg._LANG_NAMES_TABLE_FILENAME), glob
        )  # noqa: E501


def _materialize_divisions_pass(path: Path, glob: str, with_hierarchies: bool) -> None:
    """One COPY of the local divisions table; see _materialize_divisions_table."""
    cols = _pkg.overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        raise _pkg.overture.UpstreamUnavailable("divisions dataset missing 'names' column")
    population_expr = "population" if cols is None or "population" in cols else "NULL AS population"
    hierarchies_expr = (
        "list_transform(hierarchies[1], x -> x.name) AS admin_chain"
        if with_hierarchies and (cols is None or "hierarchies" in cols)
        else "NULL::VARCHAR[] AS admin_chain"
    )
    region_expr = "region" if cols is None or "region" in cols else "NULL AS region"
    tmp_path = _pkg._unique_tmp_path(path)
    con = _pkg.overture._new_connection()
    sql = f"""
        COPY (
            SELECT id, names.primary AS name, subtype, country, {region_expr},
                   bbox.ymin AS lat, bbox.xmin AS lon, {population_expr},
                   {hierarchies_expr},
                   bbox.xmin AS bbox_xmin, bbox.ymin AS bbox_ymin,
                   bbox.xmax AS bbox_xmax, bbox.ymax AS bbox_ymax
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE names.primary IS NOT NULL
        ) TO '{tmp_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """
    _copy_and_publish(con, sql, tmp_path, path)


# Upgrade threads already started this process, keyed by table path — one
# upgrade per table at a time; a failed upgrade discards its key so the next
# geocode retries.
_UPGRADE_DELAY_S = 20.0


# Full-table builds already started this process, keyed by table path.
_build_started: set[str] = set()

_build_lock = threading.Lock()


# Serializes the *blocking* (foreground) divisions-table builds — the
# unbundled-release first call in _local_divisions_table and the #224 bbox
# rebuild. _build_lock above only dedups the background spawn; the blocking
# COPY itself never held any lock, so two threads resolving names in
# parallel (from_to's _resolve_pair runs its workers on isolated cursors,
# see db.isolated_reads) could both run _materialize_divisions_table
# against the same table.parquet.tmp and race the final rename.
_blocking_build_lock = threading.Lock()


def _spawn_divisions_build(path: Path, glob: str) -> None:
    """Build the full local table in the background (stage-0 index serving
    meanwhile). Delayed and gated on the fetch slots exactly like the
    stage-2 upgrade, and for the same measured reason: an undelayed
    background read of hundreds of MB starves the request that spawned it.
    """
    key = str(path)
    with _build_lock:
        if key in _build_started:
            return
        _build_started.add(key)

    def _run():
        try:
            time.sleep(_UPGRADE_DELAY_S)
            with _pkg.cache._background_fetch_slots:
                # glob was captured at spawn: re-resolving here, 20s later,
                # could follow a background release rollover and materialize
                # a NEWER release's rows into the pinned release's table
                # path — wrong-vintage data served for the install's life.
                # Under the blocking-build lock like every other COPY of
                # this table: a foreground #224 rebuild or an unbundled
                # first-call build must never run beside this one.
                with _blocking_build_lock:
                    if not path.exists():
                        _pkg._materialize_divisions_table(path, glob)
                        _pkg.logger.info(
                            "full divisions table built behind the bundled index -> %s", path
                        )
        except Exception as e:  # noqa: BLE001 - background build must never surface
            _pkg.logger.warning("background divisions build failed (next geocode retries): %s", e)
            with _build_lock:
                _build_started.discard(key)

    threading.Thread(target=_run, daemon=True).start()


_upgrade_started: set[str] = set()

_upgrade_lock = threading.Lock()


def _spawn_divisions_upgrade(path: Path, glob: str) -> None:
    key = str(path)
    with _upgrade_lock:
        if key in _upgrade_started:
            return
        _upgrade_started.add(key)

    def _run():
        try:
            # Let the request that triggered this build finish first: the
            # upgrade re-reads ~394MB and, started immediately, it starved
            # the very call it was spawned from (geocode_address measured
            # 126s vs 26s with the delay — the same self-starvation the
            # tile fetch semaphore exists for, which also gates this).
            time.sleep(_UPGRADE_DELAY_S)
            with _pkg.cache._background_fetch_slots, _blocking_build_lock:
                _pkg._upgrade_divisions_table(path, glob)
        except Exception as e:  # noqa: BLE001 - background upgrade must never surface
            _pkg.logger.warning("divisions table upgrade failed (next geocode retries): %s", e)
            with _upgrade_lock:
                _upgrade_started.discard(key)

    threading.Thread(target=_run, daemon=True).start()


def _upgrade_divisions_table(path: Path, glob: str) -> None:
    """Stage 2: rebuild the full table (admin chains) and the alt-name table,
    atomically replacing stage 1, then clear the sentinel."""
    t0 = time.time()
    _materialize_divisions_pass(path, glob, with_hierarchies=True)
    _pkg._try_materialize_alt_names_table(path.with_name(_pkg._ALT_NAMES_TABLE_FILENAME), glob)
    _pkg._try_materialize_lang_names_table(path.with_name(_pkg._LANG_NAMES_TABLE_FILENAME), glob)
    _pkg._stage1_sentinel(path).unlink(missing_ok=True)
    _pkg.logger.info(
        "divisions table upgraded with admin chains in %.1fs -> %s",
        time.time() - t0,
        path,
    )


# #214: releases whose alt table this process has already tried (and failed)
# to build. A cache directory written before this feature has a divisions
# table but no alt table; rather than leaving those installs on primary-only
# search until the next Overture release rolls the cache directory over, the
# alt half is built on its own the first time it is found missing. Bounded to
# one attempt per release per process so a persistent failure (no network, a
# release without names.common) costs one try, not one per geocode call.
_ALT_BUILD_ATTEMPTED: set[str] = set()


def _try_materialize_alt_names_table(alt_path: Path, glob: str) -> None:
    """Build the alt table, logging and swallowing any failure — see
    _materialize_divisions_table on why this half is best-effort.

    OSError is caught alongside the query errors because the filesystem half
    of the build (mkdir, and the tmp-file rename) fails on its own terms: a
    read-only or full cache directory raises OSError, not duckdb.Error, and
    every caller of this treats a failed alt build as "search primary names
    only". Letting it escape would take down a geocode call whose primary
    table had already been written successfully.
    """
    t0 = time.time()
    try:
        _pkg._materialize_alt_names_table(alt_path, glob)
    except (duckdb.Error, _pkg.overture.UpstreamUnavailable, OSError) as e:
        _pkg.logger.warning(
            "alternate-name table materialization failed, geocode will search "
            "primary names only: %s",
            e,
        )
        return
    _pkg.logger.info("alternate-name table materialized in %.1fs -> %s", time.time() - t0, alt_path)


_LANG_BUILD_ATTEMPTED: set[str] = set()


def _try_materialize_lang_names_table(lang_path: Path, glob: str) -> None:
    """Build the #410 lang table, logging and swallowing any failure — same
    reasoning as _try_materialize_alt_names_table: losing this table means
    geocode/resolve_place/place_details answer with primary names only,
    which is a much smaller loss than failing the call that triggered the
    build.
    """
    t0 = time.time()
    try:
        _materialize_lang_names_table(lang_path, glob)
    except (duckdb.Error, _pkg.overture.UpstreamUnavailable, OSError) as e:
        _pkg.logger.warning(
            "language-tagged name table materialization failed, lang lookups "
            "will find no variant: %s",
            e,
        )
        return
    _pkg.logger.info(
        "language-tagged name table materialized in %.1fs -> %s", time.time() - t0, lang_path
    )


def _local_lang_names_table(local_table: str | None) -> str | None:
    """Path to the #410 language-tagged name table sitting beside
    `local_table`, or None if there isn't one — same "no lang variants
    available" convention as _local_alt_names_table's None (cache off, a
    cache directory predating #410, a dataset without names.common, or a
    failed best-effort build).
    """
    if local_table is None:
        return None
    path = Path(local_table).with_name(_pkg._LANG_NAMES_TABLE_FILENAME)
    if path.exists():
        return str(path)
    key = str(path)
    # Check-and-add and the build itself both under _blocking_build_lock:
    # two parallel resolves (from_to's isolated workers) could otherwise
    # both see the key missing and run the same COPY side by side.
    with _blocking_build_lock:
        if path.exists():
            return str(path)
        if key in _pkg._LANG_BUILD_ATTEMPTED:
            return None
        _pkg._LANG_BUILD_ATTEMPTED.add(key)
        _pkg.logger.info(
            "no language-tagged name table at %s (cache predates #410); building it", path
        )  # noqa: E501
        _pkg._try_materialize_lang_names_table(
            path, _pkg.overture.upstream_glob(theme="divisions", type_="division")
        )
        if path.exists():
            return str(path)
    return None


def _lang_variants_for(lang_table: str | None, ids: list[str], lang: str) -> dict[str, str]:
    """{id: variant name} for every id in `ids` that has a names.common entry
    under `lang` in `lang_table`. One indexed lookup for the whole batch of
    result rows — not one query per row — so applying #410's lang preference
    to a page of results costs one extra small local-parquet scan, not N.

    Empty dict (never an error) when there's no lang table, no ids, or the
    id/lang combination just isn't present — all of which mean the same
    thing to the caller: no variant, primary name stands.
    """
    if not lang_table or not ids:
        return {}
    sql = f"""
        SELECT id, name
        FROM read_parquet('{lang_table}')
        WHERE lang = $lang AND id IN (SELECT unnest($ids))
    """
    try:
        with _pkg.overture._conn_lock:
            rows = _pkg.overture.conn().execute(sql, {"lang": lang, "ids": ids}).fetchall()
    except duckdb.Error:
        return {}
    return dict(rows)


def _local_alt_names_table(local_table: str | None) -> str | None:
    """Path to the #214 alternate-name table sitting beside `local_table`, or
    None if there isn't one.

    None is not an error state: no local divisions table at all (cache off),
    a cache directory written before this feature, a dataset without
    names.common, or a failed best-effort build all land here, and every one
    of them means the same thing to the caller — search primary names only.
    """
    if local_table is None:
        return None
    path = Path(local_table).with_name(_pkg._ALT_NAMES_TABLE_FILENAME)
    if path.exists():
        return str(path)
    key = str(path)
    # Check-and-add and the build itself both under _blocking_build_lock —
    # see _local_lang_names_table.
    with _blocking_build_lock:
        if path.exists():
            return str(path)
        if key in _pkg._ALT_BUILD_ATTEMPTED:
            return None
        _pkg._ALT_BUILD_ATTEMPTED.add(key)
        _pkg.logger.info("no alternate-name table at %s (cache predates #214); building it", path)
        _pkg._try_materialize_alt_names_table(
            path, _pkg.overture.upstream_glob(theme="divisions", type_="division")
        )
        if path.exists():
            return str(path)
    return None


# #224: divisions tables this process has already checked for the bbox columns
# (and, if they were missing, tried once to rebuild). Same shape and same
# reasoning as _ALT_BUILD_ATTEMPTED above — an existing cache directory has a
# divisions table without the bbox columns, and rather than leaving it that way
# until the next Overture release rolls the cache over, it is rebuilt in place
# the first time the columns are found missing. Bounded to one attempt per
# release per process so a persistent failure costs one try, not one per
# geocode call.
_DIVISIONS_BBOX_CHECKED: set[str] = set()


def _divisions_table_has_bbox(path: Path) -> bool:
    """Whether the materialized table at `path` carries the #224 bbox columns.

    False for any table written before #224, and also for an unreadable one —
    both mean the same thing to the caller (rebuild if you haven't yet, and
    treat bbox lookups as unavailable either way).
    """
    try:
        with _pkg.overture._conn_lock:
            cols = (
                _pkg.overture.conn()
                .execute(f"SELECT * FROM read_parquet('{path}') LIMIT 0")
                .description
            )
    except duckdb.Error:
        return False
    names = {c[0] for c in cols or []}
    return all(c in names for c in _pkg._DIVISIONS_BBOX_COLUMNS)


def _division_bbox(
    local_table: str | None, division_id: str
) -> tuple[float, float, float, float] | None:
    """(xmin, ymin, xmax, ymax) for `division_id`, or None.

    INTERNAL — #225 (street-level address search) is the intended consumer; no
    tool response exposes this. None means "no usable extent", which covers
    four cases the caller must treat identically: no local table (cache off),
    a table predating #224, an unknown id, and — on release 2026-07-22.0, for
    *every* division row measured — a bbox whose span in *either* axis is
    below _DEGENERATE_BBOX_SPAN_DEG, i.e. something that bounds nothing rather
    than an extent. That last test is per-axis, so a wide-but-flat box and an
    inverted (antimeridian-crossing) one are rejected alongside a point.

    That last case is the normal one today, not an edge case: division rows
    are points and their bbox is the point's float32 rounding envelope. A
    caller needing a real city extent must join divisions/type=division_area
    on its `division_id` column instead. See the module docstring's #224
    section for the measurements and for why that join is not done here.
    """
    if not local_table:
        return None
    sql = f"""
        SELECT bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax
        FROM read_parquet('{local_table}') WHERE id = $id LIMIT 1
    """
    try:
        with _pkg.overture._conn_lock:
            row = _pkg.overture.conn().execute(sql, {"id": division_id}).fetchone()
    except duckdb.Error:
        # Pre-#224 table (no such columns), or an unreadable one.
        return None
    if row is None or any(v is None for v in row):
        return None
    xmin, ymin, xmax, ymax = (float(v) for v in row)
    # Either axis below the floor is enough: a box that is wide but flat bounds
    # nothing, exactly like a point does. A negative span (xmin > xmax, i.e. an
    # antimeridian-crossing row stored unwrapped) is below the floor too and
    # lands in this branch deliberately — callers bound scans with a plain
    # `BETWEEN xmin AND xmax`, which an inverted box turns into a silently
    # empty range. Supporting wrapped extents means splitting the box at ±180,
    # not passing it through.
    if (xmax - xmin) < _pkg._DEGENERATE_BBOX_SPAN_DEG or (
        ymax - ymin
    ) < _pkg._DEGENERATE_BBOX_SPAN_DEG:  # noqa: E501
        return None
    return xmin, ymin, xmax, ymax


def _rebuild_once_for_bbox_columns(path: Path) -> None:
    """Rebuild a pre-#224 divisions table in place so it carries the bbox
    columns, at most once per release per process.

    Failure is logged and swallowed: the existing table is still a perfectly
    good name table, and the only thing missing without the rebuild is a bbox
    lookup that returns None today anyway. Note this also refreshes the #214
    alt table, since _materialize_divisions_table writes both from the one
    upstream read.
    """
    key = str(path)
    if key in _pkg._DIVISIONS_BBOX_CHECKED:
        return
    with _blocking_build_lock:
        # Re-checked under the lock so two parallel resolves (from_to's
        # isolated workers, see db.isolated_reads) can't both
        # probe-and-rebuild the same table; the whole probe-and-rebuild
        # holds the lock so the COPY and rename never race.
        if key in _pkg._DIVISIONS_BBOX_CHECKED:
            return
        # Recorded before the check, not after, so the schema probe is also
        # once per process: the common case is a table that already has the
        # columns, and re-probing it on every geocode call would be pure
        # overhead.
        _pkg._DIVISIONS_BBOX_CHECKED.add(key)
        if _divisions_table_has_bbox(path):
            return
        _pkg.logger.info(
            "divisions table at %s predates #224 (no bbox columns); rebuilding it", path
        )
        t0 = time.time()
        try:
            # Resolved here rather than by the caller: a warm table must not so
            # much as name the upstream dataset (see #215's
            # test_fuzzy_pass_never_scans_upstream), and this is the one branch
            # that genuinely needs it. _local_alt_names_table resolves it the
            # same way, for the same reason.
            _pkg._materialize_divisions_table(
                path, _pkg.overture.upstream_glob(theme="divisions", type_="division")
            )
        except (duckdb.Error, _pkg.overture.UpstreamUnavailable) as e:
            _pkg.logger.warning(
                "divisions table rebuild for bbox columns failed, keeping the "
                "existing table (bbox lookups stay unavailable): %s",
                e,
            )
            return
    _pkg.logger.info("divisions table rebuilt with bbox columns in %.1fs", time.time() - t0)


def _bundled_index_path(active_release: str) -> Path | None:
    """The wheel-bundled stage-0 geocode index for active_release, or None.

    ~150k most-populous divisions in the exact schema
    _query_divisions_from_local reads (scripts/build_geocode_index.py) —
    every city and town anyone geocodes or anchors on, locally, before
    the full table has ever been built.
    """
    try:
        p = (
            resources.files("placeroot")
            / "data"
            / "geocode-index"
            / active_release
            / "table.parquet"
        )
        return Path(str(p)) if p.is_file() else None
    except (OSError, TypeError):
        return None


def _is_bundled_table(table_path: str | None) -> bool:
    return bool(table_path) and "geocode-index" in str(table_path)


def _local_divisions_table() -> str | None:
    """Path to the local divisions name table for the active release.

    Resolution order, fastest first:
    1. The materialized full table (built earlier this install).
    2. The wheel-bundled stage-0 index for a bundled release: answers the
       populous-division queries — which is nearly all of them — locally
       and instantly, while the full build runs in the background (kicked
       here, delayed and fetch-slot-gated like the stage-2 upgrade, so it
       never starves the query that triggered it). Long-tail names absent
       from the index fall back per-query to a direct upstream scan (see
       geocode_detailed) until the full table lands.
    3. Blocking build (unbundled release), the pre-index behavior.

    Returns None if caching is off (PLACEROOT_CACHE=off) and no bundled
    index applies, or materialization fails — callers fall back to direct
    upstream scans, the pre-#43 behavior.
    """
    active_release = release.resolve_release()
    divisions_pinned = _pkg.overture.dataset_is_pinned("divisions", "division")
    if not _pkg.cache.enabled():
        # The bundled index is real-release data; a deployment that pinned
        # divisions to its own dataset (fixtures, an extract) must never be
        # answered from it.
        if divisions_pinned:
            return None
        bundled = _bundled_index_path(active_release)
        return str(bundled) if bundled else None
    path = _pkg._local_divisions_table_path(active_release)
    if not path.exists():
        bundled = _bundled_index_path(active_release)
        if bundled is not None and not divisions_pinned:
            _spawn_divisions_build(
                path, _pkg.overture.upstream_glob(theme="divisions", type_="division")
            )
            return str(bundled)
    if path.exists():
        _rebuild_once_for_bbox_columns(path)
        if _pkg._stage1_sentinel(path).exists():
            # A stage-1 table whose upgrade never finished (process died
            # mid-upgrade): resume it in the background; this call answers
            # from stage 1 meanwhile (admin_context [] until it lands).
            _pkg._spawn_divisions_upgrade(
                path, _pkg.overture.upstream_glob(theme="divisions", type_="division")
            )
        return str(path)
    glob = _pkg.overture.upstream_glob(theme="divisions", type_="division")
    progress.report(
        "Building the place-name index (one-time per data release) — this "
        "first name search is slow; every search after it answers instantly",
        eta_s=progress.DIVISIONS_INDEX_S,
    )
    _pkg.logger.info(
        "materializing local divisions name table for release %s "
        "(first geocode call this process; one-time cost per release)",
        active_release,
    )
    t0 = time.time()
    try:
        with _blocking_build_lock:
            # A parallel resolve may have finished the build while this
            # thread waited on the lock.
            if not path.exists():
                _pkg._materialize_divisions_table(path, glob)
    except (duckdb.Error, _pkg.overture.UpstreamUnavailable) as e:
        _pkg.logger.warning(
            "local divisions table materialization failed, falling back to "
            "direct upstream scans: %s",
            e,
        )
        return None
    _pkg.logger.info("local divisions table materialized in %.1fs -> %s", time.time() - t0, path)
    return str(path)


def _region_population_lookup(local_table: str | None) -> dict[str, int]:
    """region ISO/local code -> population, from region-subtype rows of the
    local divisions table. Empty if no local table is available (cache off,
    or materialization failed) — the #47 "more populous region" tiebreak
    degrades to a no-op in that case rather than paying for an extra live
    full-table scan just to build this map.
    """
    if not local_table:
        return {}
    try:
        return _pkg._region_population_lookup_cached(local_table)
    except duckdb.Error:
        return {}


@lru_cache(maxsize=8)
def _region_population_lookup_cached(local_table: str) -> dict[str, int]:
    """The scan behind _region_population_lookup, once per table path per
    process. The file at a path is immutable for the release it belongs to
    (a rebuild publishes through _publish_copied_parquet, which clears this
    via _clear_table_derived_caches), so re-reading every region row on
    every geocode call was pure overhead. Raises duckdb.Error rather than
    caching a failed read as an empty map — lru_cache never stores an
    exception, so the next call retries. Callers only read the map.
    """
    sql = f"""
        SELECT region, population FROM read_parquet('{local_table}')
        WHERE subtype = 'region' AND region IS NOT NULL AND population IS NOT NULL
    """
    with _pkg.overture._conn_lock:
        rows = _pkg.overture.conn().execute(sql).fetchall()
    return dict(rows)
