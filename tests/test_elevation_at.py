"""elevation_at (issue #358): Copernicus GLO-30 DEM point lookups.

Default (non-live) tests build a tiny synthetic tiled, DEFLATE-compressed,
floating-point-predictor TIFF byte-for-byte and monkeypatch
elevation._range_fetcher to serve range slices of it — no network. The
fixture mirrors the *real* GLO-30 COG format, verified empirically during
development against several live tiles: classic little-endian TIFF,
Compression=8 (DEFLATE), SampleFormat=3 (float32), Predictor=3 (the TIFF
floating-point predictor), ModelPixelScale/ModelTiepoint tags.

One live test (@pytest.mark.live, excluded by default per pyproject's
`-m 'not live'` addopts) hits the real bucket.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from placeroot import elevation, errors, server

# ---------------------------------------------------------------------------
# Synthetic GLO-30-shaped COG builder
# ---------------------------------------------------------------------------

TILE_NAME = "Copernicus_DSM_COG_10_N50_00_E006_00_DEM"

# Image: 128 wide x 64 tall, tiled 64x64 -> a 2x1 tile grid (tile 0 = west
# half, tile 1 = east half), so tile-selection math gets exercised.
IMAGE_WIDTH = 128
IMAGE_LENGTH = 64
TILE_WIDTH = 64
TILE_LENGTH = 64
TILES_ACROSS = 2

# Matches a real GLO-30 tile's tiepoint convention: pixel (0,0) is the tile's
# NW corner. Tile N50/E006 spans lat [50, 51], lon [6, 7]; NW corner is
# (lon=6.0, lat=51.0) exactly like the real tile inspected during dev.
TIE_LON, TIE_LAT = 6.0, 51.0
SCALE_X = 1.0 / IMAGE_WIDTH  # deg/px
SCALE_Y = 1.0 / IMAGE_LENGTH  # deg/px


def _pixel_value(row: int, col: int) -> float:
    """A unique-per-pixel value, small enough to round-trip float32 cleanly."""
    return round(100.0 + row * 1000.0 + col * 0.1, 1)


def _encode_float_predictor(rows: list[list[float]], width: int, height: int) -> bytes:
    """Forward direction of Predictor 3: the encoder side, mirroring what a
    real COG writer does and what elevation._undo_float_predictor must undo.
    """
    bytes_per_sample = 4
    row_len = width * bytes_per_sample
    out = bytearray(height * row_len)
    for r in range(height):
        # Per-pixel values -> big-endian bytes (predictor 3's fixed byte order).
        pixel_bytes = b"".join(struct.pack(">f", v) for v in rows[r])
        # Per-pixel interleaved -> byte-plane layout (all byte-0's, then
        # all byte-1's, ...): inverse of the decoder's plane-to-pixel step.
        planes = bytearray(row_len)
        for j in range(width):
            for k in range(bytes_per_sample):
                planes[k * width + j] = pixel_bytes[j * bytes_per_sample + k]
        # Horizontal difference across the whole row, byte-wise.
        base = r * row_len
        prev = planes[0]
        out[base] = prev
        for i in range(1, row_len):
            cur = planes[i]
            out[base + i] = (cur - prev) & 0xFF
            prev = cur
    return bytes(out)


def _tiff_short(value: int) -> bytes:
    return struct.pack("<H", value) + b"\x00\x00"


def build_synthetic_cog(pixel_value=_pixel_value, predictor=3) -> bytes:
    """A classic little-endian tiled TIFF matching the real GLO-30 COG shape."""
    n_tiles = TILES_ACROSS * (IMAGE_LENGTH // TILE_LENGTH)
    assert n_tiles == 2

    # Full-resolution pixel grid, tile by tile, predictor-encoded + deflated.
    tile_blocks = []
    for tile_row in range(IMAGE_LENGTH // TILE_LENGTH):
        for tile_col in range(TILES_ACROSS):
            rows = []
            for r in range(TILE_LENGTH):
                global_row = tile_row * TILE_LENGTH + r
                row_vals = []
                for c in range(TILE_WIDTH):
                    global_col = tile_col * TILE_WIDTH + c
                    row_vals.append(pixel_value(global_row, global_col))
                rows.append(row_vals)
            if predictor == 3:
                encoded = _encode_float_predictor(rows, TILE_WIDTH, TILE_LENGTH)
            else:  # predictor 1: raw samples in the file's (little-endian) order
                encoded = b"".join(struct.pack("<f", v) for row in rows for v in row)
            tile_blocks.append(zlib.compress(encoded, 6))

    # --- Tag table (sorted by tag id, as real TIFFs are) ---
    # (tag, type, count, inline_bytes_or_None)
    # type codes: 3=SHORT, 4=LONG, 12=DOUBLE
    entries = []
    extra_data = bytearray()
    extra_base_placeholder = []  # filled after header size is known

    def add_inline(tag, typ, count, packed4):
        entries.append((tag, typ, count, packed4))

    def add_array(tag, typ, values, fmt):
        packed = b"".join(struct.pack(f"<{fmt}", v) for v in values)
        entries.append((tag, typ, len(values), None))
        extra_base_placeholder.append((len(entries) - 1, packed))

    add_inline(256, 3, 1, _tiff_short(IMAGE_WIDTH))  # ImageWidth
    add_inline(257, 3, 1, _tiff_short(IMAGE_LENGTH))  # ImageLength
    add_inline(258, 3, 1, _tiff_short(32))  # BitsPerSample
    add_inline(259, 3, 1, _tiff_short(8))  # Compression = DEFLATE
    add_inline(317, 3, 1, _tiff_short(predictor))  # Predictor
    add_inline(322, 3, 1, _tiff_short(TILE_WIDTH))  # TileWidth
    add_inline(323, 3, 1, _tiff_short(TILE_LENGTH))  # TileLength
    add_array(324, 4, [0] * n_tiles, "I")  # TileOffsets (patched below)
    add_array(325, 4, [len(b) for b in tile_blocks], "I")  # TileByteCounts
    add_inline(339, 3, 1, _tiff_short(3))  # SampleFormat = float
    add_array(33550, 12, [SCALE_X, SCALE_Y, 0.0], "d")  # ModelPixelScaleTag
    add_array(33922, 12, [0.0, 0.0, 0.0, TIE_LON, TIE_LAT, 0.0], "d")  # ModelTiepointTag

    header_size = 8
    ifd_size = 2 + len(entries) * 12 + 4
    ifd_offset = header_size

    # Lay out extra (overflow) tag data right after the IFD.
    extra_offset = ifd_offset + ifd_size
    array_offsets = {}
    cursor = extra_offset
    for idx, packed in extra_base_placeholder:
        array_offsets[idx] = cursor
        extra_data += packed
        cursor += len(packed)

    # Tile pixel data goes after all the extra tag arrays; TileOffsets
    # needs patching once we know where each block lands.
    tile_data = bytearray()
    tile_offsets_final = []
    for block in tile_blocks:
        tile_offsets_final.append(cursor)
        tile_data += block
        cursor += len(block)

    # Patch the TileOffsets array (index 7 in `entries`, by construction
    # above — the 8th add_* call) now that real offsets are known.
    tile_offsets_entry_idx = 7
    assert entries[tile_offsets_entry_idx][0] == 324
    patched = b"".join(struct.pack("<I", o) for o in tile_offsets_final)
    for idx, packed in extra_base_placeholder:
        if idx == tile_offsets_entry_idx:
            start = array_offsets[idx] - extra_offset
            extra_data[start : start + len(packed)] = patched

    # --- Assemble ---
    out = bytearray()
    out += b"II" + struct.pack("<H", 42) + struct.pack("<I", ifd_offset)
    assert len(out) == header_size

    ifd = bytearray()
    ifd += struct.pack("<H", len(entries))
    for i, (tag, typ, count, packed4) in enumerate(entries):
        ifd += struct.pack("<HHI", tag, typ, count)
        if packed4 is not None:
            ifd += packed4
        else:
            ifd += struct.pack("<I", array_offsets[i])
    ifd += struct.pack("<I", 0)  # next IFD offset (none)
    assert len(ifd) == ifd_size

    out += ifd
    out += extra_data
    out += tile_data
    return bytes(out)


FIXTURE_BYTES = build_synthetic_cog()


def _lonlat_for_pixel(row: int, col: int) -> tuple[float, float]:
    """Center-ish lon/lat that maps back to (row, col) under the reader's mapping."""
    lon = TIE_LON + (col + 0.5) * SCALE_X
    lat = TIE_LAT - (row + 0.5) * SCALE_Y
    return lat, lon


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_caches_and_fetcher(monkeypatch):
    elevation._ifd_cache.clear()
    elevation._block_cache.clear()
    monkeypatch.setattr(elevation, "_range_fetcher", None)
    yield
    elevation._ifd_cache.clear()
    elevation._block_cache.clear()
    monkeypatch.setattr(elevation, "_range_fetcher", None)


