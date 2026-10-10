//! Optional compiled kernels for placeroot's street-graph build and the
//! target-terminated Dijkstra behind route() / map_match().
//!
//! Each routine mirrors a Python function in src/placeroot/routing.py
//! operation for operation, so the two paths agree bit-for-bit wherever the
//! platform libm agrees:
//!
//! * `build_graph_arrays` <-> the per-segment loop of `build_graph` (the
//!   non-shape path): WKT parsing, cumulative haversine, connector
//!   interpolation via `_point_at_fraction`, `pt_<lon>_<lat>` node ids
//!   (Python's `round(x, 6)` + `repr`), and the add_node/add_edge call
//!   sequence. It returns the calls as flat arrays; Python replays them.
//! * `Csr.dijkstra` / `dijkstra` <-> `_dijkstra_path_to_target`. Nodes are
//!   integer indices assigned in sorted-string order, so the heap's
//!   `(time, index)` ordering breaks ties exactly as Python's `(time, node)`.
//!
//! The Python side (src/placeroot/native.py) is the only importer. Nothing
//! here reads the environment or touches global state.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyAnyMethods;
use std::cmp::{Ordering, Reverse};
use std::collections::{BinaryHeap, HashMap};

/// Marks "no predecessor" in the Dijkstra predecessor table.
const NO_PREV: u32 = u32::MAX;

// ---------------------------------------------------------------------------
// Geometry (mirrors routing._haversine_m, _cumulative_lengths_m,
// _point_at_fraction and _parse_linestring_wkt)
// ---------------------------------------------------------------------------

/// Haversine distance in metres. Same operation order as
/// routing._haversine_m: radians of the *difference* for dphi/dlambda.
fn haversine_m(radius_m: f64, lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let p1 = lat1.to_radians();
    let p2 = lat2.to_radians();
    let dphi = (lat2 - lat1).to_radians();
    let dlambda = (lon2 - lon1).to_radians();
    let a = (dphi / 2.0).sin().powi(2) + p1.cos() * p2.cos() * (dlambda / 2.0).sin().powi(2);
    2.0 * radius_m * a.sqrt().asin()
}

/// Cumulative arc length at each vertex; `pts` are (lon, lat) like Python.
fn cumulative_m(radius_m: f64, pts: &[(f64, f64)]) -> Vec<f64> {
    let mut cum = Vec::with_capacity(pts.len());
    cum.push(0.0);
    for w in pts.windows(2) {
        let (lon1, lat1) = w[0];
        let (lon2, lat2) = w[1];
        let last = cum[cum.len() - 1];
        cum.push(last + haversine_m(radius_m, lat1, lon1, lat2, lon2));
    }
    cum
}

/// (lat, lon) at arc-length fraction `at` of the polyline. Mirrors
/// routing._point_at_fraction, including its clamping and zero-length guards.
fn point_at_fraction(pts: &[(f64, f64)], at: f64, cum: &[f64]) -> (f64, f64) {
    let total = cum[cum.len() - 1];
    if at <= 0.0 || total <= 0.0 {
        let (lon, lat) = pts[0];
        return (lat, lon);
    }
    if at >= 1.0 {
        let (lon, lat) = pts[pts.len() - 1];
        return (lat, lon);
    }
    let target = at * total;
    let mut i = 0usize;
    while i + 2 < cum.len() && cum[i + 1] < target {
        i += 1;
    }
    let seg_len = cum[i + 1] - cum[i];
    let frac = if seg_len > 0.0 {
        (target - cum[i]) / seg_len
    } else {
        0.0
    };
    let (lon1, lat1) = pts[i];
    let (lon2, lat2) = pts[i + 1];
    (lat1 + frac * (lat2 - lat1), lon1 + frac * (lon2 - lon1))
}

/// Parse "LINESTRING (lon lat, ...)" exactly as routing._parse_linestring_wkt
/// does. Any input Python would reject with ValueError/IndexError returns None.
/// Note: Python's float() also accepts underscore digit separators ("1_0");
/// Overture's WKT never contains them, and they are rejected here.
fn parse_linestring(wkt: &str) -> Option<Vec<(f64, f64)>> {
    let s = wkt.trim();
    let open = s.find('(')?;
    let close = s.rfind(')')?;
    if close < open + 1 {
        return None;
    }
    let mut pts = Vec::new();
    for piece in s[open + 1..close].split(',') {
        let mut tokens = piece.split_whitespace();
        let (a, b) = match (tokens.next(), tokens.next(), tokens.next()) {
            (Some(a), Some(b), None) => (a, b),
            _ => return None,
        };
        let lon: f64 = a.parse().ok()?;
        let lat: f64 = b.parse().ok()?;
        pts.push((lon, lat));
    }
    Some(pts)
}

