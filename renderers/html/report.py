"""Printable HTML diagnostic report from an export bundle.

Pure: a bundle dict in, bytes out. No server state, no store, no config — which is
why it lives here rather than in topo_server, and why its escaping can be tested
without booting anything.

Every scanned or host-supplied string is escaped (`_h`) because device labels are
untrusted (CONTRIBUTING.md gotcha 6), and the embedded raw JSON has its `<`
escaped so a label can never close the <script> element it sits in.
"""
from __future__ import annotations

import html as _html
import json


_REPORT_CSS = (
    "body{margin:0;background:#0b1016;color:#c6d3e2;"
    "font:13px/1.5 ui-monospace,Menlo,Consolas,monospace}"
    ".wrap{max-width:1100px;margin:0 auto;padding:24px}"
    "h1{font-size:17px;color:#7fd4ff;letter-spacing:.05em;margin:0 0 2px}"
    "h3{font-size:11px;color:#9fb2c6;letter-spacing:.08em;text-transform:uppercase;margin:16px 0 6px}"
    ".host{border:1px solid #1e2a38;border-radius:6px;padding:18px 20px;margin:18px 0;background:#0e141c}"
    ".meta{color:#6b7d90;font-size:11px}"
    "table{border-collapse:collapse;width:100%;margin:4px 0;font-size:12px}"
    "th,td{text-align:left;padding:4px 8px;border-bottom:1px solid #16202c;vertical-align:top;"
    "word-break:break-word}"
    "th{color:#6b7d90;font-weight:400;text-transform:uppercase;font-size:10px}"
    "td.k{color:#8fa2b6;width:190px}"
    ".empty{color:#5a6b7d;font-style:italic;padding:3px 0}"
    ".tag{display:inline-block;padding:1px 7px;border:1px solid #1e2a38;border-radius:3px;"
    "color:#7fd4ff;font-size:10px;margin-left:8px}"
    "button{font:inherit;background:transparent;border:1px dashed #2a3a4c;color:#7fd4ff;"
    "padding:7px 12px;border-radius:4px;cursor:pointer;margin-top:16px}"
    "button:hover{border-color:#7fd4ff;background:rgba(127,212,255,.08)}"
)


def _h(v) -> str:
    """Scalar -> escaped text; dict/list -> compact escaped JSON."""
    if isinstance(v, (dict, list)):
        v = json.dumps(v, separators=(",", ":"))
    return _html.escape("" if v is None else str(v))


def _table(rows: list, cols: list) -> str:
    head = "".join(f"<th>{_h(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{_h(r.get(c))}</td>" for c in cols) + "</tr>"
                   for r in rows if isinstance(r, dict))
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _kv(d: dict) -> str:
    rows = "".join(f'<tr><td class="k">{_h(k)}</td><td>{_h(v)}</td></tr>'
                   for k, v in d.items() if k != "error")
    return f"<table>{rows}</table>" if rows else '<div class="empty">none</div>'


def _host_report(b: dict) -> str:
    topo = b.get("topology") if isinstance(b.get("topology"), dict) else {}
    nodes, vlans = topo.get("nodes") or [], topo.get("vlans") or []
    name = topo.get("name") or b.get("id") or "host"
    out = [f'<div class="host"><h1>{_h(name)}<span class="tag">{_h(b.get("id"))}</span></h1>'
           f'<div class="meta">exported {_h(b.get("exported"))} · {len(nodes)} nodes · '
           f'{len(vlans)} vlans</div><h3>Topology</h3>']
    if nodes:
        cols = [c for c in ("label", "kind", "sub", "up", "cap", "cls", "grp", "meta", "parent")
                if any(c in n for n in nodes if isinstance(n, dict))]
        out.append(_table(nodes, cols))
    else:
        out.append('<div class="empty">no nodes</div>')
    if vlans:
        out.append("<h3>VLANs</h3>" + _table(vlans, sorted({k for v in vlans for k in v})))
    for label, data in (("Telemetry", b.get("telemetry")), ("Glances", b.get("glances"))):
        out.append(f"<h3>{label}</h3>")
        out.append(_kv(data) if isinstance(data, dict) and data else '<div class="empty">none</div>')
    svc = b.get("services") if isinstance(b.get("services"), dict) else {}
    conts = svc.get("containers") or []
    out.append("<h3>Services</h3>")
    if conts:
        cols = [c for c in ("name", "state", "status", "image", "project", "cpu", "memp", "engine")
                if any(c in c2 for c2 in conts if isinstance(c2, dict))]
        out.append(_table(conts, cols))
    else:
        out.append(f'<div class="empty">{_h(svc.get("error") or "none reported")}</div>')
    out.append("</div>")
    return "".join(out)


def export_html(obj: dict) -> bytes:
    hosts = obj.get("hosts") if isinstance(obj.get("hosts"), list) else [obj]
    title = ("Dashboard diagnostics" if "hosts" in obj
             else "Diagnostics — " + str((obj.get("topology") or {}).get("name") or obj.get("id") or "host"))
    body = "".join(_host_report(h) for h in hosts if isinstance(h, dict))
    raw = json.dumps(obj, separators=(",", ":")).replace("<", "\\u003c")   # can't break out of <script>
    meta = f'generated {_h(obj.get("exported"))}' + (f' · server {_h(obj.get("server"))}' if obj.get("server") else "")
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>{_h(title)}</title>'
        f'<style>{_REPORT_CSS}</style></head><body><div class="wrap">'
        f'<h1 style="font-size:19px">{_h(title)}</h1><div class="meta">{meta}</div>'
        f'{body}<script type="application/json" id="raw">{raw}</script>'
        '<button onclick="var b=new Blob([document.getElementById(\'raw\').textContent],'
        '{type:\'application/json\'}),a=document.createElement(\'a\');a.href=URL.createObjectURL(b);'
        'a.download=document.title.replace(/[^A-Za-z0-9._-]/g,\'-\')+\'.json\';a.click()">'
        '⭳ Download raw JSON</button></div></body></html>'
    ).encode()


if __name__ == "__main__":   # ponytail: the escaping invariant, offline
    # a hostile device label must not reach the document as markup ...
    evil = "</script><img src=x onerror=alert(1)>"
    out = export_html({"id": "h", "exported": "t",
                       "topology": {"name": evil, "nodes": [{"label": evil, "kind": "nic"}]}})
    assert b"<img src=x" not in out, "device label escaped into markup"
    assert b"&lt;/script&gt;" in out, "label not html-escaped"
    # ... and must not close the <script> holding the raw payload
    raw = out.split(b'id="raw">')[1].split(b"</script>")[0]
    assert b"</script>" not in raw and rb"\u003c" in raw, raw[:200]
    # shape: whole-dashboard bundle vs single host, and an empty one
    assert b"Dashboard diagnostics" in export_html({"hosts": [], "exported": "t"})
    assert "Diagnostics" in export_html({"id": "h9"}).decode() and b"h9" in export_html({"id": "h9"})
    assert export_html({}).startswith(b"<!doctype html>")
    assert b"no nodes" in export_html({"id": "h", "topology": {"nodes": []}})
    print("renderers/html/report self-check ok")
