# Platform & toolchain standards

Binding technical constraints for this repository. `AGENTS.md` carries the invariants and
`docs/standards/engineering.md` the process contract; this file is the toolchain detail
behind both. Every entry is verified against a primary source; cite it and date the
verification.

- **Language/runtime:** Python (>= 3.13, pinned in `.python-version`). Full type annotations,
  checked with `mypy --strict` (`disallow_any_explicit`); no escape hatches — no `Any`, no
  unjustified `# type: ignore`, no type-laundering `cast`. Prefer types over runtime checks:
  validated construction, discriminated unions, exhaustive matches with no catch-all.
- **Package/project manager:** `uv` only — never bare `pip`/`poetry`/`conda`. Dependencies in
  `pyproject.toml`, fully locked in the committed `uv.lock`. Frozen install: `uv sync
  --frozen`. Dev tools are exact-pinned: builds are deterministic, with no floating ranges.
- **Lint & format:** `ruff` (format + a strong curated lint rule set — see `pyproject.toml`).
- **Efficiency is a standing constraint, not an afterthought.** This is a real-time media
  path: audio arrives every 20 ms and the event loop must never be the thing that stalls it.
  A design touching the media or signalling path gets an explicit budget (CPU per packet,
  allocations per frame, queue bounds) reviewed alongside correctness, and a hot-path change
  re-measures and reports the number rather than asserting "fixed". Two consequences that
  are easy to get wrong: `asyncio.to_thread` does **not** free the event loop from CPU-bound
  work (the GIL — a long C-level regex holds it even off-thread), so an input-driven CPU
  stall is fixed by *bounding the input*, not by offloading it; and every queue on the media
  path is explicitly bounded, because an unbounded one is a memory-exhaustion path.
- **Tests:** `pytest`. **Build backend:** `hatchling`, `src/` layout
  (`src/hermes_voip`). **Local gate:** `pre-commit` (`repo: local` hooks calling `uv run`).
  The default `uv run pytest` (no extras synced) runs the base suite in ~30-40s (3167
  tests, verified 2026-07-03). Syncing the `hermes`, `media`, and `webrtc` extras
  (`uv sync --frozen --extra hermes --extra webrtc --extra media`, as the `hermes-contract`
  CI job does) additionally collects the e2e/adapter socket+crypto tests; that full run is
  legitimately slow (~15-20 min, I/O-bound at low CPU) — this is expected, not a hang.
  `[tool.pytest.ini_options]` sets `faulthandler_timeout = 300`: pytest's built-in
  faulthandler support dumps every thread's stack to stderr, naming the specific hung
  test, once any single test has run longer than 300s — a hang safety-net with no new
  dependency, generous enough that it never fires on a legitimately slow test.
  To tell a genuine hang from a merely-slow run without waiting out the full bound: find
  the pytest pid (`ps -eo pid,etimes,pcpu,args | grep pytest`), then `cat
  /proc/<pid>/wchan` — a truly-hung run sits blocked in a syscall such as `do_epoll_wait`
  at ~1% CPU with its `[ N%]` progress bar frozen, while a slow-but-healthy run keeps
  advancing its `[ N%]` bar and shows non-trivial `pcpu`.
- **Hosting / deployment:** NONE assumed. This repo is a Python package (a Hermes plugin)
  loaded by the Hermes runtime; it is not a deployed service and pins no cloud/platform. The
  questions of where/how the plugin runs, the SIP-over-TLS/WebRTC media transport, and the
  STT/TTS conversational provider are deliberately deferred to future ADRs — never
  defaulted from devcontainer tooling.
- **Secret manager:** 1Password. The `op` CLI is baked into the devcontainer image and an
  `OP_SERVICE_ACCOUNT_TOKEN` is provided. Gateway/SIP credentials live ONLY in the gitignored
  `.env` (keys `HERMES_SIP_*`) and 1Password — never in a tracked file (the repo is public).
- **CI (GitHub Actions):** `gate` (ruff format check / ruff lint / database-exposure scan /
  mypy / pytest) and `wheel-smoke` run on every change. A `changes` job triages the diff and
  the four heavy native jobs — `hermes-contract`, `providers`, `media`, `webrtc` — run only
  when it touches something outside `docs/`, `.claude/` and the root markdown files; it
  fails safe, so anything it cannot prove documentation-only runs them all. `supply-chain`
  (`pip-audit` + licence allowlists) runs on dependency changes and daily; `gitleaks`
  (pinned, checksum-verified binary) runs on everything. All tooling is free/OSS.

Verified against primary sources (devcontainer config, installed tool versions): 2026-06-14.
