"""Ground elevation from Copernicus GLO-30 DEM on AWS Open Data (issue #358).

s3://copernicus-dem-30m (region eu-central-1, HTTPS-served, anonymous),
Cloud-Optimized GeoTIFFs, one file per 1 degree x 1 degree tile. Object key:

  Copernicus_DSM_COG_10_N50_00_E006_00_DEM/Copernicus_DSM_COG_10_N50_00_E006_00_DEM.tif

"10" is the product code for the 10ths-of-arcsecond release (GLO-30);
N50/E006 is the tile's SW corner, zero-padded (2 digits lat, 3 digits lon).
Some countries' tiles are excluded from the public release, and every tile
is finite (no polar/far-ocean coverage), so a 404 is a legitimate outcome
here, not a fetch failure — see elevation_at()'s null-coverage answer.

Format, verified empirically against several real tiles during development
(not assumed from the product's general spec, which allows more variation
than these files actually use):
  - Classic TIFF (magic 42), little-endian ("II"). Not BigTIFF.
  - ImageWidth/ImageLength vary by latitude band (fewer columns nearer the
    poles, to hold ~30 m ground resolution as meridians converge) — e.g.
    2400x3600 at N50. Always read these from the IFD; never hardcode them.
  - TileWidth/TileLength = 1024x1024 (not the 512x512 a COG commonly uses).
  - Compression = 8 (DEFLATE/zlib).
  - SampleFormat = 3 (IEEE float), BitsPerSample = 32.
  - Predictor = 3 (the TIFF "floating point predictor", Adobe Technical
    Note 3) — NOT predictor 2. This is the fiddly part: undoing it takes
    two passes (see _undo_float_predictor), and per the predictor 3 spec
    the recovered bytes are big-endian regardless of the file's own
    (little-endian) byte order — confirmed by decoding a real tile and
    checking the values fell in a plausible elevation range only under
    that interpretation.
  - ModelPixelScaleTag (33550) + ModelTiepointTag (33922) give an affine
    lon/lat -> pixel mapping directly; no need to parse GeoKeyDirectory.

Only this exact combination is supported. Anything else (BigTIFF, LZW,
predictor 1/2, integer samples) raises a clear "unsupported ..." error
naming what was actually found, rather than guessing.

Nearest-neighbor sampling only (no bilinear interpolation) — adequate at
~30 m cells for the "what's the elevation here" questions this tool
answers, and much simpler than a resampling kernel that would need
neighboring tiles at edges.

Two in-process LRU caches, both process-lifetime and unbounded across
tiles (only bounded in how much decoded pixel/IFD data each holds):
  - _ifd_cache: parsed IFD (dimensions, tile grid, tag values) per tile
    name. Tiny (a few hundred bytes each); capped generously.
  - _block_cache: decoded (decompressed + predictor-undone) tile blocks,
    keyed by (tile_name, tile_index). Real blocks are ~4 MB each
    (1024x1024 float32) — much bigger than a typical 512x512 COG tile
    would be — so the cap here is small; see _BLOCK_CACHE_MAX.

elevations_at() is the batch entry point routing profiles (#313) will call
for a path profile; it reuses one block cache across all points so a route
that stays within a handful of tiles pays for each tile once.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import urllib.error
import urllib.request
import zlib
from collections import OrderedDict

from placeroot.errors import UpstreamUnavailable

logger = logging.getLogger(__name__)

BUCKET_HOST = "copernicus-dem-30m.s3.eu-central-1.amazonaws.com"
_BASE_URL = f"https://{BUCKET_HOST}"

# First range-read for the TIFF header + IFD 0. COGs put these up front;
# grown (see _fetch_header) if the IFD's own entries point past this.
_HEADER_INITIAL_BYTES = 16 * 1024
_HEADER_MAX_BYTES = 256 * 1024

_HTTP_TIMEOUT_S = 20

# Real decoded tile blocks are ~4 MB (1024x1024 float32) at this product's
# actual tile size — 4x the ~1 MB a 512x512 COG tile would decode to, so the
# cap is sized down to keep the same rough memory budget (~24 MB resident).
_BLOCK_CACHE_MAX = 6
_IFD_CACHE_MAX = 180

# Points per elevations_at() call. Matches the batch caps used elsewhere in
# the query layer (reverse_geocode_batch etc.) in spirit; #313's route
# profiles are expected to sample well under this per leg.
MAX_BATCH_POINTS = 50

_TAG_IMAGE_WIDTH = 256
_TAG_IMAGE_LENGTH = 257
_TAG_COMPRESSION = 259
_TAG_TILE_WIDTH = 322
_TAG_TILE_LENGTH = 323
_TAG_TILE_OFFSETS = 324
_TAG_TILE_BYTE_COUNTS = 325
_TAG_SAMPLE_FORMAT = 339
_TAG_PREDICTOR = 317
_TAG_MODEL_PIXEL_SCALE = 33550
_TAG_MODEL_TIEPOINT = 33922

_TIFF_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}

COMPRESSION_DEFLATE = 8
SAMPLE_FORMAT_FLOAT = 3
BITS_PER_SAMPLE_FLOAT32 = 32
PREDICTOR_NONE = 1
PREDICTOR_FLOATING_POINT = 3


class ElevationFormatError(Exception):
    """A tile's TIFF structure doesn't match what GLO-30 COGs are known to use.

    Distinct from UpstreamUnavailable: this is a "the file isn't what we
    expect" bug/format-drift signal, not a transient network failure, but
    callers still can't do anything about it beyond reporting an error —
    see elevation_at's handling.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class _TileNotFound(Exception):
    """Internal: the S3 object for a tile doesn't exist (404) -> no coverage."""


