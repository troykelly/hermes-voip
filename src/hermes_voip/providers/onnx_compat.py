"""Make the pip ``sherpa-onnx`` wheel find the ``onnxruntime`` shared library.

The PyPI ``sherpa-onnx`` wheel's native module is linked against an **unversioned**
``libonnxruntime.so``, but the ``onnxruntime`` wheel ships only the versioned
``libonnxruntime.so.<x.y.z>`` (SONAME ``libonnxruntime.so.1``), so a fresh install
of both fails to load sherpa with ``ImportError: libonnxruntime.so: cannot open
shared object file``. sherpa's RPATH includes its own ``$ORIGIN``
(``sherpa_onnx/lib``), so dropping a ``libonnxruntime.so`` symlink there — pointing
at onnxruntime's versioned library — lets the dynamic loader resolve sherpa's
``NEEDED`` entry with no ``LD_LIBRARY_PATH``. The two versions are pinned together
in the ``ml`` extra so the symbol versions match.

:func:`ensure_sherpa_loadable` is a **no-op** when the optional ``ml`` extra is
not installed (so the package imports fine without it) and when the symlink
already exists (idempotent). Sherpa-consuming providers call it once before
importing ``sherpa_onnx``; the test ``conftest`` calls it at session start.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

__all__ = ["ensure_sherpa_loadable"]

# The unversioned SONAME sherpa-onnx's native module records as NEEDED.
_SONAME = "libonnxruntime.so"


def ensure_sherpa_loadable() -> None:
    """Symlink onnxruntime's library where sherpa-onnx's RPATH resolves it.

    Does nothing when sherpa-onnx or onnxruntime is absent, or when a working
    ``libonnxruntime.so`` (resolving to a real library) is already present. A
    **missing or dangling** link is (re)created pointing at onnxruntime's library;
    a concurrent-create race is accepted only if the link then resolves, else the
    error propagates.
    """
    sherpa_lib = _sherpa_lib_dir()
    if sherpa_lib is None:
        return
    link = sherpa_lib / _SONAME
    if link.exists():
        return  # an existing, resolvable library (ours or sherpa-bundled) works
    target = _onnxruntime_library()
    if target is None:
        return
    if link.is_symlink():
        link.unlink()  # a dangling symlink: remove before recreating
    try:
        link.symlink_to(target)
    except FileExistsError:
        if not link.exists():  # concurrent creation that is itself broken
            raise


def _sherpa_lib_dir() -> Path | None:
    spec = importlib.util.find_spec("sherpa_onnx")
    if spec is None or spec.origin is None:
        return None
    lib = Path(spec.origin).parent / "lib"
    return lib if lib.is_dir() else None


def _onnxruntime_library() -> Path | None:
    spec = importlib.util.find_spec("onnxruntime")
    if spec is None or spec.origin is None:
        return None
    capi = Path(spec.origin).parent / "capi"
    candidates = sorted(capi.glob(f"{_SONAME}.*"))
    return candidates[-1] if candidates else None
