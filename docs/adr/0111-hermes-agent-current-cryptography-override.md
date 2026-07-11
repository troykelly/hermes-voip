# ADR-0111: Track the current hermes-agent (0.18.2); override cryptography to the patched 48.0.x

- **Date:** 2026-07-11
- **Status:** Accepted
- **Deciders:** operator (directed testing/shipping against the CURRENT hermes-agent, not an
  ancient pin); agent session (resolved the transitive cryptography advisory and validated
  the typed contract + real runtime)
- **Relates to:** ADR-0062 (the pyjwt CVE override — the same `[tool.uv]`
  `override-dependencies` mechanism and rationale). ADR-0013 (SDES-SRTP uses pyca/cryptography).

## Context

The `hermes` optional extra pinned `hermes-agent==0.17.0`, which carries **CVE-2026-10222**
(fixed in 0.18.0). main's daily supply-chain scan (`.github/workflows/supply-chain.yml`) had
been RED on it since 2026-07-10. The operator directed that the plugin test and ship against
the **current** hermes-agent, not an ancient pin.

The current release is **0.18.2**. Bumping to it clears CVE-2026-10222 — but surfaces a
**second** advisory. hermes-agent's 0.18 line added a hard pin `cryptography==46.0.7` (0.17
declared no cryptography dependency at all), and cryptography **46.0.7** is vulnerable to
**GHSA-537c-gmf6-5ccf** (the statically-linked OpenSSL bundled in cryptography wheels before
48.0.1). Left alone the resolver honours hermes-agent's exact pin and installs the vulnerable
46.0.7, so `pip-audit` stays red — only the failing package changes.

## Decision

1. Pin `hermes = ["hermes-agent==0.18.2"]` (the current release).
2. Add a `[tool.uv]` `override-dependencies` entry `cryptography>=48.0.1,<49`, forcing the
   patched cryptography (48.0.1 ships a fixed bundled OpenSSL) over hermes-agent's exact
   `==46.0.7` request — the same override mechanism ADR-0062 uses for pyjwt. The `<49` upper
   bound keeps `pyopenssl==26.2.0` (the `webrtc` extra's DTLS stack) satisfiable (it requires
   cryptography `<49`) and stays inside the `media` extra's own `>=46.0.7,<49` range.

## Validation (rule 23/26 — proven, not assumed)

- `uv lock` resolves cleanly (123 packages); cryptography → 48.0.1, hermes-agent → 0.18.2.
- `uv run pip-audit` with `--all-extras` installed: **No known vulnerabilities found** — both
  CVE-2026-10222 and GHSA-537c-gmf6-5ccf are cleared.
- The real hermes runtime imports on the overridden cryptography (`import gateway` succeeds on
  cryptography 48.0.1) — the override does not break hermes-agent at import.
- The typed contract shim (`src/hermes_voip/hermes_surface.py`) is unchanged: `uv run mypy` on
  the adapter + contract + e2e files is clean against the real 0.18.2 runtime — **no surface
  drift** between 0.17.0 and 0.18.2.
- The `hermes-contract` suite (contract + adapter + e2e, `HERMES_CONTRACT_REQUIRED=1`) passes
  against real hermes-agent 0.18.2 on cryptography 48.0.1.

## Consequences

- main's supply-chain / `audit` gate goes green; the plugin is validated against the **current**
  hermes-agent, per the operator directive — not a stale runtime.
- The override forces cryptography 48.0.x regardless of what any dependency requests. If a
  future dependency needs cryptography `<48` or `>=49`, this single, visible override line (with
  this rationale) must be revisited.
- **Upkeep (the operator's standing directive — keep hermes-agent current):** when a newer
  hermes-agent releases, bump the pin and re-run `pip-audit` + the `hermes-contract` validation;
  if a future hermes-agent moves to a non-vulnerable cryptography pin, this override can be
  dropped. See `docs/runbooks/0003-supply-chain-audit.md`.

## Alternatives considered

- **Stay on hermes-agent 0.17.0** — rejected: it carries CVE-2026-10222 and is an ancient pin
  the operator explicitly ruled out.
- **Bump to 0.18.0 (the CVE floor) instead of 0.18.2** — rejected: same cryptography problem,
  and it is not the current release (operator: track current, not a minimal floor).
- **Accept cryptography 46.0.7 with a documented risk acceptance** — rejected: the patched
  48.0.1 is inside every relevant compatible range and validated to work, so there is no reason
  to ship a known-vulnerable bundled OpenSSL.
- **Drop the `hermes` extra's transitive cryptography by not installing it** — not applicable:
  the advisory is gated across `--all-extras`, and the extra is a real runtime surface.

## References

- ADR-0062 (`0062-*.md`) — the pyjwt CVE override; precedent for the `[tool.uv]` override
- CVE-2026-10222 (hermes-agent `< 0.18.0`); GHSA-537c-gmf6-5ccf (cryptography `< 48.0.1`)
- `.github/workflows/supply-chain.yml` — the `pip-audit` advisory gate
- `docs/runbooks/0003-supply-chain-audit.md` — the operational HOW (updated with this bump)