def _tile_name(lat_floor: int, lon_floor: int) -> str:
    ns = "N" if lat_floor >= 0 else "S"
    ew = "E" if lon_floor >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_floor):02d}_00_{ew}{abs(lon_floor):03d}_00_DEM"


def _tile_url(tile_name: str) -> str:
    return f"{_BASE_URL}/{tile_name}/{tile_name}.tif"


# ---------------------------------------------------------------------------
# HTTP range fetch
# ---------------------------------------------------------------------------

# Swapped out by tests to serve bytes from an in-memory fixture instead of
# the network. Signature: (url, start, end_inclusive) -> bytes.
_range_fetcher = None


def _default_fetch_range(url: str, start: int, end: int) -> bytes:
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _TileNotFound(url) from None
        raise UpstreamUnavailable(
            f"Copernicus DEM tile fetch failed ({e.code} {e.reason}) for {url}"
        ) from e
    except urllib.error.URLError as e:
        raise UpstreamUnavailable(f"Copernicus DEM tile fetch failed ({e.reason}) for {url}") from e
    except (TimeoutError, OSError) as e:
        raise UpstreamUnavailable(f"Copernicus DEM tile fetch failed ({e}) for {url}") from e


def _fetch_range(url: str, start: int, end: int) -> bytes:
    fetcher = _range_fetcher or _default_fetch_range
    return fetcher(url, start, end)


# ---------------------------------------------------------------------------
# TIFF / IFD parsing
# ---------------------------------------------------------------------------


