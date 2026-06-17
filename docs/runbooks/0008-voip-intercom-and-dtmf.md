# Runbook: intercom entry actuation + in-call DTMF (ADR-0031)

**What it is.** Two related capabilities of the `hermes-voip` plugin:

1. **In-call DTMF send** — the `send_dtmf(digits)` agent tool transmits RFC 4733
   telephone-event tones on the live call (IVR navigation, keypad entry). It is
   **ELEVATED**: available only on a privileged (level ≥ 2, non-degraded) call.
2. **Intercom caller mode** — a caller group that answers a door/gate intercom,
   screens the visitor, and opens the entry (the `open_entry` tool) for a legitimate
   expected visitor, via **one of two** operator-chosen actuation paths: a DTMF open
   code on the call, or an external HTTP relay / smart-lock.

The WHY is ADR-0031; this runbook is the operational HOW.

## Security model (read before configuring)

- **Caller-ID is forgeable** and is NOT authentication (ADR-0020/0021). Opening a door
  is **physical access**, so the intercom group is configured so a spoofed caller-ID
  reaching it can do **nothing but** open the entry.
- The enforcement is **by construction**, not persona wording:
  - the intercom group runs at **`privilege_level` 2** with an **`allowed_tools`
    sub-ceiling** of exactly `["open_entry"]` (ADR-0031 §1). The tool gate
    (`gate_voip_tool`) removes every tool not in that set **before** the level check, so
    `hold_call` / `list_registrations` / `send_dtmf` / `place_call` / everything else is
    blocked for an intercom call;
  - `open_entry` is **ELEVATED**, so a level-0 caller (the fail-safe default for an
    unknown context) cannot open the door.
- The relay path uses a **bearer token** (a secret): the URL must be **https** (so the
  token never travels cleartext), the token lives in **1Password** and is never committed
  or logged (`repr=False` on the config; the relay error messages carry only the HTTP
  status / failure class, never the token or URL).
- DTMF strings can carry secrets (PINs, card numbers): the plugin **never logs the
  digits** — only the digit count.

## Configuration

All keys are env vars the plugin reads (`src/hermes_voip/intercom.py`,
`src/hermes_voip/config.py`). PII / secrets live in the gitignored `.env` / 1Password
only — never a tracked file (the repo is PUBLIC).

### 1. Wire the intercom caller group

Add an `intercom` group to the caller-groups JSON (see
`docs/runbooks/0010-voip-caller-modes.md` for the file location + `HERMES_VOIP_CALLER_GROUPS_FILE`).
The `persona` is `intercom`; the `allowed_tools` array is the load-bearing
least-privilege control:

```json
{
  "groups": [
    {
      "name": "intercom",
      "privilege_level": 2,
      "persona": "intercom",
      "declined_at_sip": false,
      "allowed_tools": ["open_entry"]
    },
    {
      "name": "receptionist",
      "privilege_level": 0,
      "persona": "receptionist",
      "declined_at_sip": false
    }
  ],
  "lists": {
    "intercom": ["1000"]
  },
  "default_group": "receptionist",
  "match_order": ["intercom", "receptionist"]
}
```

Put the **door phone's caller-ID** (a digit-bearing pattern — a privileged group may
not use a digitless/match-all pattern, ADR-0021) on the `intercom` list. Everyone else
falls to the receptionist.

> If you prefer the agent to send the raw open code itself rather than a single
> `open_entry` verb, set `"allowed_tools": ["send_dtmf"]` instead and skip the DTMF
> open-mode below — but `open_entry` is the recommended, narrower surface.

### 2. Choose the actuation path — `HERMES_VOIP_INTERCOM_OPEN_MODE`

Default (unset) is **`disabled`**: `open_entry` raises a clear error (a door is never
opened, and never silently fails to open). Pick one:

**DTMF mode** — the door phone opens on an in-band code:

```sh
HERMES_VOIP_INTERCOM_OPEN_MODE=dtmf
HERMES_VOIP_INTERCOM_DTMF=9          # the open code; validated as DTMF (0-9 * # A-D)
```

`open_entry` then calls `send_dtmf` with that code on the live call. Requires the
gateway to have negotiated `telephone-event` for the call (it raises a clear error
otherwise — no silent failure).

**Relay mode** — an external HTTP relay / smart-lock / webhook:

```sh
HERMES_VOIP_INTERCOM_OPEN_MODE=relay
HERMES_VOIP_INTERCOM_RELAY_URL=https://lock.example.test/api/open   # https only
HERMES_VOIP_INTERCOM_RELAY_TOKEN=<from 1Password>                   # never commit
HERMES_VOIP_INTERCOM_RELAY_METHOD=POST     # POST (default) | GET | PUT
HERMES_VOIP_INTERCOM_RELAY_TIMEOUT_S=5     # request timeout seconds (> 0)
```

`open_entry` then POSTs `{"action":"open"}` with `Authorization: Bearer <token>` to the
URL (off the event loop). A non-2xx response or a network error raises
`IntercomRelayError` and the tool reports a clear failure (the door was NOT opened).

### 3. In-call DTMF send (no intercom needed)

`send_dtmf(digits)` is registered for **every** privileged call. No extra config — it
just needs the gateway to negotiate `telephone-event`. Use it for IVR menus / keypad
entry on outbound or trusted calls.

### 4. Inbound DTMF receive (the caller presses keys) — ADR-0010