class _CountingFetcher:
    """Serves range slices of FIXTURE_BYTES for TILE_NAME; counts calls."""

    def __init__(self, data: bytes = FIXTURE_BYTES, tile_name: str = TILE_NAME):
        self.data = data
        self.tile_name = tile_name
        self.calls = 0

    def __call__(self, url: str, start: int, end: int) -> bytes:
        self.calls += 1
        if self.tile_name not in url:
            raise elevation._TileNotFound(url)
        return self.data[start : end + 1]


def _install_fetcher(monkeypatch, fetcher):
    monkeypatch.setattr(elevation, "_range_fetcher", fetcher)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_header_and_ifd_parse(monkeypatch):
    fetcher = _CountingFetcher()
    _install_fetcher(monkeypatch, fetcher)
    ifd = elevation._get_ifd(TILE_NAME)
    assert ifd is not None
    assert ifd.image_width == IMAGE_WIDTH
    assert ifd.image_length == IMAGE_LENGTH
    assert ifd.tile_width == TILE_WIDTH
    assert ifd.tile_length == TILE_LENGTH
    assert ifd.tiles_across == TILES_ACROSS
    assert ifd.compression == elevation.COMPRESSION_DEFLATE
    assert ifd.sample_format == elevation.SAMPLE_FORMAT_FLOAT
    assert ifd.predictor == elevation.PREDICTOR_FLOATING_POINT
    assert ifd.pixel_scale[0] == pytest.approx(SCALE_X)
    assert ifd.pixel_scale[1] == pytest.approx(SCALE_Y)
    assert ifd.tiepoint[3] == pytest.approx(TIE_LON)
    assert ifd.tiepoint[4] == pytest.approx(TIE_LAT)


