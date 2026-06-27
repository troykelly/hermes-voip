# ADR-0082 — Relax over-pinned extras to compatible ranges for Hermes/plugin coexistence

**Status:** Accepted  
**Date:** 2026-06-27  
**Issues:** #239 (onnxruntime platform marker), #240 (websockets range), #241 (cryptography range)

## Context

`hermes-voip` is a **library plugin** co-installed alongside the Hermes host runtime
and potentially other plugins in a single Python environment.  Exact `==` pins in
`[project.optional-dependencies]` behave like hard constraints on the shared
environment: if the host runtime or another plugin needs a different version of the
same package, the install is unsatisfiable.

Three extras had over-tight pins:

| Extra | Package | Old constraint | Problem |
|-------|---------|---------------|---------|
| `webrtc` | `websockets` | `==16.0` | Hermes's transitive `websockets 15.0.1` (via `uvicorn[standard]`) conflicts |
| `media` | `cryptography` | `==48.0.1` | Blocks co-install with any host that already has a different `cryptography` |
| `ml` | `onnxruntime` | `==1.24.4` | No macOS wheel for py3.13; breaks `[ml]` installs on macOS |

## Decision

Plugin extras use **compatible ranges** with explicit security floors and
upper-bound gates, not exact `==` pins.  Three concrete changes (ADR policy
instantiated):

### #240 — websockets: `>=15.0,<17`

Our code uses only `websockets.asyncio.client.connect` with `subprotocols`,
`max_size`, and `ping_interval` kwargs, plus the `ClientConnection` type under
`TYPE_CHECKING` in `transport/ws_connection.py` and `stt/deepgram.py`.  This
API surface is stable since websockets 12.0 and has not changed across 15–16.
The `>=15.0` lower bound covers Hermes's transitive `websockets 15.0.1` (pulled
in by `uvicorn[standard]`).  The `<17` upper bound gates the next major.

### #241 — cryptography: `>=46.0.7,<49`

**The `>=46.0.7` lower bound is a SECURITY FLOOR — do not lower it.**

- `CVE-2026-34073` — fixed in cryptography 46.0.6.
- `CVE-2026-39892` — fixed in cryptography 46.0.7.

The `<49` upper bound is required for co-install compatibility with
`pyopenssl==26.2.0`, which declares `cryptography>=46,<49` in its own
`Requires-Dist`; `pyopenssl==26.3.0` requires `cryptography>=49`, which would
push outside our window.  Our only cryptography use is hazmat AES-CTR
(`Cipher` / `algorithms.AES` / `modes.CTR`) in `media/srtp.py` — stable
across the entire 46–48 range, verified by the RFC-3711 KAT suite.

### #239 — onnxruntime: `==1.24.4; sys_platform != 'darwin'`

`onnxruntime 1.24.4` ships no macOS wheel for Python 3.13.  `sherpa-onnx`
self-bundles its own onnxruntime on macOS, so adding the
`sys_platform != 'darwin'` environment marker lets macOS installs of the `[ml]`
extra succeed without a conflicting wheel fetch.  The exact pin `==1.24.4` is
retained because `sherpa-onnx 1.13.2` links against that specific ABI symbol
version and the `hermes_voip.providers.onnx_compat` shim symlinks the library
onto its RPATH.

## Consequences

- `uv lock` still resolves to the same locked versions (16.0 / 48.0.1 / 1.24.4)
  in our repo because we have no Hermes/DingTalk transitive constraints — the
  value is the relaxed **constraint** for downstream co-installs.
- The RFC-3711 SRTP/SRTCP KAT suite (`tests/test_media_srtp.py`,
  `tests/test_media_srtcp.py`) continues to gate that the AES-CTR path is
  correct regardless of the exact cryptography version in range.
- A static test (`tests/test_extra_dep_ranges.py`) enforces all three invariants
  (range present, security floor met, platform marker present) on every commit.
- Future bumps to the upper bounds (`<17` → `<18`, `<49` → `<50`) require a
  deliberate commit with a KAT re-run to confirm the new major's ABI is
  compatible.  The security floor for cryptography must only move **up**, never
  down.
