"""Offline coverage for scripts/check_listings.sh Cloudflare challenge handling (#495).

The quarterly listings check must not treat every HTTP 403 as an outage:
Cloudflare Bot Fight Mode returns 403 + cf-mitigated: challenge for curl
while browsers still load placeroot.dev. A plain origin 403 must still fail.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_listings.sh"

# The script under test is bash + curl; a runner without both (a bare
# Windows box, say — GitHub's windows-latest does ship Git Bash and curl)
# cannot exercise it, which is a missing tool, not a regression.
# On windows-latest `bash` on PATH is System32's WSL launcher, which prints
# "Windows Subsystem for Linux has no installed distributions" and exits 1
# before Git Bash is ever consulted; the script is not meaningfully testable
# there.
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("curl") is None or sys.platform == "win32",
    reason="scripts/check_listings.sh needs a POSIX bash and curl on PATH",
)

CF_CHALLENGE_BODY = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    "<body><script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'>"
    "</script></body></html>"
)
LIVE_HTML = (
    "<!DOCTYPE html><html><head><title>PlaceRoot — maps &amp; places</title></head>"
    "<body><h1>PlaceRoot</h1></body></html>"
)
WHY_HTML = (
    "<!DOCTYPE html><html><head><title>Why we built PlaceRoot</title></head>"
    "<body><h1>Why</h1></body></html>"
)
WRONG_HTML = (
    "<!DOCTYPE html><html><head><title>Welcome to nginx</title></head>"
    "<body><h1>Welcome to nginx!</h1></body></html>"
)


class _ChallengeHandler(BaseHTTPRequestHandler):
    """Custom-domain stand-in: CF challenge on HTML paths, plain 403 on /hard-403."""

    def log_message(self, format, *args):  # noqa: A003 — stdlib signature
        return

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path == "/hard-403":
            body = b"Forbidden"
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/why-placeroot"):
            body = CF_CHALLENGE_BODY.encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=UTF-8")
            self.send_header("cf-mitigated", "challenge")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


class _OriginHandler(BaseHTTPRequestHandler):
    """Pages-origin stand-in. mode: live | challenge | down | wrong."""

    mode = "live"

    def log_message(self, format, *args):  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if _OriginHandler.mode == "down":
            self.send_response(503)
            self.end_headers()
            return
        if _OriginHandler.mode == "wrong":
            body = WRONG_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=UTF-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if _OriginHandler.mode == "challenge":
            body = CF_CHALLENGE_BODY.encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=UTF-8")
            self.send_header("cf-mitigated", "challenge")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/why-placeroot":
            body = WHY_HTML.encode()
        elif path in ("/", ""):
            body = LIVE_HTML.encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def dual_servers():
    custom = ThreadingHTTPServer(("127.0.0.1", 0), _ChallengeHandler)
    origin = ThreadingHTTPServer(("127.0.0.1", 0), _OriginHandler)
    for srv in (custom, origin):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    custom_base = f"http://127.0.0.1:{custom.server_address[1]}"
    origin_host = f"127.0.0.1:{origin.server_address[1]}"
    try:
        yield {
            "custom_base": custom_base,
            "origin_host": origin_host,
            "set_origin_mode": partial(setattr, _OriginHandler, "mode"),
        }
    finally:
        _OriginHandler.mode = "live"
        custom.shutdown()
        origin.shutdown()


def _run_check(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    full_env.update(env)
    full_env["CHECK_LISTINGS_SKIP_REGISTRY"] = "1"
    full_env["CHECK_LISTINGS_SKIP_UVX"] = "1"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_challenge_plus_live_origin_passes(dual_servers):
    dual_servers["set_origin_mode"]("live")
    custom = dual_servers["custom_base"]
    custom_host = custom.removeprefix("http://")
    origin = dual_servers["origin_host"]
    result = _run_check(
        {
            "CHECK_LISTINGS_CUSTOM_DOMAIN_HOST": custom_host,
            "CHECK_LISTINGS_PAGES_ORIGIN_HOST": origin,
            "CHECK_LISTINGS_PAGES_ORIGIN_SCHEME": "http",
            "CHECK_LISTINGS_SITE_URL": f"{custom}/",
            "CHECK_LISTINGS_WHY_URL": f"{custom}/why-placeroot",
            # Glama is a different host — no CF fallback; give it a real 200.
            "CHECK_LISTINGS_GLAMA_URL": f"http://{origin}/",
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Cloudflare bot challenge" in result.stdout
    assert "content verified via" in result.stdout
    assert "### Summary" not in result.stdout


def test_plain_403_still_fails(dual_servers):
    dual_servers["set_origin_mode"]("live")
    custom = dual_servers["custom_base"]
    custom_host = custom.removeprefix("http://")
    origin = dual_servers["origin_host"]
    result = _run_check(
        {
            "CHECK_LISTINGS_CUSTOM_DOMAIN_HOST": custom_host,
            "CHECK_LISTINGS_PAGES_ORIGIN_HOST": origin,
            "CHECK_LISTINGS_PAGES_ORIGIN_SCHEME": "http",
            "CHECK_LISTINGS_SITE_URL": f"{custom}/hard-403",
            "CHECK_LISTINGS_WHY_URL": f"http://{origin}/why-placeroot",
            "CHECK_LISTINGS_GLAMA_URL": f"http://{origin}/",
        }
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "hard-403" in result.stdout
    assert "returned HTTP 403" in result.stdout
    assert "content verified via" not in result.stdout


def test_challenge_fails_when_origin_also_challenged(dual_servers):
    dual_servers["set_origin_mode"]("challenge")
    custom = dual_servers["custom_base"]
    custom_host = custom.removeprefix("http://")
    origin = dual_servers["origin_host"]
    result = _run_check(
        {
            "CHECK_LISTINGS_CUSTOM_DOMAIN_HOST": custom_host,
            "CHECK_LISTINGS_PAGES_ORIGIN_HOST": origin,
            "CHECK_LISTINGS_PAGES_ORIGIN_SCHEME": "http",
            "CHECK_LISTINGS_SITE_URL": f"{custom}/",
            "CHECK_LISTINGS_WHY_URL": f"{custom}/why-placeroot",
            # Different host than CUSTOM_DOMAIN_HOST, also challenged → no rewrite.
            "CHECK_LISTINGS_GLAMA_URL": f"http://{origin}/",
        }
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "content not verified" in result.stdout
    assert "no Pages origin fallback" in result.stdout


def test_challenge_fails_when_origin_down(dual_servers):
    dual_servers["set_origin_mode"]("down")
    custom = dual_servers["custom_base"]
    custom_host = custom.removeprefix("http://")
    origin = dual_servers["origin_host"]
    result = _run_check(
        {
            "CHECK_LISTINGS_CUSTOM_DOMAIN_HOST": custom_host,
            "CHECK_LISTINGS_PAGES_ORIGIN_HOST": origin,
            "CHECK_LISTINGS_PAGES_ORIGIN_SCHEME": "http",
            "CHECK_LISTINGS_SITE_URL": f"{custom}/",
            "CHECK_LISTINGS_WHY_URL": f"{custom}/why-placeroot",
            "CHECK_LISTINGS_GLAMA_URL": f"http://{origin}/",
        }
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "content not verified" in result.stdout


def test_challenge_fails_when_origin_lacks_placeroot_title(dual_servers):
    dual_servers["set_origin_mode"]("wrong")
    custom = dual_servers["custom_base"]
    custom_host = custom.removeprefix("http://")
    origin = dual_servers["origin_host"]
    result = _run_check(
        {
            "CHECK_LISTINGS_CUSTOM_DOMAIN_HOST": custom_host,
            "CHECK_LISTINGS_PAGES_ORIGIN_HOST": origin,
            "CHECK_LISTINGS_PAGES_ORIGIN_SCHEME": "http",
            "CHECK_LISTINGS_SITE_URL": f"{custom}/",
            "CHECK_LISTINGS_WHY_URL": f"{custom}/why-placeroot",
            "CHECK_LISTINGS_GLAMA_URL": f"http://{origin}/",
        }
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "content not verified" in result.stdout
    assert "content verified via" not in result.stdout


def test_unreachable_url_reports_single_000(dual_servers):
    dual_servers["set_origin_mode"]("live")
    origin = dual_servers["origin_host"]
    result = _run_check(
        {
            "CHECK_LISTINGS_SITE_URL": "http://127.0.0.1:1/",
            "CHECK_LISTINGS_WHY_URL": f"http://{origin}/why-placeroot",
            "CHECK_LISTINGS_GLAMA_URL": f"http://{origin}/",
        }
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "-> 000\n" in result.stdout
    assert "000000" not in result.stdout
