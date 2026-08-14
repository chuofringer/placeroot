"""Guards against the import fragilities behind "No module named 'placeroot.data'".

A user's environment carried a dead editable install of an old placeroot,
and the one call site that imported ``placeroot.data`` as a module was the
one tool that failed. These tests pin the two properties that make that
class of failure impossible: the package is a regular package (one
directory, no namespace assembly), and every bundled-data reader reaches
data files by traversal from the package root rather than by importing a
data "module".
"""

import importlib.metadata
import re
from pathlib import Path

from placeroot import server

SRC = Path(server.__file__).parent


def test_placeroot_is_a_regular_package():
    assert (SRC / "__init__.py").is_file()


def test_no_dotted_resource_anchor_into_data():
    # files("placeroot.data") imports placeroot.data; files("placeroot")/"data"
    # does not. Only the latter is allowed.
    offenders = []
    for path in SRC.rglob("*.py"):
        if re.search(r"""files\(\s*['"]placeroot\.""", path.read_text()):
            offenders.append(path.name)
    assert offenders == []


def test_server_reports_the_package_version():
    mcp = server.build_server()
    assert mcp.version == importlib.metadata.version("placeroot")
