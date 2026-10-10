"""Optional compiled accelerator for routing.py's two hot loops.

The `placeroot-native` extension (native/, built with maturin) provides the
street-graph segment loop of build_graph and the target Dijkstra of
_dijkstra_path_to_target. It is strictly optional: placeroot is pure Python
and `uvx placeroot` never needs it.

AVAILABLE is True only when the extension imports and PLACEROOT_NATIVE is not
"0". routing.py reads AVAILABLE at each call (not at import), so the pure
Python code is the fallback everywhere and tests can switch paths in-process.
"""

from __future__ import annotations

import os

_impl = None
if os.environ.get("PLACEROOT_NATIVE", "1") != "0":
    try:
        import placeroot_native as _impl  # type: ignore[import-not-found]
    except ImportError:
        _impl = None

AVAILABLE: bool = _impl is not None


def build_graph_arrays(rows: list, earth_radius_m: float) -> tuple:
    """Topology arrays for prepared segment rows; see native/src/lib.rs."""
    return _impl.build_graph_arrays(rows, earth_radius_m)


def csr(indptr: list, indices: list, weights: list, lengths: list):
    """A reusable CSR search object (placeroot_native.Csr)."""
    return _impl.Csr(indptr, indices, weights, lengths)
