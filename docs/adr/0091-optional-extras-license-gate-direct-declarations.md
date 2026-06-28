# ADR-0091: Licence-gate declared optional runtime packages, not host-runtime transitives

- **Date:** 2026-06-28
- **Status:** Accepted
- **Deciders:** agent session

## Context

The supply-chain workflow already installs `--all-extras` before `pip-audit`, so
advisory scanning sees the full transitive dependency surface reachable through the
`hermes`, `ml`, `media`, and `webrtc` extras (ADR-0024). The licence gate did not
match that scope: it exported only `uv export --frozen --no-dev --no-emit-project
--no-hashes`, which covers the default production dependency set and omits every
extra-only package.

The backlog item for this wave requires licence-checking all **declared optional
runtime extras**, not just default deps. A naive all-transitive licence gate based
on `uv export --frozen --all-extras --no-dev --no-emit-project --no-hashes` and a
single exact-string `pip-licenses --allow-only` list immediately fails on metadata
outside this repo's direct declaration boundary: host/runtime transitives such as
`certifi`, `pathspec`, `tqdm`, and `uvloop` report weak-copyleft or composite
strings, and several permissive packages report non-normalized composite values
such as `Apache-2.0 OR BSD-3-Clause`, `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND
CC0-1.0`, and `Apache licensed, as found in the LICENSE file`. The repo does not
choose those host-runtime transitives directly; it chooses the direct packages in
`[project.optional-dependencies]`.

Rule 35 still requires licence gating on dependency changes, but the gate must stay
stable, actionable, and aligned with the dependency boundary this repo actually
controls.

## Decision

The supply-chain workflow keeps `pip-audit` on the full installed `--all-extras`
environment, but the new optional-extras licence gate derives its package list from
`uv export --frozen --all-extras --no-dev --no-emit-project --no-hashes` and then
filters to entries marked `# via hermes-voip` before invoking `pip-licenses`.

Concretely:

- `.github/workflows/supply-chain.yml` keeps the existing production-only licence
  gate from `uv export --frozen --no-dev --no-emit-project --no-hashes`.
- The same workflow adds a second step, `Licence allowlist (optional runtime
  extras)`, which parses the all-extras export with inline Python and emits only
  the direct package names declared by `hermes-voip` in `[project.optional-
  dependencies]`.
- As of `uv.lock` on 2026-06-28, that direct optional-runtime set is:
  `aioice`, `audioop-lts`, `cryptography`, `hermes-agent`, `numpy`,
  `onnxruntime`, `opuslib`, `pyopenssl`, `sherpa-onnx`, `tokenizers`, and
  `websockets`.
- The optional-extras allowlist remains permissive-only and includes the exact
  current composite metadata strings required by those direct packages:
  `Apache-2.0 OR BSD-3-Clause`,
  `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`, and
  `Apache licensed, as found in the LICENSE file`.
- `docs/runbooks/0003-supply-chain-audit.md` documents the two-surface licence
  check, the `# via hermes-voip` extraction rule, the current direct package set,
  and the re-verification command to run when dependency metadata changes.

## Consequences

- The licence gate now covers every optional runtime package this repo directly
  declares, so an extra-only direct dependency cannot bypass CI simply because the
  default install stays lean.
- `pip-audit` still covers the full transitive extra surface for advisories, which
  is the correct gate for host/runtime packages the repo does not choose directly.
- The optional-extras licence gate is more stable than an all-transitive exact-
  string allowlist, but it now depends on the `uv export` comment format (`# via
  hermes-voip`) remaining present. If uv changes that format, the parsing step must
  be updated in the same workflow.
- The workflow carries three exact composite licence strings because
  `pip-licenses` does not normalize those package metadata values to SPDX atoms.
  When the lockfile changes, operators must re-run the documented mirror command
  and update the allowlist if a direct package's exact licence string changes.
- The policy boundary is explicit: this repo licence-gates the packages it declares
  directly, and advisory-gates the broader transitive runtime closure.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Replace the production gate with `uv export --all-extras --no-dev` over the full transitive closure | It would drop the exact default shipped-surface report the existing gate already provides, weakening rather than extending coverage. |
| Gate every package in the all-extras export with one exact `pip-licenses --allow-only` list | Verified to fail immediately on transitive host/runtime packages and non-normalized composite metadata outside this repo's direct declaration boundary, creating a noisy and brittle gate. |
| Licence-gate only the installed environment and trust `pip-audit` for extras | The backlog item explicitly requires licence-checking declared optional runtime extras; advisory scanning alone does not enforce licence policy. |
| Parse `[project.optional-dependencies]` from `pyproject.toml` directly instead of the lock-derived export | The workflow needs the resolved installed package names, not just requirement specifiers; the export proves the exact locked package set the extras currently resolve to. |
