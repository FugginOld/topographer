"""Path-injection barrier for the guarded stores (Store, Widget store).

The single check every user-supplied id passes before it becomes a filename:
strip to a basename, allowlist the charset, then confirm the resolved file sits
directly inside the store dir (blocks '..', separators, absolute paths, symlink
tricks). Deep and narrow — stdlib only, one self-check; the stores are shallow
bytes-in/out wrappers on top. See CONTEXT.md → Store / Widget.
"""
from __future__ import annotations

import os
import re


def guarded_path(store_dir: str, name: str, label: str = "id", suffix: str = ".json") -> str:
    """Resolve <name><suffix> inside store_dir, refusing any id that escapes it."""
    base = os.path.basename(name)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", base):
        raise ValueError(f"bad {label}")
    fp = os.path.realpath(os.path.join(store_dir, base + suffix))
    if os.path.dirname(fp) != os.path.realpath(store_dir):
        raise ValueError(f"bad {label}")
    return fp


if __name__ == "__main__":   # ponytail: the barrier is the whole reason to test in isolation
    import tempfile
    D = tempfile.gettempdir()
    for safe in ("network", "../etc/passwd", "a/b", "..", "/abs/x"):   # basename-reduced -> inside D
        assert os.path.dirname(guarded_path(D, safe)) == os.path.realpath(D), safe
    for reject in ("", "a b", "a;b", "a$b"):                           # invalid charset -> rejected
        try:
            guarded_path(D, reject)
            raise AssertionError(f"accepted {reject!r}")
        except ValueError:
            pass
    assert guarded_path(D, "x", suffix=".txt").endswith("x.txt")
    print("guarded_path self-check ok")