def test_lonlat_to_pixel_mapping(monkeypatch):
    _install_fetcher(monkeypatch, _CountingFetcher())
    ifd = elevation._get_ifd(TILE_NAME)
    # Pixel (10, 20): centre lon/lat should map back to (row=10, col=20).
    lat, lon = _lonlat_for_pixel(10, 20)
    row, col = elevation._pixel_for_lonlat(ifd, lat, lon)
    assert (row, col) == (10, 20)
    # Pixel in the east tile, column 90 (tile_col=1).
    lat, lon = _lonlat_for_pixel(5, 90)
    row, col = elevation._pixel_for_lonlat(ifd, lat, lon)
    assert (row, col) == (5, 90)


@pytest.mark.parametrize(
    "row,col",
    [(0, 0), (10, 20), (5, 90), (63, 127), (30, 63), (30, 64)],
)
def test_tile_decode_returns_planted_values(monkeypatch, row, col):
    """Selecting the right tile, decoding, and undoing the float predictor
    must reproduce the exact planted pixel value (round-tripped through
    float32, hence approx)."""
    _install_fetcher(monkeypatch, _CountingFetcher())
    lat, lon = _lonlat_for_pixel(row, col)
    result = elevation.elevation_at(lat, lon)
    assert result["elevation_m"] == pytest.approx(_pixel_value(row, col), abs=0.15)


def test_block_cache_hit_avoids_refetch(monkeypatch):
    fetcher = _CountingFetcher()
    _install_fetcher(monkeypatch, fetcher)
    lat, lon = _lonlat_for_pixel(3, 3)
    elevation.elevation_at(lat, lon)
    calls_after_first = fetcher.calls
    assert calls_after_first > 0

    # Same tile, same block (tile 0): header + block both cached now.
    lat2, lon2 = _lonlat_for_pixel(4, 4)
    elevation.elevation_at(lat2, lon2)
    assert fetcher.calls == calls_after_first, "second read within the same tile block re-fetched"


