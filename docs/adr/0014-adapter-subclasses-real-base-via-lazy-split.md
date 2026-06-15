# ADR-0014: VoipAdapter subclasses the real Hermes base via a lazy plugin/adapter split

- **Date:** 2026-06-15
- **Status:** Accepted
- **Deciders:** agent session (W10 adapter hardening, post adversarial review)

## Context

ADR-0002 decided `hermes-voip` is a `kind: platform` plugin whose `register(ctx)`
registers a `BasePlatformAdapter` subclass. The first W10 implementation typed the adapter
against the Protocol shim in `hermes_voip.hermes_surface` and accepted `config`/`platform`
as `object`, to keep `import hermes_voip` free of the optional `hermes-agent` runtime and
keep `mypy --strict` (with `disallow_any_explicit`) clean without the extra installed.

An adversarial review against the **real** `hermes-agent==0.16.0` proved that adapter would
not load or work in Hermes — the gate and the contract test passed only because they used
fakes and a `# type: ignore` masked the breakage:

- `gateway/platform_registry.py::PlatformRegistry.create_adapter` aborts (`return None`)
  when `validate_config(config)` is **falsey**; our validator returned `None` on success.
- The gateway relies on `isinstance(adapter, BasePlatformAdapter)` for the
  `handle_message` / `build_source` / `set_message_handler` / send-retry wiring; a duck type
  is not enough (`issubclass(VoipAdapter, BasePlatformAdapter)` was `False`).
- `BasePlatformAdapter.build_source` has no `platform_config` parameter and
  `handle_message` is **async**; the shim-typed calls (hidden behind `# type: ignore`)
  would have raised `TypeError` at runtime, and the `except ImportError` path silently fell
  back to an untested `SimpleNamespace` stub (AGENTS.md rule 6).

The binding constraints: `import hermes_voip` must stay light (the plugin loader imports the
package, and the entry point resolves `getattr(module, "register")`); `mypy --strict` with
`disallow_any_explicit` must stay clean **with zero `# type: ignore`** (rule 17);
`hermes-agent` is an optional extra that ships no `py.typed`; and the adapter must be a
genuine `BasePlatformAdapter` at runtime (rule 26 — validate against the real target).

## Decision

`VoipAdapter` **subclasses the real `gateway.platforms.base.BasePlatformAdapter`** and calls
`super().__init__(config, Platform("voip"))`, inheriting `handle_message` / `build_source` /
`set_message_handler` / send-retry. The optional-runtime/typing tension is resolved by a
**two-module split plus contract-job type-checking**, not escape hatches:

- **`src/hermes_voip/plugin.py`** (light): holds `register(ctx)` and
  `validate_voip_config(config) -> bool` (returns `True` on success; raises `ConfigError`
  on a bad config, which the registry catches as a rejection). It imports **no** hermes-agent
  runtime and no heavy media/ML dependency. `register()`'s factory lazy-imports
  `hermes_voip.adapter.VoipAdapter`. `hermes_voip/__init__.py` re-exports `register` from
  here, so `import hermes_voip` pulls in neither `gateway`/`hermes_cli` nor `adapter.py`
  (verified: empty `sys.modules` intersection).
- **`src/hermes_voip/adapter.py`** (Hermes-dependent): imports the real base at module top.
  Because it is only ever imported lazily (from the factory), the runtime import never
  fires on a bare `import hermes_voip`. It contains **zero** `# type: ignore`.
- **Typing.** `adapter.py` and `tests/test_adapter.py` are `exclude`d from the default
  no-hermes `mypy` gate (regex in `[tool.mypy]`). The `hermes-contract` CI job
  (`uv sync --frozen --extra hermes`) type-checks them explicitly
  (`uv run mypy src/hermes_voip/adapter.py tests/test_adapter.py` — passing files bypasses
  `exclude`). A `[[tool.mypy.overrides]]` sets `follow_untyped_imports = true` for
  `gateway.*` / `hermes_cli.*`, so mypy **analyses the real (un-`py.typed`) gateway source
  for genuine types** rather than erasing them to `Any` (which `ignore_missing_imports`
  would do — a banned shortcut).
- **Verification.** `tests/test_adapter.py` constructs a real `VoipAdapter(PlatformConfig)`,
  asserts `issubclass`/`isinstance` against the real base, routes `_deliver_turn` through
  the real `set_message_handler` + async `handle_message`, and asserts a leak guard.
  `tests/test_hermes_contract.py` drives the real `PlatformRegistry.create_adapter("voip",
  cfg)` end-to-end. Both run with the `hermes` extra in the `hermes-contract` job (skip
  cleanly without it). Leak-safety: any failure after the 200 OK runs `_teardown_call`
  (stops the RTP engine, removes the manager + transport in-dialog routes, marks ended).

## Consequences

- The plugin actually loads and is recognised by Hermes; inbound voice turns reach the
  agent and replies stream back — verified against the real gateway loader path.
- `import hermes_voip` stays cheap; the heavy runtime is paid only when the gateway
  instantiates the platform.
- We maintain a small CI cost: the `hermes-contract` job now also runs a focused `mypy` and
  the adapter test file. A `hermes-agent` surface change (e.g. `build_source`/`handle_message`
  signature, `create_adapter` semantics) now hard-fails that job instead of shipping a
  runtime break.
- `follow_untyped_imports` couples the adapter's type-check to the installed `hermes-agent`
  source; a breaking upstream change surfaces as a mypy error in the contract job, which is
  the intended early-warning.
- `Platform("voip")` resolves only after `register_platform` has added `"voip"` to the
  module-singleton registry (enum `_missing_` hook); since the factory runs after
  registration this holds in production, and tests register a throwaway `"voip"` entry.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Keep typing against the `hermes_surface` Protocol shim; don't subclass the real base | The gateway uses `isinstance(adapter, BasePlatformAdapter)` to wire `handle_message`/send-retry; a duck type is silently inert. Proven `issubclass == False` and a runtime no-op. |
| Subclass the real base in `adapter.py` and `import hermes_voip.adapter` from `__init__` | Makes `import hermes_voip` pull in the optional `hermes-agent` runtime, breaking the default (no-extra) install and the light-import requirement. |
| Keep `# type: ignore[import-not-found]` / `ignore_missing_imports` on the Hermes imports | Banned escape hatches (rule 17): they erase the base to `Any`, which is exactly how the original `build_source`/`handle_message` runtime breakage was hidden. |
| `validate_config` returns `None`/raises only | `create_adapter` treats falsey as failure and never builds the adapter; a valid config silently disables the platform. Must return `True`. |
| Keep the `except ImportError` → `SimpleNamespace` fallback in `_deliver_turn` | An untested production stub (rule 6); the real base is always present at runtime, so the fallback is dead code that masked the wrong `build_source` call. |
