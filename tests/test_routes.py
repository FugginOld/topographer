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
        _snapshot_round_trip(port)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _post(port: int, path: str, obj):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _snapshot_round_trip(port: int):
    """Export a snapshot, restore a crafted one, confirm it lands — then clean up.
    Runs against the real store, so everything it creates is deleted again."""
    st, hdrs = _download_headers(port, "/api/snapshot")
    assert st == 200 and "topographer-snapshot-" in hdrs.get("Content-Disposition", ""), hdrs
    st, body = _get(port, "/api/snapshot")
    snap = json.loads(body)
    assert snap["kind"] == "topographer-snapshot" and snap["version"] == 1, snap
    assert isinstance(snap["topologies"], list) and isinstance(snap["widgets"], dict)

    # restoring a server's own snapshot must be a no-op, not a duplicate-maker:
    # stored ids are not always slug(name) — a locally generated one is timestamped
    before = sorted(r["id"] for r in json.loads(_get(port, "/api/list")[1]))
    assert _post(port, "/api/snapshot-restore", snap)[0] == 200
    after = sorted(r["id"] for r in json.loads(_get(port, "/api/list")[1]))
    assert before == after, f"self-restore changed the store: {before} -> {after}"

    # a foreign file is refused outright
    assert _post(port, "/api/snapshot-restore", {"kind": "something-else"})[0] == 400
    assert _post(port, "/api/snapshot-restore", {"nodes": []})[0] == 400

    # shaped like a real snapshot: the id is the slug the store already uses, and
    # the widget key matches it (they come from widget_store.hosts(), already slugged)
    name, sid = "selftest snapshot marker", "selftest-snapshot-marker"
    restore = {"kind": "topographer-snapshot", "version": 1,
               "topologies": [
                   {"id": sid, "topology": {"name": name, "nodes": [{"id": "n0", "label": "cpu"}]}},
                   {"id": "no-topology-key"},
                   {"id": "../../escape", "topology": {"nodes": [{"id": "n0"}]}},  # traversal, no name -> skipped
                   "not even a dict"],
               "widgets": {sid: [{"id": "w1", "type": "pihole", "config": {"url": "http://x"}}],
                           "../../etc/passwd": [{"id": "w9", "type": "pihole", "config": {}}]}}
    tid = None
    try:
        st, res = _post(port, "/api/snapshot-restore", restore)
        assert st == 200 and res["topologies"] == 1, (st, res)   # junk + traversal dropped
        assert res["widgets"] == 1 and res["hosts"] == 1, res
        assert res["skipped"] == 2, res                          # both traversal attempts
        row = next((r for r in json.loads(_get(port, "/api/list")[1]) if r["name"] == name), None)
        assert row, "restored topology is not in the list"
        tid = row["id"]
        assert tid == sid, f"the snapshot's own id should be kept, got {tid}"
        assert json.loads(_get(port, f"/api/widgets?host={tid}")[1])[0]["type"] == "pihole"
    finally:
        if tid:                                   # leave the real store exactly as we found it
            _post(port, "/api/delete", {"id": tid})
            try:
                os.remove(os.path.join(ROOT, "out", "widgets", f"{tid}.json"))
            except OSError:
                pass


if __name__ == "__main__":
    test_routes()
    print("all tests passed")
