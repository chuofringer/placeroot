"""The on-disk graph cache is unpickled on load, so a file is only ever
loaded when it could only have been written by this user: owned by the
current uid, not group/world writable, inside a 0o700 directory. Anything
else is refused and rebuilt (never trusted).
"""

import os
import pickle

import pytest

from placeroot import cache, routing

_BBOX = (-73.91, 40.69, -73.89, 40.71)


def _graph() -> routing.Graph:
    graph = routing.Graph()
    graph.add_node("a", 40.7, -73.9)
    graph.add_node("b", 40.7, -73.899)
    graph.add_edge("a", "b", 85.0, 85.0)
    return graph


def _write_payload(path, graph=None):
    payload = {
        "format": routing.GRAPH_DISK_FORMAT,
        "bbox": _BBOX,
        "radius_m": 500.0,
        "graph": graph or _graph(),
    }
    with open(path, "wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.chmod(path, 0o600)


posix_only = pytest.mark.skipif(not hasattr(os, "getuid"), reason="uid/mode checks are POSIX")


def test_private_file_owned_by_us_loads(tmp_path):
    path = tmp_path / "walk_x.pkl"
    _write_payload(path)
    loaded = routing._load_one_graph_file(path)
    assert loaded is not None
    bbox, graph = loaded
    assert bbox == _BBOX
    assert graph.node_count() == 2


@posix_only
@pytest.mark.parametrize("mode", [0o666, 0o664, 0o622, 0o602])
def test_group_or_world_writable_file_is_refused(tmp_path, mode, caplog):
    path = tmp_path / "walk_x.pkl"
    _write_payload(path)
    os.chmod(path, mode)
    assert routing._load_one_graph_file(path) is None
    assert "refused" in caplog.text and "writable" in caplog.text


@posix_only
def test_file_owned_by_another_uid_is_refused(tmp_path, monkeypatch, caplog):
    path = tmp_path / "walk_x.pkl"
    _write_payload(path)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    assert routing._load_one_graph_file(path) is None
    assert "refused" in caplog.text and "owned by uid" in caplog.text


def test_untrusted_file_is_never_unpickled(tmp_path, monkeypatch):
    # The ownership check runs before pickle.load — a planted file is
    # refused on stat alone, its bytes never interpreted.
    path = tmp_path / "walk_x.pkl"
    _write_payload(path)
    if hasattr(os, "getuid"):
        os.chmod(path, 0o666)
    else:
        pytest.skip("uid/mode checks are POSIX")

    def boom(*args, **kwargs):
        raise AssertionError("pickle.load must not run on a refused file")

    monkeypatch.setattr(routing.pickle, "load", boom)
    assert routing._load_one_graph_file(path) is None


@posix_only
def test_refusal_falls_through_to_rebuild_in_disk_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    key_prefix = ("2099-01-01.0", "fake://segments", "walk", "s1.4", "")
    root = routing._graph_disk_root(key_prefix[0])
    root.mkdir(parents=True)
    path = root / routing._graph_disk_name("walk", "s1.4", (0, 0), 500.0, False, "")
    _write_payload(path)
    os.chmod(path, 0o666)
    needed = (-73.905, 40.695, -73.895, 40.705)
    assert routing._load_graph_from_disk(key_prefix, needed, False, 40.7, -73.9) is None
    os.chmod(path, 0o600)
    assert routing._load_graph_from_disk(key_prefix, needed, False, 40.7, -73.9) is not None


@posix_only
def test_persist_creates_a_private_graph_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    assert cache.enabled()
    key_prefix = ("2099-01-01.0", "fake://segments", "walk", "s1.4", "")
    routing._persist_graph_to_disk(key_prefix, 40.7, -73.9, 500.0, _BBOX, _graph())
    root = routing._graph_disk_root(key_prefix[0])
    assert root.is_dir()
    assert os.stat(root).st_mode & 0o077 == 0
    (written,) = list(root.glob("*.pkl"))
    assert routing._load_one_graph_file(written) is not None