def test_predictor_undo_matches_planted_bytes():
    """Unit-level check of the predictor undo, independent of HTTP/zlib."""
    width, height = 8, 4
    rows = [[_pixel_value(r, c) for c in range(width)] for r in range(height)]
    encoded = _encode_float_predictor(rows, width, height)
    decoded = elevation._undo_float_predictor(encoded, width, height, 4)
    for r in range(height):
        for c in range(width):
            off = (r * width + c) * 4
            (val,) = struct.unpack_from(">f", decoded, off)
            assert val == pytest.approx(rows[r][c], abs=0.01)


def test_404_returns_null_coverage_answer(monkeypatch):
    def fetcher(url, start, end):
        raise elevation._TileNotFound(url)

    _install_fetcher(monkeypatch, fetcher)
    result = elevation.elevation_at(0.0, -140.0)
    assert result == {"elevation_m": None, "note": elevation._NO_COVERAGE_NOTE}


def test_404_no_coverage_is_negatively_cached(monkeypatch):
    """A missing tile must not be re-fetched on every ocean-point query."""
    calls = []

    def fetcher(url, start, end):
        calls.append(url)
        raise elevation._TileNotFound(url)

    _install_fetcher(monkeypatch, fetcher)
    elevation.elevation_at(0.0, -140.0)
    calls_after_first = len(calls)
    assert calls_after_first > 0
    elevation.elevation_at(0.0, -140.0)
    assert len(calls) == calls_after_first, "404 result was not cached; tile re-fetched"


def test_lon_180_maps_to_w180_tile(monkeypatch):
    """lon == 180.0 is valid input but has no E180 tile; it must normalize
    to the W180 tile instead of silently answering 'no coverage'."""
    urls = []

    def fetcher(url, start, end):
        urls.append(url)
        raise elevation._TileNotFound(url)

    _install_fetcher(monkeypatch, fetcher)
    elevation.elevation_at(-16.8, 180.0)
    assert urls, "no fetch attempted"
    assert all("W180" in u for u in urls), urls
    assert not any("E180" in u for u in urls), urls


def test_block_404_returns_no_coverage_not_traceback(monkeypatch):
    """_TileNotFound raised by the BLOCK fetch (header succeeded) must come
    out as the no-coverage answer, not escape as a raw exception."""

    def fetcher(url, start, end):
        if start == 0:  # header reads start at byte 0
            return FIXTURE_BYTES[start : end + 1]
        raise elevation._TileNotFound(url)

    _install_fetcher(monkeypatch, fetcher)
    lat, lon = _lonlat_for_pixel(10, 10)
    result = elevation.elevation_at(lat, lon)
    assert result == {"elevation_m": None, "note": elevation._NO_COVERAGE_NOTE}


def test_void_fill_value_returns_no_coverage(monkeypatch):
    """GLO-30 voids (-32767 fill) must answer null coverage, not -32767.0 m."""

    def pixel_value(row, col):
        if (row, col) == (10, 10):
            return -32767.0
        return _pixel_value(row, col)

    fixture = build_synthetic_cog(pixel_value=pixel_value)
    _install_fetcher(monkeypatch, _CountingFetcher(data=fixture))
    lat, lon = _lonlat_for_pixel(10, 10)
    result = elevation.elevation_at(lat, lon)
    assert result == {"elevation_m": None, "note": elevation._NO_COVERAGE_NOTE}
    # A neighboring normal pixel in the same fixture still answers.
    lat2, lon2 = _lonlat_for_pixel(11, 11)
    assert elevation.elevation_at(lat2, lon2)["elevation_m"] == pytest.approx(
        _pixel_value(11, 11), abs=0.15
    )


