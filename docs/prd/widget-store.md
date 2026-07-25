# PRD — Widget Store

Status: draft · Owner: FugginOld · Related: `CONTEXT.md` (domain), `CONTRIBUTING.md` (gotchas)

## Problem

The machine dashboard shows a fixed set of panels (Glances tiles, telemetry HUD,
services/containers). There's no way to add per-service widgets — Pi-hole query
counts, Proxmox VM load, qBittorrent transfer rates — the way
[gethomepage.dev/widgets](https://gethomepage.dev/widgets/) does. Users want to
browse a catalog of available widgets, add and configure them **in the browser**,
and see them on a machine's dashboard.

## Goals / success criteria

1. A browsable **catalog** ("+ Add Widget") of widget *types*, grouped by category,
   searchable, rendered in the dashboard UI.
2. Adding a widget is done **in-browser**: pick a type → fill a config form
   (URL / API key / options) → it saves and renders. No editing `config.yaml`.
3. Widgets render as panels on a **machine's dashboard**, alongside Glances and
   services, and can be removed / reordered (drag, like the Glances tiles).
4. Live data: each widget polls its service through the **server** (never the
   browser directly — CORS + secret-hiding), on a sensible interval.
5. Adding a *new* widget type is small and mechanical: one fetcher + one registry
   entry, reusing existing `collectors/` clients where they exist.

## Non-goals (v1)

- No auth/multi-user model (the dashboard is a trusted-LAN tool today; unchanged).
- No global/network-level widget board — machine dashboards only (v1).
- Not matching Homepage's full 100+ library on day one; breadth is phased.
- No custom user-authored widget types via UI (registry is code, not user data).

## Domain (add to `CONTEXT.md` glossary)

- **Widget Type** — a code-defined kind of widget: id, label, category, config
  field schema, and a fetcher. Lives in the registry; not user data.
- **Widget** — a *configured instance* of a Type, owned by one host: `{id, type,
  config, position}`. User data, persisted, holds secrets.
- **Widget Store** — the catalog UI + the persistence + the server API together.

Reuses existing terms: **Collector** (some fetchers wrap one), **Store** (the
widget store mirrors the topology `Store`'s guarded-directory pattern).

## Architecture

Mirrors the proven Glances path: `config → server proxy → panel`. Five modules.

### 1. Widget registry — `widgets/registry.py` (new, stdlib-only)
Declarative catalog. Each Type: `{id, label, category, icon, fields, fetch}`
where `fields` is a schema `[{name, label, type: text|url|password|number|bool,
required, secret}]`. Drives the catalog UI **and** server-side config validation.
`password`/`secret` fields are never returned to the browser in cleartext.

### 2. Widget fetchers — `widgets/<type>.py` (new; reuse `collectors/`)
Per Type: `fetch(cfg) -> dict` returning a small normalized stats dict for the
panel (e.g. `{"blocked_today": 12043, "pct": 31.2, "status": "ok"}`). **Same
contract as collectors: stdlib-only (`urllib`), return `{}`/`[]` on any failure,
never raise.** Where a collector client already exists (proxmox, unifi, dns/
pi-hole), the fetcher calls it; otherwise it's a few lines of `urllib`.

### 3. Widget config store — `renderers/html/widget_store.py` (new)
Guarded per-host persistence, mirroring `store.py`'s path-injection barrier
(basename + charset allowlist + realpath-inside-dir). File: `out/widgets/<host>.json`
(gitignored via `out/`). Holds each host's `[{id, type, config, position}]`.
Secrets live here, server-side only. Deep+narrow, testable with a `__main__`
self-check (the barrier is the reason to isolate it — same as `store.py`).

### 4. Server API — routes in `topo_server.py`
- `GET  /api/widget-catalog` → registry Types + field schemas (**no secrets**).
- `GET  /api/widgets?host=<id>` → this host's instances with secret fields
  **masked** (`"••••"` + `has_value: true`) **plus** each one's live fetched data
  (server-side, short-cached like Glances).
- `POST /api/widget-add    {host, type, config}` → validate vs schema, persist.
- `POST /api/widget-update {host, id, config}` → merge; a masked secret left
  unchanged keeps the stored value (don't overwrite with `••••`).
- `POST /api/widget-delete {host, id}` and `POST /api/widget-reorder {host, order}`.

### 5. Client store UI — `index.html`
- Catalog modal: Type cards with icon/label, category filter + search, "Add".
- Config form built from the Type's `fields` schema; `password` fields render
  masked and only send a new value when changed.
- Configured widgets render as panels in `dashpanel` on the machine dashboard,
  drag-reorderable (reuse the Glances tile-drag code), remove button per panel.
- **All service-supplied strings via `textContent`** (hard rule #6 — untrusted).
- Icons via the existing `/icon` endpoint (no external CDN — self-contained rule).

## Security

- **Secrets never leave the server in cleartext.** Store in `out/widgets/`
  (gitignored). List/read endpoints mask secret fields; the server does every
  outbound API call. This is the load-bearing rule — get it wrong and API keys
  leak to any LAN browser.
- **Path-injection barrier** on `host` id (reuse `store.py` pattern).
- **Fetchers never raise** and are stdlib-only (collector rule).
- **SSRF note:** in-browser config lets an admin point a widget URL that the
  *server* then fetches. Acceptable for a trusted-LAN admin tool; documented, not
  mitigated in v1 (see open questions).
- Dashboard has no auth today — the write endpoints are as exposed as the existing
  `/api/generate` / `/api/delete`. Same trust boundary; noted below.

## Phased roadmap

- **Phase 1 — framework + vertical slice.** Registry + config store + all server
  routes + catalog UI + config form + panel render + secret masking, with **2–3
  Types wired to existing collectors** (dns/pi-hole, proxmox, unifi). Proves the
  whole path end-to-end. Ships one `widget_store.py` self-check + one `test_*`.
- **Phase 2 — breadth.** Add the Homepage-common Types as small fetchers +
  registry entries: qBittorrent, Sonarr/Radarr, Jellyfin/Plex, AdGuard, Uptime
  Kuma, Portainer, Nextcloud, Speedtest-tracker, etc. Each is a self-contained add.
- **Phase 3 — polish.** Categories/search refinement, per-widget refresh interval,
  layout persistence, health/error states, info widgets (clock/weather/search).

## Decisions

- **Phase 1 starter widgets:** Pi-hole, Proxmox, UniFi (reuse existing collectors).
- **Write-endpoint auth:** none new — same trust boundary as `/api/generate` /
  `/api/delete`. Secrets still stored server-side and masked in responses.

## Open questions

1. ~~Write-endpoint auth~~ — decided: no new auth (trusted-LAN model).
2. ~~SSRF~~ — resolved: `widgets/net.py` only fetches private / loopback / link-local
   targets (a hostname must resolve entirely to private addrs, blocking DNS
   rebinding). Engine + media/pi-hole fetchers route through it; a `verify_tls`
   opt-in enables cert validation per widget. Collectors (proxmox/unifi) keep their
   own config-gated `verify_tls` and are a separate, admin-controlled path.
3. **Icons** — bundle a small local icon set, or extend `/icon` to serve them?
   (No external CDN allowed.)
4. **Placement** — v1 is machine dashboards; do we later want a global board?
5. ~~Config precedence~~ — decided: a Type may declare `config_key`; the widget's
   form values layer **over** that `config.yaml` collector block, so blank fields
   inherit already-configured credentials (proxmox/unifi). pihole has no mapping
   (its `dns` block uses different keys), so it's form-only.