class _TileIFD:
    __slots__ = (
        "endian",
        "image_width",
        "image_length",
        "tile_width",
        "tile_length",
        "tiles_across",
        "tile_offsets",
        "tile_byte_counts",
        "compression",
        "sample_format",
        "predictor",
        "pixel_scale",
        "tiepoint",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _read_ifd_values(data: bytes, endian: str, typ: int, count: int, value_field_off: int):
    size = _TIFF_TYPE_SIZES.get(typ)
    if size is None:
        raise ElevationFormatError(f"unsupported TIFF tag type {typ}")
    total = size * count
    if total <= 4:
        raw_off = value_field_off
    else:
        raw_off = struct.unpack_from(f"{endian}I", data, value_field_off)[0]
    if raw_off + total > len(data):
        raise ElevationFormatError(
            f"IFD value at offset {raw_off} (len {total}) extends past fetched header bytes"
        )
    fmt = {
        1: "B",
        2: "s",
        3: "H",
        4: "I",
        5: "I",
        6: "b",
        7: "B",
        8: "h",
        9: "i",
        10: "i",
        11: "f",
        12: "d",
    }[typ]
    if typ == 5:  # RATIONAL: two uint32 (numerator, denominator) per value
        raw = struct.unpack_from(f"{endian}{2 * count}I", data, raw_off)
        return [raw[2 * i] / raw[2 * i + 1] for i in range(count)]
    if typ == 2:
        return data[raw_off : raw_off + total].split(b"\x00")[0].decode("ascii", "replace")
    return list(struct.unpack_from(f"{endian}{count}{fmt}", data, raw_off))


def _parse_classic_tiff_ifd0(data: bytes) -> _TileIFD:
    """Parse IFD 0 of a classic (non-Big) little-endian TIFF from `data`.

    Raises ElevationFormatError on BigTIFF, big-endian byte order, or any
    tag value this reader needs that's missing or malformed. `data` must
    already extend far enough to cover every IFD entry's value — callers
    grow the fetch (see _fetch_header) and retry if not.
    """
    if len(data) < 8:
        raise ElevationFormatError("header too short to contain a TIFF byte-order mark")
    bom = data[0:2]
    if bom == b"II":
        endian = "<"
    elif bom == b"MM":
        raise ElevationFormatError(
            "big-endian ('MM') TIFF byte order — GLO-30 COGs are expected little-endian ('II')"
        )
    else:
        raise ElevationFormatError(f"not a TIFF file (bad byte-order mark {bom!r})")

    version = struct.unpack_from(f"{endian}H", data, 2)[0]
    if version == 43:
        raise ElevationFormatError(
            "BigTIFF (magic 43) — this reader only supports classic 32-bit-offset TIFF, "
            "which is what GLO-30 COGs actually use"
        )
    if version != 42:
        raise ElevationFormatError(f"not a classic TIFF (magic {version}, expected 42)")

    ifd_offset = struct.unpack_from(f"{endian}I", data, 4)[0]
    if ifd_offset + 2 > len(data):
        raise ElevationFormatError("IFD 0 offset extends past fetched header bytes")
    count = struct.unpack_from(f"{endian}H", data, ifd_offset)[0]
    entries_end = ifd_offset + 2 + count * 12
    if entries_end + 4 > len(data):
        raise ElevationFormatError("IFD 0 entry table extends past fetched header bytes")

    tags: dict[int, tuple[int, int, int]] = {}
    for i in range(count):
        entry_off = ifd_offset + 2 + i * 12
        tag, typ, cnt = struct.unpack_from(f"{endian}HHI", data, entry_off)
        tags[tag] = (typ, cnt, entry_off + 8)

    def require(tag: int, name: str):
        if tag not in tags:
            raise ElevationFormatError(f"IFD 0 is missing required tag {name} ({tag})")
        typ, cnt, val_off = tags[tag]
        return _read_ifd_values(data, endian, typ, cnt, val_off)

    def optional(tag: int, default):
        if tag not in tags:
            return default
        typ, cnt, val_off = tags[tag]
        return _read_ifd_values(data, endian, typ, cnt, val_off)

    image_width = require(_TAG_IMAGE_WIDTH, "ImageWidth")[0]
    image_length = require(_TAG_IMAGE_LENGTH, "ImageLength")[0]
    tile_width = require(_TAG_TILE_WIDTH, "TileWidth")[0]
    tile_length = require(_TAG_TILE_LENGTH, "TileLength")[0]
    tile_offsets = require(_TAG_TILE_OFFSETS, "TileOffsets")
    tile_byte_counts = require(_TAG_TILE_BYTE_COUNTS, "TileByteCounts")
    compression = optional(_TAG_COMPRESSION, [1])[0]
    sample_format = optional(_TAG_SAMPLE_FORMAT, [1])[0]
    predictor = optional(_TAG_PREDICTOR, [1])[0]
    pixel_scale = require(_TAG_MODEL_PIXEL_SCALE, "ModelPixelScaleTag")
    tiepoint = require(_TAG_MODEL_TIEPOINT, "ModelTiepointTag")

    if compression != COMPRESSION_DEFLATE:
        raise ElevationFormatError(
            f"unsupported Compression={compression} (expected {COMPRESSION_DEFLATE}=DEFLATE, "
            "the only compression GLO-30 COGs are known to use)"
        )
    if sample_format != SAMPLE_FORMAT_FLOAT:
        raise ElevationFormatError(
            f"unsupported SampleFormat={sample_format} (expected {SAMPLE_FORMAT_FLOAT}=IEEE float)"
        )
    if predictor not in (PREDICTOR_NONE, PREDICTOR_FLOATING_POINT):
        raise ElevationFormatError(
            f"unsupported Predictor={predictor} (expected 1=none or "
            f"{PREDICTOR_FLOATING_POINT}=floating-point, the one GLO-30 COGs are known to use)"
        )

    tiles_across = (image_width + tile_width - 1) // tile_width
    tiles_down = (image_length + tile_length - 1) // tile_length
    expected_tiles = tiles_across * tiles_down
    if len(tile_offsets) != expected_tiles or len(tile_byte_counts) != expected_tiles:
        raise ElevationFormatError(
            f"tile grid mismatch: expected {expected_tiles} tiles "
            f"({tiles_across}x{tiles_down}), got {len(tile_offsets)} offsets"
        )

    return _TileIFD(
        endian=endian,
        image_width=image_width,
        image_length=image_length,
        tile_width=tile_width,
        tile_length=tile_length,
        tiles_across=tiles_across,
        tile_offsets=tile_offsets,
        tile_byte_counts=tile_byte_counts,
        compression=compression,
        sample_format=sample_format,
        predictor=predictor,
        pixel_scale=pixel_scale,
        tiepoint=tiepoint,
    )


def _fetch_header(url: str) -> bytes:
    """Range-read enough of `url` to parse IFD 0, growing the read if needed."""
    size = _HEADER_INITIAL_BYTES
    data = _fetch_range(url, 0, size - 1)
    while True:
        try:
            _parse_classic_tiff_ifd0(data)
        except ElevationFormatError as e:
            if "extends past fetched header bytes" in e.detail and size < _HEADER_MAX_BYTES:
                size = min(size * 4, _HEADER_MAX_BYTES)
                data = _fetch_range(url, 0, size - 1)
                continue
            raise
        return data


_ifd_cache_lock = threading.Lock()
_ifd_cache: OrderedDict[str, _TileIFD] = OrderedDict()

_block_cache_lock = threading.Lock()
_block_cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()


def _get_ifd(tile_name: str) -> _TileIFD | None:
    """Parsed IFD for `tile_name`, or None if the tile doesn't exist (404)."""
    with _ifd_cache_lock:
        cached = _ifd_cache.get(tile_name)
        if cached is not None:
            _ifd_cache.move_to_end(tile_name)
            return cached

    url = _tile_url(tile_name)
    try:
        header = _fetch_header(url)
    except _TileNotFound:
        return None
    ifd = _parse_classic_tiff_ifd0(header)

    with _ifd_cache_lock:
        _ifd_cache[tile_name] = ifd
        _ifd_cache.move_to_end(tile_name)
        while len(_ifd_cache) > _IFD_CACHE_MAX:
            _ifd_cache.popitem(last=False)
    return ifd


# ---------------------------------------------------------------------------
# Predictor undo + tile decode
# ---------------------------------------------------------------------------


def _undo_horizontal_diff_bytes(buf: bytearray, num_rows: int, row_len: int) -> None:
    """In place: each row's bytes are cumulative differences; undo to absolutes (mod 256)."""
    for r in range(num_rows):
        base = r * row_len
        prev = buf[base]
        for i in range(base + 1, base + row_len):
            prev = (prev + buf[i]) & 0xFF
            buf[i] = prev


def _undo_float_predictor(data: bytes, width: int, height: int, bytes_per_sample: int) -> bytes:
    """Undo TIFF Predictor 3 (Adobe Technical Note 3) on one decompressed tile block.

    Two passes: (1) cumulative-sum each byte of a row against the previous
    byte in that row (undoes the horizontal differencing, byte-wise across
    the whole row regardless of sample boundaries); (2) de-interleave the
    row from "byte-plane" layout (all MSBs, then all next-most-significant
    bytes, ..., all LSBs) back to per-pixel big-endian byte order. The
    predictor spec fixes step 2's output as big-endian regardless of the
    TIFF's own byte order — callers must unpack with '>' accordingly.
    """
    row_len = width * bytes_per_sample
    buf = bytearray(data)
    _undo_horizontal_diff_bytes(buf, height, row_len)

    out = bytearray(len(buf))
    for r in range(height):
        base = r * row_len
        row = memoryview(buf)[base : base + row_len]
        for k in range(bytes_per_sample):
            plane = row[k * width : (k + 1) * width]
            out[base + k : base + row_len : bytes_per_sample] = plane
    return bytes(out)


def _decode_tile_block(tile_name: str, tile_index: int, ifd: _TileIFD) -> bytes:
    """Decoded (decompressed + predictor-undone) bytes for one tile block, LRU-cached."""
    key = (tile_name, tile_index)
    with _block_cache_lock:
        cached = _block_cache.get(key)
        if cached is not None:
            _block_cache.move_to_end(key)
            return cached

    offset = ifd.tile_offsets[tile_index]
    byte_count = ifd.tile_byte_counts[tile_index]
    raw = _fetch_range(_tile_url(tile_name), offset, offset + byte_count - 1)
    try:
        decompressed = zlib.decompress(raw)
    except zlib.error as e:
        raise ElevationFormatError(
            f"tile {tile_name} block {tile_index}: DEFLATE decode failed ({e})"
        )

    bytes_per_sample = BITS_PER_SAMPLE_FLOAT32 // 8
    expected = ifd.tile_width * ifd.tile_length * bytes_per_sample
    if len(decompressed) != expected:
        raise ElevationFormatError(
            f"tile {tile_name} block {tile_index}: decompressed to {len(decompressed)} bytes, "
            f"expected {expected}"
        )

    if ifd.predictor == PREDICTOR_FLOATING_POINT:
        block = _undo_float_predictor(
            decompressed, ifd.tile_width, ifd.tile_length, bytes_per_sample
        )
    else:
        block = decompressed

    with _block_cache_lock:
        _block_cache[key] = block
        _block_cache.move_to_end(key)
        while len(_block_cache) > _BLOCK_CACHE_MAX:
            _block_cache.popitem(last=False)
    return block


def _sample_value(block: bytes, ifd: _TileIFD, row_in_tile: int, col_in_tile: int) -> float:
    bytes_per_sample = BITS_PER_SAMPLE_FLOAT32 // 8
    off = (row_in_tile * ifd.tile_width + col_in_tile) * bytes_per_sample
    # Predictor 3's recovered bytes are big-endian per spec; with no
    # predictor, samples are stored in the file's own (little-endian) order.
    fmt = ">f" if ifd.predictor == PREDICTOR_FLOATING_POINT else f"{ifd.endian}f"
    return struct.unpack_from(fmt, block, off)[0]


def _pixel_for_lonlat(ifd: _TileIFD, lat: float, lon: float) -> tuple[int, int]:
    """Nearest-neighbor (row, col) in the tile's full-resolution raster for (lat, lon)."""
    scale_x, scale_y = ifd.pixel_scale[0], ifd.pixel_scale[1]
    # ModelTiepointTag: (I, J, K, X, Y, Z) — raster (I,J) maps to model (X,Y).
    # GLO-30 tiles always tie pixel (0,0) to the tile's NW (upper-left) corner.
    tie_x, tie_y = ifd.tiepoint[3], ifd.tiepoint[4]
    col = int((lon - tie_x) / scale_x)
    row = int((tie_y - lat) / scale_y)
    col = min(max(col, 0), ifd.image_width - 1)
    row = min(max(row, 0), ifd.image_length - 1)
    return row, col


def _elevation_at_one(lat: float, lon: float) -> float | None:
    """None means no Copernicus coverage (ocean, or a tile excluded from release)."""
    lat_floor = math.floor(lat)
    lon_floor = math.floor(lon)
    tile_name = _tile_name(lat_floor, lon_floor)
    ifd = _get_ifd(tile_name)
    if ifd is None:
        return None

    row, col = _pixel_for_lonlat(ifd, lat, lon)
    tile_col = col // ifd.tile_width
    tile_row = row // ifd.tile_length
    tile_index = tile_row * ifd.tiles_across + tile_col
    row_in_tile = row % ifd.tile_length
    col_in_tile = col % ifd.tile_width

    block = _decode_tile_block(tile_name, tile_index, ifd)
    return _sample_value(block, ifd, row_in_tile, col_in_tile)


_NO_COVERAGE_NOTE = (
    "no Copernicus GLO-30 coverage here (ocean, or a tile excluded from public release)"
)


def elevation_at(lat: float, lon: float) -> dict:
    """Ground elevation (meters) at (lat, lon) from Copernicus GLO-30, or a null-coverage note.

    Caller is responsible for range-validating lat/lon first (see
    server._invalid_coord); this function assumes valid coordinates.
    """
    value = _elevation_at_one(lat, lon)
    if value is None:
        return {"elevation_m": None, "note": _NO_COVERAGE_NOTE}
    return {"elevation_m": round(value, 1)}


def elevations_at(points: list[tuple[float, float]]) -> list[dict]:
    """Batch elevation_at over (lat, lon) pairs, reusing the block cache across all of them.

    Internal-only (no MCP tool yet) — for #313's route-profile callers.
    Raises ValueError if more than MAX_BATCH_POINTS points are given.
    """
    if len(points) > MAX_BATCH_POINTS:
        raise ValueError(
            f"elevations_at accepts at most {MAX_BATCH_POINTS} points, got {len(points)}"
        )
    return [elevation_at(lat, lon) for lat, lon in points]