// ---------------------------------------------------------------------------
// Python float formatting for node ids: "pt_{round(x, 6)}"
// ---------------------------------------------------------------------------

/// repr() of a finite f64 as CPython prints it (shortest round-trip digits;
/// fixed notation for decimal exponents in [-4, 16), else d.ddde+XX).
fn py_repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_string();
    }
    if x.is_infinite() {
        return if x > 0.0 {
            "inf".to_string()
        } else {
            "-inf".to_string()
        };
    }
    // `{:e}` yields the shortest round-trip digits, e.g. "-1.2345e-5".
    let sci = format!("{:e}", x);
    let (mantissa, exp_str) = sci.split_once('e').unwrap_or((sci.as_str(), "0"));
    let exp: i32 = exp_str.parse().unwrap_or(0);
    let (neg, mantissa) = match mantissa.strip_prefix('-') {
        Some(m) => (true, m),
        None => (false, mantissa),
    };
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let mut out = String::with_capacity(digits.len() + 8);
    if neg {
        out.push('-');
    }
    if (-4..16).contains(&exp) {
        if exp >= 0 {
            let int_len = exp as usize + 1;
            if digits.len() <= int_len {
                out.push_str(&digits);
                out.push_str(&"0".repeat(int_len - digits.len()));
                out.push_str(".0");
            } else {
                out.push_str(&digits[..int_len]);
                out.push('.');
                out.push_str(&digits[int_len..]);
            }
        } else {
            out.push_str("0.");
            out.push_str(&"0".repeat((-exp - 1) as usize));
            out.push_str(&digits);
        }
    } else {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        out.push('e');
        out.push(if exp < 0 { '-' } else { '+' });
        out.push_str(&format!("{:02}", exp.abs()));
    }
    out
}

/// str(round(x, 6)) for a float x, as f"{round(x, 6)}" renders it. Rounding is
/// the correctly rounded decimal (round-half-even on exact ties), then the
/// nearest double, which is what CPython's round() produces.
fn py_round6_str(x: f64) -> String {
    if !x.is_finite() {
        return py_repr(x);
    }
    let rounded: f64 = format!("{:.6}", x).parse().unwrap_or(x);
    py_repr(rounded)
}

// ---------------------------------------------------------------------------
// Graph build (mirrors the per-segment loop of routing.build_graph)
// ---------------------------------------------------------------------------

/// The flat add_node / add_edge call sequence, with nodes deduplicated on
/// first sight (Graph.add_node is idempotent and first-wins on coordinates).
#[derive(Default)]
struct GraphOut {
    index: HashMap<String, u32>,
    ids: Vec<String>,
    lat: Vec<f64>,
    lon: Vec<f64>,
    edge_a: Vec<u32>,
    edge_b: Vec<u32>,
    edge_w: Vec<f64>,
    edge_len: Vec<f64>,
    edge_directed: Vec<bool>,
    edge_row: Vec<i64>,
}

impl GraphOut {
    fn node(&mut self, id: &str, lat: f64, lon: f64) -> u32 {
        if let Some(&i) = self.index.get(id) {
            return i;
        }
        let i = self.ids.len() as u32;
        self.index.insert(id.to_string(), i);
        self.ids.push(id.to_string());
        self.lat.push(lat);
        self.lon.push(lon);
        i
    }

    fn edge(&mut self, a: u32, b: u32, weight: f64, length: f64, directed: bool, row: i64) {
        self.edge_a.push(a);
        self.edge_b.push(b);
        self.edge_w.push(weight);
        self.edge_len.push(length);
        self.edge_directed.push(directed);
        self.edge_row.push(row);
    }
}

/// One connector as the Python loop reads it: `conn["at"]`, `conn["connector_id"]`.
fn read_connector(conn: &Bound<'_, PyAny>) -> PyResult<(f64, String)> {
    let at: f64 = conn.get_item("at")?.extract()?;
    let id: String = conn.get_item("connector_id")?.extract()?;
    Ok((at, id))
}

