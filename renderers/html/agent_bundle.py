"""The reporting agent's file set, packed as a tar.gz / zip the server serves to
fresh machines. bootstrap.sh fetches /agent.tar.gz and extracts it, so clients
run the client code from THIS server, not github.

Lifted out of topo_server.py: a pure bytes producer over the repo tree. To ship
new agent-side code to clients, edit AGENT_PATHS here, restart the server, then
re-run bootstrap on the client (see CONTRIBUTING.md → gotcha 8).
"""
from __future__ import annotations

import io
import os
import tarfile
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))

# Union of both OSes' needs: the Windows scanner (make_pc_topo.py) pulls
# renderers/card.py; the Linux one is self-contained. Extra files on the other OS
# are a few harmless KB.
AGENT_PATHS = ["agent", "scanners/make_linux_topo.py", "scanners/make_pc_topo.py",
               "scanners/scan_services.py", "core/__init__.py", "core/local_telemetry.py",
               "core/glances.py",
               "renderers/__init__.py", "renderers/card.py"]


def _agent_files():
    """(arcname, abspath) for every real file under AGENT_PATHS, __pycache__ skipped."""
    for rel in AGENT_PATHS:
        p = os.path.join(ROOT, rel)
        if os.path.isdir(p):
            for dirpath, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in files:
                    ap = os.path.join(dirpath, f)
                    yield os.path.relpath(ap, ROOT).replace("\\", "/"), ap
        elif os.path.isfile(p):
            yield rel, p


def bundle(fmt: str) -> bytes:
    """The agent file set as a tar.gz ('tar') or zip. No top-level dir prefix, so
    the client extracts straight into its install dir (no --strip-components)."""
    buf = io.BytesIO()
    if fmt == "zip":
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for arc, ap in _agent_files():
                z.write(ap, arc)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for arc, ap in _agent_files():
                tar.add(ap, arcname=arc)
    return buf.getvalue()


if __name__ == "__main__":   # ponytail: build both formats from the real tree, offline
    names = {arc for arc, _ in _agent_files()}
    assert "scanners/make_linux_topo.py" in names, names   # the Linux client must ship
    assert not any("__pycache__" in n for n in names), names
    with tarfile.open(fileobj=io.BytesIO(bundle("tar")), mode="r:gz") as t:
        assert "scanners/make_linux_topo.py" in t.getnames()
    with zipfile.ZipFile(io.BytesIO(bundle("zip"))) as z:
        assert "renderers/card.py" in z.namelist()
    print(f"renderers/html/agent_bundle self-check ok ({len(names)} files)")
