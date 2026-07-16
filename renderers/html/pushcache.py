"""PushCache — per-host samples pushed by remote agents.

The push model lived in three places (telemetry / glances / services) as the same
`{host: {t, data}}` dict + freshness read + ingest write, hand-written each time.
This is that invariant, once: `put()` timestamps a sample, `get()` returns it
tagged `live`/`stale`, or `None` if a host never pushed. The key policy is injected
(the server passes `store.stable_slug`), so this stays stdlib-only and self-testable.
The per-channel fallbacks (local host, miss → zeros/{}/error) are NOT shared and
stay in each getter. See CONTEXT.md → Store.
"""
from __future__ import annotations

import time


class PushCache:
    def __init__(self, fresh_seconds: float, key=lambda h: h):
        self._fresh = fresh_seconds
        self._key = key
        self._d: dict[str, dict] = {}

    def put(self, host: str, data: dict) -> str:
        """Store a host's latest sample; returns the keyed id."""
        hid = self._key(host)
        self._d[hid] = {"t": time.monotonic(), "data": data}
        return hid

    def get(self, host: str) -> dict | None:
        """The host's sample tagged {live|stale}, or None if it never pushed."""
        h = self._d.get(self._key(host))
        if not h:
            return None
        tag = "live" if time.monotonic() - h["t"] < self._fresh else "stale"
        return {**h["data"], tag: True}


if __name__ == "__main__":   # ponytail: fresh->live / stale->stale / miss->None
    c = PushCache(0.05)
    assert c.get("h") is None                              # miss
    assert c.put("h", {"x": 1}) == "h"
    assert c.get("h") == {"x": 1, "live": True}            # fresh
    time.sleep(0.06)
    assert c.get("h") == {"x": 1, "stale": True}           # aged out
    c2 = PushCache(10, key=str.upper)                      # injected key policy
    assert c2.put("ab", {"y": 2}) == "AB" and c2.get("ab")["live"]
    print("pushcache self-check ok")
