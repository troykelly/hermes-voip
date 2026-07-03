# ADR-0099: File-backed source for the outbound dial allowlist — `HERMES_VOIP_OUTBOUND_ALLOW_FILE` (union-merged, PII off the shell)

- **Date:** 2026-07-03
- **Status:** Accepted (extends ADR-0029 §2; partially reverses ADR-0029's "Alternatives considered" rejection of file-path allowlists; adopts the PII-safe file-path pattern of ADR-0020/ADR-0021)
- **Deciders:** agent session (outbound-allow-file lane) — operator-directed (Wave-9 gap-review, backlog `[medium] security`)

## Context

ADR-0029 shipped the agent-triggered outbound dial gate. Its allowlist —
`HERMES_VOIP_OUTBOUND_ALLOW` — is read by
`hermes_voip.outbound_allow.load_outbound_allowlist(extra)` from a **single inline env
var**: a comma-separated list of dial targets. ADR-0029 deliberately chose the inline
form and its "Alternatives considered" table **rejected** a file-path allowlist, reasoning
that "dial targets (extensions/SIP URIs) are short allowlist entries, not a PII corpus."

That reasoning holds for a handful of internal extensions, but the operator's Wave-9
gap-review flagged a real security gap for the other case: a **real PSTN dial target is
potentially PII**, and an environment variable is **shell-visible** — it appears in
`printenv`, `/proc/<pid>/environ`, `ps eww`, container-inspect output, and any
orchestration/CI dashboard that echoes the process environment. Forcing a corpus of real
callable numbers / SIP URIs to live in `HERMES_VOIP_OUTBOUND_ALLOW` therefore puts PII on
the shell, on a **public** repo's deployment surface. The values are already kept out of
git (they live only in the gitignored `.env`), but the env var itself is a weaker PII
posture than a file with restrictive filesystem permissions that never enters the process
environment.

The repo already has the safe pattern for exactly this: ADR-0020/ADR-0021 keep caller-mode
/ caller-group number lists in gitignored **files** referenced by a `*_FILE` env var
(`HERMES_VOIP_CALLER_{ALLOW,DENY,GREY}_FILE`, `HERMES_VOIP_CALLER_GROUPS_FILE`), never
inline — because caller numbers are PII. ADR-0045's `HERMES_VOIP_INTERCOM_CONFIG_FILE` is
the same path-from-env precedent. This ADR brings the outbound allowlist into line with
that pattern **without removing** the inline form (which stays correct and convenient for
the minimal-extensions case ADR-0029 optimised for).

Constraints that bound the answer: rule 37 (errors propagate, never swallowed — a
misconfigured security-list must fail loud, never silently grant or silently deny); the
**public-repo** invariant (no real number/host/URI in any tracked file — tests/docs/ADR use
fakes only); the adapter's single `load_outbound_allowlist(extra)` call site at
`connect()` must stay unchanged; fully-typed, `mypy --strict`-clean, no new dependency.

## Decision

Add an **optional, additional** source for the outbound dial allowlist: a new env var
**`HERMES_VOIP_OUTBOUND_ALLOW_FILE`** naming a **path** to a gitignored file of dial-target
entries. `load_outbound_allowlist` reads the path from `extra`, parses the file, and
**unions** its entries with the inline `HERMES_VOIP_OUTBOUND_ALLOW` entries. The path is
sourced from `extra`, so the adapter's single `load_outbound_allowlist(extra)` call site is
unchanged.

### 1. File format — same entry grammar as the inline var, comma **or** newline separated

The file is a **plain list**, not JSON: entries are separated by commas **and/or
newlines** (a file's natural separator), each entry is trimmed, and blank entries are
dropped. Every entry uses the **identical grammar and validation** as the inline var
(ADR-0029 §2/§2a): an exact extension, an `x`/`X` digit-mask (`10xx` → `1000`..`1099`), a
literal-`*` star/service code (`*67` is exact), or a SIP URI (exact). The same
`_entry_to_regex` / exact-vs-pattern split applies verbatim, so an operator moves entries
from the env var to the file **1:1** with no transformation.

The plain-list form (not the caller-modes JSON `{"patterns": […]}`) is chosen so the file
content is byte-identical to what the operator would otherwise put after
`HERMES_VOIP_OUTBOUND_ALLOW=` — the migration is a copy, and there is no second grammar to
learn. **No comment syntax is supported**: `#` is a legal dial character (feature codes
like `#82`, and it appears in masks), so a `#`-comment convention would be ambiguous with a
real dial target and is deliberately omitted. Every non-blank entry is a dial target.

### 2. Precedence / merge rule — **UNION** (file entries ∪ inline entries)

The effective allowlist is the **set union** of the inline entries and the file entries,
both parsed with the identical per-entry logic (exact entries dedupe via `frozenset`;
pattern entries concatenate). Either source may be empty/absent. When **both** are
empty/absent the union is empty ⇒ deny-all — the ADR-0029 inert default is preserved.

**Why union is the safest rule** (the choice this ADR makes):

1. **Additive and operator-authored only.** Every entry in the effective allowlist was
   explicitly written by the operator in *one* of the two sources. Union can neither
   conjure an entry the operator never typed nor silently **drop** an entry a source lists.
   There is no "shadow allow" and no shadow deny.
2. **Fail-closed preserved.** Empty ∪ empty = empty = deny-all; the feature stays inert
   until the operator opts a target in, in *either* place.
3. **Robust to partial misconfiguration.** A *file-overrides-inline* rule would let a
   present-but-**empty** file silently **wipe** an inline allowlist the operator believes
   is active — a surprising, silent loss of an intended-allowed set (and, symmetrically, a
   surprising silent grant if the file replaced a smaller inline list). Union has no such
   footgun: an empty file contributes nothing and the inline var still applies. The only
   effect of adding a file is *exactly* the entries the operator put in it.
4. **Clean, incremental migration.** An operator moving numbers off the shell can relocate
   them to the file incrementally; entries momentarily present in both dedupe. The target
   end-state (all entries in the file, `HERMES_VOIP_OUTBOUND_ALLOW` unset) is the intended
   PII posture and needs no flag-day.

The one honestly-recorded residual (see Consequences): under union a **stale** entry left
in the env var after "moving" it to the file stays allowed. This is not a fail-open —
it is still an operator-authored entry, still gated by the ADR-0029 level-3 +
non-degraded privilege clamp — only a migration-hygiene note. The runbook's
rotation/rollback section instructs the operator to unset `HERMES_VOIP_OUTBOUND_ALLOW`
once fully migrated. This residual is strictly less dangerous than override's silent-wipe.

### 3. Error contract — fail loud and explicit (rule 37), matching the caller-modes loader

`load_outbound_allowlist` gains the exact error contract the caller-modes / caller-groups
file loaders already use:

- `HERMES_VOIP_OUTBOUND_ALLOW_FILE` **unset or blank** ⇒ no file is read; behaviour is
  identical to today (inline var only). Running with only the inline var, or with neither,
  stays valid.
- Path **set but the file does not exist** ⇒ raise `ConfigError` ("configured but does not
  exist (unset the variable to run without a file)"). A typo'd security-list path must fail
  loudly, never silently behave as an empty list.
- Path set but the file **cannot be read** (`OSError`) ⇒ `raise ConfigError(...) from exc`.

`ConfigError` (`hermes_voip.config.ConfigError`, a `ValueError`) is the canonical config
error the sibling loaders raise; it is imported the same way `caller_modes` imports it.
`load_outbound_allowlist(extra)` is called at `VoipAdapter.connect()` with no swallowing
try/except (verified), so a `ConfigError` propagates and the plugin **fails to connect** on
a misconfigured allowlist file — fail-**closed** and **explicit**, never silently allow-all
and never silently allow-none. This is byte-for-byte the posture of the adjacent
`load_caller_groups` / `load_intercom_config` calls in the same `connect()` block.

### 4. PII posture — updated reasoning

ADR-0029 and the `outbound_allow` module docstring asserted dial targets "live ONLY in the
gitignored `.env`." That is updated: a dial target may live in **either** the gitignored
`.env` (inline `HERMES_VOIP_OUTBOUND_ALLOW`, fine for a few extensions) **or** a gitignored
file referenced by `HERMES_VOIP_OUTBOUND_ALLOW_FILE` (preferred when the list contains real
PSTN numbers or is large). **Neither is ever a tracked file.** The file form is the
stronger PII posture because the values stay off the process environment and out of
shell/process introspection (`printenv`, `/proc/<pid>/environ`, `ps`, container inspect).

## Consequences

- **Easier:** an operator can keep a corpus of real callable numbers / SIP URIs in a
  gitignored, restrictive-permission file instead of a shell-visible env var — the PII
  posture the gap-review asked for. The inline form still works unchanged for the common
  minimal-extensions case, and the two compose additively.
- **We now maintain** one more config surface (`HERMES_VOIP_OUTBOUND_ALLOW_FILE`) and one
  more fail-loud path in `load_outbound_allowlist` (previously it never raised). This is
  consistent with every sibling loader in `connect()`, which already raise `ConfigError` on
  misconfiguration.
- **Migration hygiene (the recorded residual):** because the merge is a union, an entry
  left in `HERMES_VOIP_OUTBOUND_ALLOW` after being copied to the file remains allowed. The
  runbook's rollback/rotation section tells operators to unset the env var once fully
  migrated. Documented, not hidden; it is an operator-authored entry, never a silent grant.
- **No new dependency**, no change to the adapter, no change to the dial chokepoint or the
  privilege gate. The `place_call` IRREVERSIBLE level-3 + non-degraded clamp (ADR-0029) is
  untouched — the file only changes *where the operator writes the same entries*.
- **Public-repo safety:** the ADR, tests, runbook, and module docs use fakes only (ext
  `1000`/`1001`, `sip:1000@pbx.example.test`). A real value lives only in the operator's
  gitignored `.env` or gitignored allow file.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Keep inline-only (ADR-0029's original choice) | Forces a real-number corpus onto the shell-visible process environment on a public-repo deployment — the exact PII gap the gap-review flagged. Kept as an *option*, not the only one. |
| **File-overrides-inline** (file wins when set) | A present-but-empty or partially-filled file would silently **wipe / shrink** an inline allowlist the operator believes is active — a silent deny (or, replacing a smaller list, a silent grant). Union has no silent-change footgun: the file can only *add* explicitly-listed entries. |
| Replace the inline var entirely (file-only, like caller-modes) | Breaks every existing deployment that lists a few extensions inline, for no benefit in that case; ADR-0029 rightly optimised the minimal case for the inline form. Additive is a superset, not a swap. |
| JSON file format (`{"patterns": […]}`, like caller-modes) | Introduces a second grammar and a non-copy migration. A plain comma/newline list is byte-identical to the inline value, so relocating entries is a copy and the same `_entry_to_regex` validation applies verbatim. |
| Silently treat a missing/unreadable file as an empty list | Violates rule 37: a typo'd security-list path would silently disable the gate (deny-all with no signal) or, worse under a different merge, be mistaken for intent. A configured path that can't be read must fail loud (`ConfigError`), exactly as the caller-modes loader does. |
| `#`-prefixed comment lines in the file | `#` is a legal dial character (feature codes `#82`, mask positions); a comment convention is ambiguous with a real target. Omitted so every non-blank line is unambiguously a dial entry. |