/// One segment as Python prepared it: geometry, connectors, the one-way
/// decision for the mode, and the drive speed (1.0 when the graph is untimed).
struct Segment<'py> {
    wkt: String,
    connectors: Bound<'py, PyAny>,
    forward_allowed: bool,
    backward_allowed: bool,
    speed_m_s: f64,
}

/// Replays build_graph's segment loop for one prepared row. `row` is the
/// index into the Python-side list, reported back so Python can attach the
/// primary name. Rows that Python would `continue` past (bad WKT, fewer than
/// two points) emit nothing, exactly as the Python loop does.
fn add_segment(out: &mut GraphOut, radius_m: f64, row: i64, seg: &Segment<'_>) -> PyResult<()> {
    let connectors = &seg.connectors;
    let (forward_allowed, backward_allowed) = (seg.forward_allowed, seg.backward_allowed);
    let speed_m_s = seg.speed_m_s;
    let pts = match parse_linestring(&seg.wkt) {
        Some(p) if p.len() >= 2 => p,
        _ => return Ok(()),
    };
    let (start_lon, start_lat) = pts[0];
    let (end_lon, end_lat) = pts[pts.len() - 1];

    let mut start_ids: Vec<String> = Vec::new();
    let mut end_ids: Vec<String> = Vec::new();
    let mut interior: Vec<(f64, String)> = Vec::new();
    if !connectors.is_none() {
        for conn in connectors.try_iter()? {
            let (at, id) = read_connector(&conn?)?;
            if at <= 0.0 {
                start_ids.push(id);
            } else if at >= 1.0 {
                end_ids.push(id);
            } else {
                interior.push((at, id));
            }
        }
    }
    if start_ids.is_empty() {
        start_ids.push(format!(
            "pt_{}_{}",
            py_round6_str(start_lon),
            py_round6_str(start_lat)
        ));
    }
    if end_ids.is_empty() {
        end_ids.push(format!(
            "pt_{}_{}",
            py_round6_str(end_lon),
            py_round6_str(end_lat)
        ));
    }

    let cum = cumulative_m(radius_m, &pts);
    let total = cum[cum.len() - 1];

    // Nodes, in the order the Python loop adds them: endpoints, interior
    // connectors (connector order), then the co-located duplicate endpoints
    // each tied to their anchor by a zero-length undirected edge. These run
    // before the one-way check, so a fully one-way-blocked row still adds them.
    let start_node = out.node(&start_ids[0], start_lat, start_lon);
    let end_node = out.node(&end_ids[0], end_lat, end_lon);
    let mut stops: Vec<(f64, String, u32)> = Vec::with_capacity(interior.len() + 2);
    for (at, id) in &interior {
        let (ilat, ilon) = point_at_fraction(&pts, *at, &cum);
        let idx = out.node(id, ilat, ilon);
        stops.push((*at, id.clone(), idx));
    }
    for dup in &start_ids[1..] {
        let d = out.node(dup, start_lat, start_lon);
        out.edge(start_node, d, 0.0, 0.0, false, -1);
    }
    for dup in &end_ids[1..] {
        let d = out.node(dup, end_lat, end_lon);
        out.edge(end_node, d, 0.0, 0.0, false, -1);
    }
    if !forward_allowed && !backward_allowed {
        return Ok(());
    }

    // Stops along the segment, sorted by (at, connector_id) like Python's
    // sorted(interior) on (float, str) tuples.
    stops.sort_by(|x, y| {
        x.0.partial_cmp(&y.0)
            .unwrap_or(Ordering::Equal)
            .then_with(|| x.1.cmp(&y.1))
    });
    let mut chain: Vec<(f64, u32)> = Vec::with_capacity(stops.len() + 2);
    chain.push((0.0, start_node));
    chain.extend(stops.into_iter().map(|(at, _, idx)| (at, idx)));
    chain.push((1.0, end_node));

    for w in chain.windows(2) {
        let (at_a, id_a) = w[0];
        let (at_b, id_b) = w[1];
        let length = (at_b - at_a) * total;
        let weight = length / speed_m_s;
        if forward_allowed && backward_allowed {
            out.edge(id_a, id_b, weight, length, false, row);
        } else if forward_allowed {
            out.edge(id_a, id_b, weight, length, true, row);
        } else {
            out.edge(id_b, id_a, weight, length, true, row);
        }
    }
    Ok(())
}