Inbound DTMF is decoded from RFC 4733 telephone-event RTP and surfaced by the call
controller (`CallLoop`), not the engine: (1) while an irreversible tool has ARMED a
confirmation, a digit resolves it directly (the spoof-resistant channel that gates
transfer — ADR-0009); (2) otherwise a digit group (terminated by `#` or the inter-digit
gap) is delivered to the agent as a tagged `[DTMF] 1234` turn. Digits never pass through
STT / the LLM as a fake transcript. Wired for **every** call (inbound and outbound) whose
gateway negotiated `telephone-event` — the adapter wires `engine.on_dtmf` to the loop and
binds a per-call `ArmedConfirmation` in `_wire_dtmf_receive`.

No extra config is required for the default behaviour. The three env keys (all optional)
now drive real behaviour (no inert key):

```sh
# Receive mechanism. ONLY auto / rfc4733 are implemented; sip_info / inband are REJECTED
# at config load (a loud ConfigError) — never a key that silently does nothing.
HERMES_SIP_DTMF_MODE=auto              # auto (default) | rfc4733
# Gap (ms) after which a buffered menu group with no '#' terminator is delivered.
HERMES_SIP_DTMF_INTERDIGIT_MS=2000     # unset => built-in default (2000); must be > 0
# Whether the (unbuilt) in-band last-resort detector is permitted. Default true. On a
# call where the gateway negotiated no telephone-event this flag only changes whether the
# missing-DTMF state is logged loud (true => UNAVAILABLE) or quiet (false => DISABLED).
HERMES_SIP_DTMF_INBAND_ENABLED=true
```

## Where the secret lives + rotation

- **Relay token (`HERMES_VOIP_INTERCOM_RELAY_TOKEN`).** Canonical store: 1Password
  (AGENTS.md rule 41). Materialise into the gitignored `.env` (or the process env) at
  deploy time with `op`. **Rotate** = mint a new token at the relay/lock provider, update
  the 1Password item, update every deployment's env, redeploy the plugin, then revoke the
  old token at the provider. Never echo/log/commit the value.
- **DTMF open code (`HERMES_VOIP_INTERCOM_DTMF`).** Site-specific, sensitive: gitignored
  `.env` / 1Password only.

## Verify

- **Config parses (fail-loud check).** A bad config fails at startup, not at door-open
  time. Quick local check (no live call):
  ```sh
  uv run python -c "import os; from hermes_voip.intercom import load_intercom_config as L; \
    print(L({'HERMES_VOIP_INTERCOM_OPEN_MODE':'dtmf','HERMES_VOIP_INTERCOM_DTMF':'9'}).open_mode)"
  # -> IntercomOpenMode.DTMF   (a typo'd code / non-https relay URL raises ConfigError)
  ```
- **Tools registered.** On plugin load the agent has `send_dtmf` and `open_entry` (gated).
  `register_voip_tools` installs the `pre_tool_call` gate first and skips an ELEVATED tool
  if the gate is absent (fail-closed), so a registered `open_entry` is always gated.
- **Live (pending operator redeploy + an intercom group config + a real door/relay).**
  Call the extension that maps to the intercom group; confirm the agent uses the intercom
  persona (screens the visitor) and that `open_entry` actuates ONLY for a legitimate
  visitor. DTMF mode: confirm the door opens on the code (the log shows
  `intercom open_entry (dtmf) for call <id>` + `agent send_dtmf tool: sending N DTMF
  digit(s)` — never the digits). Relay mode: confirm the log shows
  `intercom open_entry (relay)` + `intercom relay: entry opened (HTTP 2xx)`.
- **Least-privilege.** From an intercom call, a request to "transfer me" / "list the
  extensions" must be refused — those tools are removed by the `allowed_tools`
  sub-ceiling. (Covered by `tests/test_voip_tools.py::test_open_entry_scoped_by_allowed_tools_blocks_other_tools`.)
- **DTMF receive config (fail-loud check).** An unsupported mode fails at startup, not
  at key-press time:
  ```sh
  uv run python -c "from hermes_voip.config import load_media_config as L; \
    L({'HERMES_SIP_DTMF_MODE':'sip_info'})"
  # -> ConfigError: dtmf_mode 'sip_info' is not supported for DTMF receive ...
  uv run python -c "from hermes_voip.config import load_media_config as L; \
    print(L({'HERMES_SIP_DTMF_MODE':'rfc4733'}).dtmf_mode)"   # -> rfc4733
  ```
- **DTMF receive live (pending operator redeploy + a real call).** On a call whose
  gateway negotiated `telephone-event`, the answer log shows `inbound DTMF receive active
  (RFC 4733, PT <n>)`; pressing keys logs `dtmf rx: digit '<d>'` (the digit is
  operational, not a secret) and — for a menu group — `dtmf: delivering menu group
  '[DTMF] 1234'`. If the gateway negotiated NO telephone-event the log shows a single
  WARNING `inbound DTMF receive ... UNAVAILABLE` (with `HERMES_SIP_DTMF_INBAND_ENABLED`
  true) rather than silence.

## Roll back / disable

- **Disable actuation:** unset `HERMES_VOIP_INTERCOM_OPEN_MODE` (or set `disabled`) and
  redeploy — `open_entry` then refuses to open (a clear error), but the intercom group
  still screens callers.
- **Remove the intercom mode entirely:** drop the `intercom` group from the caller-groups
  JSON (callers fall to the receptionist) and redeploy.
- **Revoke a leaked relay token:** revoke it at the provider, mint + deploy a replacement
  (see rotation above).

## Related

- ADR-0031 (this feature's WHY); ADR-0021 (caller groups + the `allowed_tools` clause);
  ADR-0010 (DTMF; the RFC 4733 send AND receive paths + the armed-confirmation resolver
  are now shipped — SIP INFO / in-band remain deferred); ADR-0009 (the tool gate).
- `docs/runbooks/0010-voip-caller-modes.md` (the caller-groups file + JSON schema).
