# Adding a widget to the store

The store lists every Homepage service widget (`docs/references/homepage-widgets.md`),
but a widget only shows live data once it has a backend on our side. Most take
~10 lines of **data**; the awkward ones take a small function. You need a running
instance of the service to verify it — that's the whole point of who adds what.

Two paths:

## 1. Engine definition (the common case — api-key / basic / bearer / token)

Add an entry to `widgets/definitions.py`. The generic engine (`widgets/engine.py`)
does the HTTP + field extraction. No Python function, no UI, no per-widget test.

```python
{
    "id": "adguard",                 # MUST match the id in widgets/catalog.json
    "label": "AdGuard Home", "category": "Network", "icon": "adguard-home",
    "desc": "DNS queries, blocked, protection status.",
    "auth": "basic",                 # none | apikey-header | apikey-query | bearer | token | basic
    # "header": "X-Api-Key",         # apikey-header only (default X-Api-Key)
    # "param":  "apikey",            # apikey-query only (default apikey)
    # "params": [{"name":"env","label":"Environment ID","required":True}],  # extra config/path params
    "calls": {                       # named endpoints, relative to the configured url; {cfg-key} is templated
        "stats":  "/control/stats",
        "status": "/control/status",
    },
    "show": [                        # what the panel displays; each reads from one call
        {"key": "queries", "call": "stats",  "path": "num_dns_queries"},
        {"key": "blocked", "call": "stats",  "path": "num_blocked_filtering"},
        {"key": "block_pct", "call": "stats", "op": "ratio",
         "num": "num_blocked_filtering", "den": "num_dns_queries", "fmt": "pct"},
        {"key": "protection", "call": "status", "path": "protection_enabled"},
    ],
}
```

**Where the values come from:** the `calls` (endpoints + auth style) are in
Homepage's source — `src/widgets/<id>/widget.js` (its `api` template shows the
auth: `?apikey={key}` → `apikey-query`; a `credentialedProxyHandler` → a header).
The `path`s are the JSON keys in that endpoint's response — check the service's
own API, or `docs/references/homepage-widgets-full.md` for the config + fields.

**`show` field ops** (in `engine.py`):

| op | meaning |
|----|---------|
| *(none)* | value at `path` (dotted, e.g. `queries.total`) |
| `len` | length of the list/dict at `path` (or the whole call) |
| `count_where:F=V` | count list items where `item[F] == V` |
| `ratio` (+ `num`,`den`) | `100 * num / den`; add `"fmt":"pct"` to round |

`fmt`: `pct` or `round`. Key names drive display formatting — a key ending
`_bps` renders as a byte rate, `_pct`/`pct` as a percentage (see `index.html`
`wvFmt`).

The `id` must exist in `widgets/catalog.json` (it's already there for all 155) so
the store flips it from "not built" to installable automatically.

## 2. Custom fetcher (auth-weird — cookie login, CSRF, Subsonic token, …)

If the service needs a login round-trip or a signed token (qBittorrent, Deluge,
Navidrome, Home Assistant's helper, …), the engine can't express it. Write a
function in `widgets/fetchers.py` and a normal registry entry:

```python
# widgets/fetchers.py — collector contract: stdlib only, NEVER raise, {} on failure
def qbittorrent(cfg):
    base = (cfg.get("url") or "").rstrip("/")
    if not base:
        return {}
    ...  # login -> cookie, then GET stats; return a small {key: value} dict
```

```python
# widgets/registry.py — add to the literal CATALOG list
{
    "id": "qbittorrent", "label": "qBittorrent", "category": "Downloads",
    "icon": "qbittorrent", "desc": "Transfer speeds + active torrents.",
    "fields": [
        {"name": "url", "label": "URL", "type": "url", "required": True},
        {"name": "username", "label": "Username", "type": "text"},
        {"name": "password", "label": "Password", "type": "password", "secret": True},
    ],
    "fetch": fetchers.qbittorrent,
},
```

Reuse `fetchers._get_json` (unverified TLS, never raises) for the HTTP.

## Verify it (required)

1. Point it at your live instance:
   ```
   PYTHONPATH=vendor python3 -c "import sys; sys.path[:0]=['.','renderers/html']; \
     from widgets import engine, definitions; \
     d=[x for x in definitions.DEFS if x['id']=='adguard'][0]; \
     print(engine.fetch(d, {'url':'http://ADGUARD:3000','username':'u','password':'p'}))"
   ```
2. Or add it in the dashboard and hit the panel's **⟳ test** button — it fetches
   fresh and flags the health dot green (data) or red (nothing → wrong path/auth).
3. Run the self-checks: `python -m widgets.registry` and `python widgets/engine.py`.

## Submit

Open a PR with just your definition (or fetcher + registry entry). Because you run
the service, your paths are verified — that's coverage we can trust, versus
bulk-guessing widgets nobody here can test.