type BuildArrays = (
    Vec<String>,
    Vec<f64>,
    Vec<f64>,
    Vec<u32>,
    Vec<u32>,
    Vec<f64>,
    Vec<f64>,
    Vec<bool>,
    Vec<i64>,
);

/// Topology and weights for a prepared segment list.
///
/// `rows` is a list of `(wkt, connectors, forward_allowed, backward_allowed,
/// speed_m_s)` tuples, one per segment that survived Python's class and
/// geometry-null checks. `speed_m_s` is the drive speed for time-baked graphs
/// and 1.0 otherwise (so weight == length exactly, as the Python path yields).
///
/// Returns `(node_ids, lat, lon, edge_a, edge_b, weight, length_m, directed,
/// row)`: nodes in first-add order, edges as indices into node_ids in
/// add_edge order, and `row` the index into `rows` (-1 for a duplicate-endpoint
/// tie edge, which carries no name).
#[pyfunction]
fn build_graph_arrays(rows: &Bound<'_, PyAny>, earth_radius_m: f64) -> PyResult<BuildArrays> {
    let mut out = GraphOut::default();
    for (i, item) in rows.try_iter()?.enumerate() {
        let item = item?;
        let (wkt, connectors, forward_allowed, backward_allowed, speed_m_s): (
            String,
            Bound<'_, PyAny>,
            bool,
            bool,
            f64,
        ) = item.extract()?;
        let seg = Segment {
            wkt,
            connectors,
            forward_allowed,
            backward_allowed,
            speed_m_s,
        };
        add_segment(&mut out, earth_radius_m, i as i64, &seg)?;
    }
    Ok((
        out.ids,
        out.lat,
        out.lon,
        out.edge_a,
        out.edge_b,
        out.edge_w,
        out.edge_len,
        out.edge_directed,
        out.edge_row,
    ))
}

// ---------------------------------------------------------------------------
// Dijkstra (mirrors routing._dijkstra_path_to_target)
// ---------------------------------------------------------------------------

/// Heap entry ordered like Python's (time, node) tuples: time first, then
/// node index. Indices follow sorted node-id order, so this matches string order.
#[derive(PartialEq)]
struct Item {
    time: f64,
    node: u32,
}

impl Eq for Item {}

impl PartialOrd for Item {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Item {
    fn cmp(&self, other: &Self) -> Ordering {
        self.time
            .partial_cmp(&other.time)
            .unwrap_or(Ordering::Equal)
            .then(self.node.cmp(&other.node))
    }
}

/// Result: (elapsed, distance_m, path node indices, cumulative distance at each path node).
type PathResult = (f64, f64, Vec<u32>, Vec<f64>);

/// CSR adjacency, built once per graph. `weights` are raw edge weights; the
/// search divides by speed_m_s per relaxation, as the Python code does.
#[pyclass(module = "placeroot_native")]
pub struct Csr {
    indptr: Vec<usize>,
    indices: Vec<u32>,
    weights: Vec<f64>,
    lengths: Vec<f64>,
}

impl Csr {
    fn validated(
        indptr: Vec<usize>,
        indices: Vec<u32>,
        weights: Vec<f64>,
        lengths: Vec<f64>,
    ) -> PyResult<Self> {
        if indptr.is_empty() || indptr[0] != 0 {
            return Err(PyValueError::new_err(
                "indptr must start at 0 and be non-empty",
            ));
        }
        if indptr.windows(2).any(|w| w[0] > w[1]) {
            return Err(PyValueError::new_err("indptr must be non-decreasing"));
        }
        let edges = *indptr.last().unwrap_or(&0);
        if indices.len() != edges || weights.len() != edges || lengths.len() != edges {
            return Err(PyValueError::new_err(
                "indices, weights and lengths must match indptr[-1]",
            ));
        }
        let nodes = indptr.len() - 1;
        if indices.iter().any(|&j| j as usize >= nodes) {
            return Err(PyValueError::new_err("indices out of range"));
        }
        Ok(Csr {
            indptr,
            indices,
            weights,
            lengths,
        })
    }

