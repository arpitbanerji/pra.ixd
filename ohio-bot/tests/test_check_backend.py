"""Tests for the check_backend diagnostic script.

These drive a real local HTTP server rather than mocking httpx, because the bug
being guarded against is a status-code handling mistake: httpx does not raise on
4xx, so a 401 body parsed as JSON and the check reported success.
"""

import importlib.util
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_backend", REPO_ROOT / "scripts" / "check_backend.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_backend = _load_module()


class _Handler(BaseHTTPRequestHandler):
    settings_status = 200

    def do_GET(self):  # noqa: N802 - required by http.server
        if self.path.startswith("/server_info"):
            self._respond(200, b'{"version": "9.9.9"}')
        elif self.path.startswith("/api/settings"):
            self._respond(self.settings_status, b'{"detail": "rejected"}')
        else:
            self._respond(404, b"{}")

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output quiet
        pass


@pytest.fixture
def stub_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_rejected_key_is_not_reported_as_success(stub_server):
    # Regression: /server_info answers anonymously, so reaching it proves
    # nothing about the key. Only /api/settings can prove authentication.
    _Handler.settings_status = 401
    reachable, authenticated, detail = check_backend.check(stub_server, {"X-Session-API-Key": "x"})
    assert reachable is True
    assert authenticated is False
    assert "401" in detail


def test_forbidden_credentials_are_an_auth_failure(stub_server):
    _Handler.settings_status = 403
    reachable, authenticated, _ = check_backend.check(stub_server, {})
    assert reachable is True
    assert authenticated is False


def test_accepted_credentials_report_the_version(stub_server):
    _Handler.settings_status = 200
    reachable, authenticated, detail = check_backend.check(stub_server, {})
    assert reachable is True
    assert authenticated is True
    assert "9.9.9" in detail


def test_unreachable_server_is_distinguished_from_auth_failure():
    # Port 1 is never listening; this must not be reported as an auth problem.
    reachable, authenticated, _ = check_backend.check("http://127.0.0.1:1", {}, timeout=2.0)
    assert reachable is False
    assert authenticated is False


def test_probe_candidates_include_the_expected_topologies():
    joined = " ".join(check_backend.PROBE_CANDIDATES)
    assert "127.0.0.1" in joined
    assert "host.docker.internal" in joined
