# ADR-0097: GuardSessionState defaults to least-privilege (level 0), not operator (level 3)

- **Date:** 2026-07-02
- **Status:** Accepted
- **Deciders:** agent session (hermes-voip guard-hardening lane — M2 from the 2026-07-02
  injection-guard/caller-policy gap-review)

## Context

`GuardSessionState.__init__` (`src/hermes_voip/providers/policy.py`) has
`privilege_level: int = 3` as its bare constructor default — level 3 is "operator", the
**most** permissive tier (`SAFE` + `ELEVATED` + `IRREVERSIBLE`-with-confirmation all
reachable). The class docstring records why: "``GuardSessionState(call_id)`` — default,
level 3 (assistant), same as ``privileged=True`` was. Preserves the ADR-0009 default where
every session starts as the assistant unless the adapter explicitly lowers the level." A
dedicated test, `tests/test_caller_privilege.py::test_privileged_defaults_true_for_back_compat`,
asserts this is deliberate: "An existing construction site that does not set privileged
keeps today's behaviour (assistant). The clamp is opt-in via `privileged=False`."

A 2026-07-02 adversarial gap-review of the injection guard / caller-policy stack flagged
this as a fail-open default: any *future* construction site that omits
`privilege_level`/`privileged` silently inherits the single most-permissive tier rather than
the least — the opposite of ADR-0021's own stated security-spine principle (§0.3):
"Least privilege for the untrusted. Untrusted and unknown groups run `privilege_level=0`."
ADR-0021's config layer already enforces this exact principle one level up:
`CallerGroupConfig.__post_init__` (`src/hermes_voip/caller_modes.py:316-328`) rejects a
`default_group` whose `privilege_level != 0`, with the recorded rationale: "the default
group is the catch-all for unmatched (unknown, forgeable) callers and must be unprivileged
... A privileged default would grant that privilege to every unmatched caller on a
spoofable identifier — operator/elevated privilege requires an explicit allow-list match,
never the default." `GuardSessionState`'s own bare constructor default violates the
identical principle one layer down, at the runtime object every caller-classification path
ultimately constructs.

**Current blast radius (verified empirically — every construction site was read, not
assumed).** A full-repo scan of every `GuardSessionState(` construction (168 sites across
`src/` + `tests/`) found exactly 5 production (non-test) sites:

