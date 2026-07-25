"""Dashboard routing test — boots the real topo_server on a free port and hits it.

Offline: only localhost is touched. Covers the contract every GET route shares —
a query string must not change which route matches — plus the JSON/HTML shapes the
dashboard's fetch() calls depend on.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port: int, path: str):
    """(status, body bytes) — 4xx/5xx come back as a status, not an exception."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _download_headers(port: int, path: str):
    """(status, headers) for a download route — the response headers are the point."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=15) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)


def _serve():
    """Start the server, wait for it to answer, yield the port, always kill it."""
    port = _free_port()
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "renderers", "html", "topo_server.py"),
                          "--port", str(port)], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):                      # ~10s for a cold interpreter + imports
        if p.poll() is not None:
            raise AssertionError(f"server died: {p.stderr.read().decode()[-500:]}")
        try:
            _get(port, "/api/list")
            return port, p
        except OSError:
            time.sleep(0.1)
    p.kill()
    raise AssertionError("server never came up")


def test_routes():
    port, proc = _serve()
    try:
        st, body = _get(port, "/api/list")
        assert st == 200 and isinstance(json.loads(body), list), (st, body[:200])

        # a query string must not stop a route from matching — the dashboard appends
        # cache-busters, and every API route is matched on the path alone
        st_q, body_q = _get(port, "/api/list?t=123")
        assert st_q == 200 and json.loads(body_q) == json.loads(body), (st_q, body_q[:200])

        st, body = _get(port, "/api/widget-catalog?x=1")
        cat = json.loads(body)
        assert st == 200 and any(w["built"] for w in cat), st

        st, body = _get(port, "/api/telemetry?host=none")
        assert st == 200 and isinstance(json.loads(body), dict), st

        st, body = _get(port, "/")
        assert st == 200 and b"<title>Topographer</title>" in body, st

        assert _get(port, "/no/such/thing")[0] == 404
        assert _get(port, "/t/../secret.json")[0] in (400, 404)   # store guard holds over HTTP

        # export: an id that isn't in the store is refused outright, so nothing
        # request-derived can reach the Content-Disposition filename
        for bad in ("../../etc/passwd", "x%0d%0aSet-Cookie:+a%3db", 'a"b', "nope"):
            assert _get(port, f"/api/export?id={bad}")[0] == 404, bad
        st, hdrs = _download_headers(port, "/api/export")         # whole-dashboard export
        assert st == 200, st
        cd = hdrs.get("Content-Disposition", "")
        assert cd.startswith('attachment; filename="dashboard-') and cd.endswith('.json"'), cd
        assert "\r" not in cd and "\n" not in cd
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    test_routes()
    print("all tests passed")
