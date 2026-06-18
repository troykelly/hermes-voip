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

### 4. Inbound DTMF receive (the caller presses keys) — ADR-0010/0034

Inbound DTMF is decoded and surfaced by the call controller (`CallLoop`), not directly
exposed: (1) while an irreversible tool has ARMED a confirmation, a digit resolves it
directly (the spoof-resistant channel that gates transfer — ADR-0009); (2) otherwise a
digit group (terminated by `#` or the inter-digit gap) is delivered to the agent as a
tagged `[DTMF] 1234` turn. Digits never pass through STT / the LLM as a fake transcript.
All **three** mechanisms feed the SAME `CallLoop.feed_dtmf` router, so surfacing is
uniform; `_wire_dtmf_receive` resolves the per-call backend and binds a per-call
`ArmedConfirmation` for every active one:

- **RFC 4733** (`engine.on_dtmf`) — when the gateway negotiated `telephone-event`.
- **SIP INFO** (`CallSession.on_dtmf`) — inbound `application/dtmf-relay` /
  `application/dtmf` `INFO` requests (forced with `HERMES_SIP_DTMF_MODE=sip_info`).
- **In-band Goertzel** (`engine.on_dtmf`) — tone detection on the decoded G.711 audio,
  the last resort when no `telephone-event` was negotiated (G.711 only).

No extra config is required for the default behaviour. The env keys (all optional) drive
the backend selection (no inert key — every value picks a real backend):

```sh
# Backend selector (send AND receive). ALL FOUR are implemented (ADR-0035):
#   auto     (default) negotiate RFC 4733, else in-band on a G.711 call
#   rfc4733  force telephone-event (UNAVAILABLE if the peer offered none)
#   sip_info force in-dialog INFO (always available)
#   inband   force Goertzel/tone-gen (G.711 only; UNAVAILABLE on Opus/G.722)
HERMES_SIP_DTMF_MODE=auto
# Gap (ms) after which a buffered menu group with no '#' terminator is delivered.
HERMES_SIP_DTMF_INTERDIGIT_MS=2000     # unset => built-in default (2000); must be > 0
# Whether the in-band last resort is PERMITTED under `auto` when the peer offered no
# telephone-event. Default true. false forbids it (an `auto` call then resolves to no
# DTMF rather than the less spoof-resistant in-band backend). No effect when the mode is
# forced to a specific backend.
HERMES_SIP_DTMF_INBAND_ENABLED=true
```

> **Spoof-resistance note.** In-band is the WEAKEST channel (a caller can play tones), so
> it is the last resort and a deployment that relies on DTMF for the ADR-0009 confirmation
> should prefer RFC 4733 / SIP INFO. The confirmation resolver still binds on the in-band
> backend so the human-in-the-loop gate works when only in-band is available.

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
- **DTMF mode loads (config check).** All four modes load now; only an unknown mode
  fails at startup:
  ```sh
  uv run python -c "from hermes_voip.config import load_media_config as L; \
    print([L({'HERMES_SIP_DTMF_MODE':m}).dtmf_mode for m in ('auto','rfc4733','sip_info','inband')])"
  # -> ['auto', 'rfc4733', 'sip_info', 'inband']
  ```
- **Backend resolution (unit check).** The per-call backend follows the codec +
  telephone-event negotiation (ADR-0035):
  ```sh
  uv run python -c "from hermes_voip.config import load_media_config as L; \
    from hermes_voip.dtmf_config import resolve_dtmf_receive_mode as R; \
    c=L({'HERMES_SIP_DTMF_MODE':'auto'}); \
    print(R(c, telephone_event_payload_type=None, codec='PCMU'))"   # -> DtmfReceiveMode.INBAND
  ```
- **DTMF receive live (pending operator redeploy + a real call).** The answer log shows
  `inbound DTMF receive active (<backend>)` (`rfc4733` / `sip_info` / `inband`); pressing
  keys logs `dtmf rx: digit '<d>'` (the digit is operational, not a secret) and — for a
  menu group — `dtmf: delivering menu group '[DTMF] 1234'`. If no backend can run (no
  telephone-event AND a non-G.711 codec, or in-band forbidden) the log shows a single
  WARNING `inbound DTMF receive ... UNAVAILABLE` rather than silence. **In-band detection
  reliability on a real lossy G.711 path is to be re-measured live (ADR-0035, rules
  23/26)** — the unit tests prove tone-in/tone-out + speech rejection on clean PCM.

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
  ADR-0010 (DTMF) + ADR-0035 (the SIP INFO + in-band Goertzel mechanisms, send AND
  receive — all three ADR-0010 mechanisms are now shipped); ADR-0009 (the tool gate).
- `docs/runbooks/0010-voip-caller-modes.md` (the caller-groups file + JSON schema).