| Site | Explicit level passed? |
| --- | --- |
| `tools.py:359` (`_ambient_state`, feeds only `list_registrations` when `self._call is None`) | No — relies on the implicit default |
| `voip_tools.py:1572` (`voip_pre_tool_call`'s fail-safe path) | Yes — `3 if proactive else 0` |
| `adapter.py:2252` (outbound call) | Yes — `_outbound_group.privilege_level` (0) |
| `adapter.py:2862` (outbound call, WebRTC path) | Yes — `_outbound_group.privilege_level` (0) |
| `adapter.py:3412` (inbound call) | Yes — `group.privilege_level` (resolved caller group) |

The one site relying on the implicit default (`tools.py:359`) is benign by design:
`list_registrations`'s own docstring states "When invoked with no active call, the ambient
state is clean+privileged so the operator can still list registrations outside a call" —
there is no caller present at all on that branch (no untrusted party to defend against), so
the ADR-0020 threat model this default protects against does not apply there. **No real
vulnerability exists today.** The concern is exclusively about a *future* call site (a new
tool surface, a caller-classification refactor) that omits the argument and silently
inherits operator-level trust instead of failing closed.

Flipping the default's blast radius was measured, not guessed: temporarily setting the
default to 0 and running the full suite (3047 tests) produced exactly 14 failures, 0
unexpected. Every failure is a test whose intent is to exercise the privileged/clean-session
path via the *implicit* default rather than an explicit `privilege_level=3` /
`privileged=True` — e.g. `test_hold_call_runs_when_not_degraded`,
`test_transfer_blind_runs_when_confirmed_and_clean`, `test_irreversible_requires_confirmation`.
Of the 14: one, `test_privileged_defaults_true_for_back_compat`, directly asserts the current
default as its subject and must be rewritten (not merely have a fixture updated) to assert the
new one; one, `test_list_registrations_allowed_on_privileged_call`, needs no test-side change
at all because it exercises `tools.py:359`'s ambient no-call state, which the production fix
below makes explicit; the remaining twelve need a one-line `privilege_level=3` (or
`privileged=True`) added to their existing `GuardSessionState(...)` construction.

## Decision

Flip `GuardSessionState.__init__`'s bare `privilege_level` default from `3` to `0`
(least-privilege / fail-closed), matching ADR-0021's own security-spine principle. Every
current call site that needs level-3/operator behaviour now states it **explicitly**:

- `tools.py:359` (`_ambient_state`) passes `privilege_level=3` explicitly for the no-call
  `list_registrations` case, with a comment recording that this is a deliberate override,
  not an inherited default. This alone restores `test_list_registrations_allowed_on_privileged_call`
  with no test-side change.
- The remaining 12 affected tests (4 in `test_gate_decision.py`, 2 in
  `test_providers_policy.py`, 2 direct constructions plus the shared `_FakeCall` fixture
  default in `test_tools.py`, covering 4 more) gain an explicit `privilege_level=3` wherever
  their scenario requires the privileged/clean-session path — restoring their exact previous
  tested behaviour, now stated rather than inherited. `test_tools.py`'s `_FakeCall` fixture
  is fixed once, at its constructor, rather than at each of its call sites: the module's own
  docstring already scopes it to the confirmed/degraded axis (the privilege axis is
  `test_caller_privilege.py`'s job), so a privileged-by-default fixture matches its
  documented intent.
- `test_privileged_defaults_true_for_back_compat` (the 14th affected test) is rewritten to
  assert the new contract (bare construction is unprivileged) and renamed to
  `test_unprivileged_is_the_construction_default`; its surrounding comment is updated to
  record this ADR's supersession of the old back-compat rationale. Committed separately, as
  the RED test, before the source change (rule 18).
- The `privileged=True/False` legacy kwarg keeps its existing mapping (`True` → 3,
  `False` → 0) unchanged — only the *bare* (no-argument) default changes.
- The class docstring's "Construction backward-compat" section is rewritten to describe the
  new default and cite this ADR.

No adapter/production call site changes behaviour except `tools.py:359`, which is updated to
preserve its exact current runtime behaviour, now explicitly.

## Consequences

- **Easier:** a future call site that forgets to pass a privilege level now fails closed
  (level 0 — `SAFE` tools only) instead of silently granting operator trust; this closes the
  attractive-nuisance the gap-review flagged, consistent with the ADR-0021 security-spine
  principle and its `caller_modes.py` precedent.
- **Harder:** a new test fixture that wants "a privileged call, don't care about the
  details" must now say so explicitly (`privilege_level=3`) rather than getting it for free
  from a bare constructor call. Judged an acceptable, one-line cost per call site — the same
  trade ADR-0021 already made at the config layer.
- We are now committed to keeping the class docstring and this ADR in sync if the level
  model changes again (e.g. a future intermediate tier).
- This ADR does not touch `voip_tools.py`'s `_proactive_place_call_allowed` opt-in
  relaxation (an orthogonal, already-narrow, already-explicit mechanism) or the
  caller-group resolution paths in `adapter.py` (already explicit, already least-privilege
  for unmatched callers per ADR-0021).

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Leave the default at `3`, document the risk only | Does not defend against a future omission bug — exactly the "a loader-only check is bypassable by any direct construction path" lesson from the ADR-0021 / `caller_modes.py` precedent, applied here to a doc-comment instead of a loader; a comment cannot fail closed. |
| Remove the default entirely (make `privilege_level` a required argument, no fallback) | Strictly stronger in principle (rule 17: prefer types over runtime checks — eliminates the silent-omission vector completely rather than making it safe), but breaks every one of the 81 test constructions that omit an explicit level today (not just the 14 that exercise privilege-gated behaviour) — a disproportionately large mechanical diff for this task with no additional security benefit over defaulting to 0 (both make silent over-privileging impossible; only the required-arg form also forces ~67 unrelated-purpose tests to state a value they do not care about). Left as a possible future follow-up if the class ever drops the legacy `privileged` kwarg. |
| Add a runtime warning (`warnings.warn`) on implicit-default use, keep default at `3` | Still fail-open — a warning is easy to miss in production logs and does not change the actual gate decision; rejected for the same reason as the doc-only alternative. |
| Defer this fix to a separate, larger initiative outside task #38 | Rejected per AGENTS.md rule 6 (never defer a known, cheaply-fixable gap) — the measured fallout (14 tests, 1 production call site) is small enough to fix in the same lane; the task's own framing anticipated exactly this ("needs care/ADR before touching", not "defer indefinitely"). |