def test_nan_sample_returns_no_coverage(monkeypatch):
    """A non-finite cell must not leak NaN into the (JSON) answer."""
    _install_fetcher(monkeypatch, _CountingFetcher())
    monkeypatch.setattr(elevation, "_sample_value", lambda *a: float("nan"))
    lat, lon = _lonlat_for_pixel(10, 10)
    result = elevation.elevation_at(lat, lon)
    assert result == {"elevation_m": None, "note": elevation._NO_COVERAGE_NOTE}


def test_predictor_1_decodes(monkeypatch):
    """Predictor=1 (none) is accepted and read in the file's own byte order."""
    fixture = build_synthetic_cog(predictor=1)
    _install_fetcher(monkeypatch, _CountingFetcher(data=fixture))
    lat, lon = _lonlat_for_pixel(10, 20)
    result = elevation.elevation_at(lat, lon)
    assert result["elevation_m"] == pytest.approx(_pixel_value(10, 20), abs=0.15)


def test_http_error_raises_upstream_unavailable(monkeypatch):
    def fetcher(url, start, end):
        raise errors.UpstreamUnavailable("simulated network failure")

    _install_fetcher(monkeypatch, fetcher)
    with pytest.raises(errors.UpstreamUnavailable):
        elevation.elevation_at(50.5, 6.2)


def test_batch_cap():
    points = [(50.5 + 0.001 * i, 6.2) for i in range(elevation.MAX_BATCH_POINTS + 1)]
    with pytest.raises(ValueError):
        elevation.elevations_at(points)


def test_batch_reuses_cache(monkeypatch):
    fetcher = _CountingFetcher()
    _install_fetcher(monkeypatch, fetcher)
    lat1, lon1 = _lonlat_for_pixel(1, 1)
    lat2, lon2 = _lonlat_for_pixel(2, 2)
    results = elevation.elevations_at([(lat1, lon1), (lat2, lon2)])
    assert results[0]["elevation_m"] == pytest.approx(_pixel_value(1, 1), abs=0.15)
    assert results[1]["elevation_m"] == pytest.approx(_pixel_value(2, 2), abs=0.15)
    calls_after_both = fetcher.calls
    # A third point in the same tile block should not add fetcher calls.
    lat3, lon3 = _lonlat_for_pixel(3, 3)
    elevation.elevations_at([(lat3, lon3)])
    assert fetcher.calls == calls_after_both


# ---------------------------------------------------------------------------
# Server-tool level
# ---------------------------------------------------------------------------


def test_server_tool_out_of_range_coords_bad_request():
    result = server.elevation_at(lat=95.0, lon=6.0)
    assert result["error"] == "bad_request"


def test_server_tool_swapped_lat_lon_bad_request():
    result = server.elevation_at(lat=6.0, lon=200.0)
    assert result["error"] == "bad_request"


def test_server_tool_returns_elevation(monkeypatch):
    _install_fetcher(monkeypatch, _CountingFetcher())
    lat, lon = _lonlat_for_pixel(10, 10)
    result = server.elevation_at(lat=lat, lon=lon)
    assert "error" not in result
    assert result["elevation_m"] == pytest.approx(_pixel_value(10, 10), abs=0.15)


def test_server_tool_no_coverage(monkeypatch):
    def fetcher(url, start, end):
        raise elevation._TileNotFound(url)

    _install_fetcher(monkeypatch, fetcher)
    result = server.elevation_at(lat=0.0, lon=-140.0)
    assert result["elevation_m"] is None
    assert "note" in result


def test_server_tool_upstream_error(monkeypatch):
    def fetcher(url, start, end):
        raise errors.UpstreamUnavailable("simulated network failure")

    _install_fetcher(monkeypatch, fetcher)
    result = server.elevation_at(lat=50.5, lon=6.2)
    assert result["error"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# Live (real network) — excluded by default (`-m 'not live'`)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_live_sanity_points():
    # Dutch coastal point: near sea level.
    r = elevation.elevation_at(52.0907, 4.0)
    assert r["elevation_m"] is None or -10.0 <= r["elevation_m"] <= 15.0
    # An Alpine pass-ish point: plausible mountain elevation.
    r2 = elevation.elevation_at(46.5587, 8.0)
    assert r2["elevation_m"] is not None
    assert 500.0 <= r2["elevation_m"] <= 4500.0