    /// Target-terminated search. Mirrors _dijkstra_path_to_target step for step:
    /// stale-entry skip, then the max_cost cutoff, then the target test, then
    /// strict-improvement relaxation.
    fn search(
        &self,
        source: usize,
        target: usize,
        speed_m_s: f64,
        max_cost: f64,
    ) -> Option<PathResult> {
        if source == target {
            return Some((0.0, 0.0, vec![source as u32], vec![0.0]));
        }
        let n = self.indptr.len() - 1;
        let mut time_to = vec![f64::INFINITY; n];
        let mut dist_to = vec![0.0f64; n];
        let mut prev = vec![NO_PREV; n];
        time_to[source] = 0.0;
        let mut heap = BinaryHeap::new();
        heap.push(Reverse(Item {
            time: 0.0,
            node: source as u32,
        }));
        while let Some(Reverse(Item { time, node })) = heap.pop() {
            let u = node as usize;
            if time > time_to[u] {
                continue;
            }
            if time > max_cost {
                return None;
            }
            if u == target {
                let mut nodes = Vec::new();
                let mut dists = Vec::new();
                let mut cur = u;
                loop {
                    nodes.push(cur as u32);
                    dists.push(dist_to[cur]);
                    if cur == source {
                        break;
                    }
                    cur = prev[cur] as usize;
                }
                nodes.reverse();
                dists.reverse();
                return Some((time, dist_to[u], nodes, dists));
            }
            for k in self.indptr[u]..self.indptr[u + 1] {
                let v = self.indices[k] as usize;
                let nt = time + self.weights[k] / speed_m_s;
                if nt < time_to[v] {
                    time_to[v] = nt;
                    dist_to[v] = dist_to[u] + self.lengths[k];
                    prev[v] = u as u32;
                    heap.push(Reverse(Item {
                        time: nt,
                        node: v as u32,
                    }));
                }
            }
        }
        None
    }
}

#[pymethods]
impl Csr {
    #[new]
    fn py_new(
        indptr: Vec<usize>,
        indices: Vec<u32>,
        weights: Vec<f64>,
        lengths: Vec<f64>,
    ) -> PyResult<Self> {
        Csr::validated(indptr, indices, weights, lengths)
    }

    /// Number of nodes in the CSR.
    fn node_count(&self) -> usize {
        self.indptr.len() - 1
    }

    /// (elapsed, distance_m, path, path_distances) or None, for node indices.
    #[pyo3(signature = (source, target, speed_m_s=1.0, max_cost=f64::INFINITY))]
    fn dijkstra(
        &self,
        source: usize,
        target: usize,
        speed_m_s: f64,
        max_cost: f64,
    ) -> PyResult<Option<PathResult>> {
        check_index(source, self.node_count())?;
        check_index(target, self.node_count())?;
        Ok(self.search(source, target, speed_m_s, max_cost))
    }
}

fn check_index(i: usize, nodes: usize) -> PyResult<()> {
    if i >= nodes {
        return Err(PyValueError::new_err("node index out of range"));
    }
    Ok(())
}

/// Functional form of Csr.dijkstra for one-off searches (no CSR reuse).
/// Eight arguments by design: this is the flat Python-facing search signature.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (indptr, indices, weights, lengths, source_idx, target_idx, max_cost=f64::INFINITY, speed_m_s=1.0))]
fn dijkstra(
    indptr: Vec<usize>,
    indices: Vec<u32>,
    weights: Vec<f64>,
    lengths: Vec<f64>,
    source_idx: usize,
    target_idx: usize,
    max_cost: f64,
    speed_m_s: f64,
) -> PyResult<Option<PathResult>> {
    let csr = Csr::validated(indptr, indices, weights, lengths)?;
    check_index(source_idx, csr.node_count())?;
    check_index(target_idx, csr.node_count())?;
    Ok(csr.search(source_idx, target_idx, speed_m_s, max_cost))
}

#[pymodule]
fn placeroot_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(build_graph_arrays, m)?)?;
    m.add_function(wrap_pyfunction!(dijkstra, m)?)?;
    m.add_class::<Csr>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::py_repr;

    #[test]
    fn repr_matches_cpython_for_round6_range() {
        assert_eq!(py_repr(0.0), "0.0");
        assert_eq!(py_repr(-0.0), "-0.0");
        assert_eq!(py_repr(100.0), "100.0");
        assert_eq!(py_repr(-73.9), "-73.9");
        assert_eq!(py_repr(0.0001), "0.0001");
        assert_eq!(py_repr(0.00001), "1e-05");
        assert_eq!(py_repr(-0.000015), "-1.5e-05");
        assert_eq!(py_repr(1e16), "1e+16");
        assert_eq!(py_repr(1234567890123456.0), "1234567890123456.0");
    }
}
