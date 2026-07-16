"""Shared Collector transport — one home for the unverified-TLS urllib GET/POST
that proxmox / unifi / opnsense / dns each hand-rolled (stdlib only, no requests).

`get_json` returns parsed JSON or None on any failure (collectors never raise) and
logs a *redacted* URL (query stripped, so query-param tokens don't leak) + the
error — the diagnostic that surfaced the Proxmox 401. NOT SSRF-guarded: collector
URLs come from admin `config.yaml`; `widgets/net.py` owns the private-only guarded
variant for user-supplied widget URLs (the guard is why they stay separate).
"""
from __future__ import annotations

import base64
import json
import logging
import ssl
import urllib.error
import urllib.request

log = logging.getLogger("collector.http")


def ssl_ctx(verify: bool):
    """Default TLS context, or one that skips verification for self-signed hosts."""
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def get_json(url, *, headers=None, verify=False, method=None, json_body=None,
             basic_auth=None, timeout=15):
    """GET/POST -> parsed JSON, or None on any failure. json_body sets a POST."""
    hdr = dict(headers or {})
    if basic_auth:
        hdr["Authorization"] = "Basic " + base64.b64encode(
            f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdr.setdefault("Content-Type", "application/json")
        method = method or "POST"
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx(verify)) as r:
            return json.load(r)
    except (urllib.error.URLError, ValueError, OSError) as e:
        log.warning("%s failed: %s", url.split("?")[0], e)   # redact query (may hold a token)
        return None


if __name__ == "__main__":   # ponytail: ctx toggle + basic-auth/json POST shaping, offline
    import io
    assert ssl_ctx(True).verify_mode == ssl.CERT_REQUIRED
    assert ssl_ctx(False).verify_mode == ssl.CERT_NONE
    seen = {}

    def _stub(req, timeout=None, context=None):
        seen.update(auth=req.headers.get("Authorization"), method=req.get_method(),
                    ctype=req.headers.get("Content-type"))
        return io.BytesIO(b'{"ok": 1}')

    _orig = urllib.request.urlopen
    urllib.request.urlopen = _stub
    try:
        r = get_json("http://h/x?secret=abc", basic_auth=("u", "p"), json_body={"a": 1})
    finally:
        urllib.request.urlopen = _orig
    assert r == {"ok": 1}, r
    assert seen["auth"].startswith("Basic ") and seen["method"] == "POST"
    assert seen["ctype"] == "application/json"
    print("collectors/transport self-check ok")
