# ADR-0045: Multiple intercoms, each with named DTMF/WebHook openings

- **Date:** 2026-06-18
- **Status:** Accepted (extends ADR-0031 intercom entry actuation)
- **Deciders:** agent session (multi-intercom openings lane) — operator-directed (#38)

## Context

ADR-0031 shipped a **single** intercom with a **single** actuation path: one DTMF
open code OR one HTTP relay, selected by the `HERMES_VOIP_INTERCOM_*` env scheme, and
exposed to the agent as the no-argument `open_entry` tool. The operator (2026-06-17,
issue #38) needs more:

1. **Multiple intercoms.** A deployment has several door phones / entry points, each
   presenting its own caller-ID (front door, side gate, garage), not one.
2. **A named set of openings per intercom.** One intercom can control several entries
   — e.g. the front-door panel opens both the pedestrian *door* and the vehicle
   *gate*. The agent must be told **which** named openings the calling intercom has,
   and `open_entry(name)` must actuate the one it chooses.
3. **DTMF *or* WebHook per opening.** Each opening actuates by EITHER a DTMF code on
   the live call OR an HTTP request (GET/POST/PUT) to an operator-configured endpoint
   with operator-settable headers and body (a smart-lock bridge / home-automation
   webhook). The choice is **per opening**, not per intercom.

The constraint that bounds the answer is ADR-0031's security spine, which must carry
over unchanged: caller-ID is **forgeable** and is never an authorization boundary
(ADR-0020/0021); opening an entry is **physical access**, so the action must be the
least-privilege it can be — scoped, gated, and free of any secret leak. Back-compat is
mandatory: the existing single-intercom path and its tests stay green.

## Decision

1. **A JSON config document**, referenced by `HERMES_VOIP_INTERCOM_CONFIG_FILE` (a
   gitignored path — the document holds caller-IDs and may hold secrets), maps each
   intercom's caller-ID to its named openings:

   ```json
   {
     "intercoms": {
       "1000": {
         "openings": {
           "door": {"type": "dtmf", "dtmf_code": "9"},
           "gate": {"type": "webhook", "method": "POST",
                    "url": "https://lock.example.test/gate",
                    "headers": {"Authorization": "Bearer …"},
                    "body": "open=true"}
         }
       }
     }
   }
   ```

   A new pure, sans-IO module `src/hermes_voip/multi_intercom.py` parses it into a
   frozen `MultiIntercomConfig` of `IntercomEntry` (caller-id + `{name: Opening}`).
   `Opening` is a discriminated value (`OpeningType.DTMF` | `WEBHOOK`). Validation is
   **fail-loud** at load (rule 37): unknown type, a DTMF code that is not real DTMF, a
   webhook without an `https://` URL, a bad method, an intercom with no openings, a
   non-object document, or invalid JSON each raise `ConfigError` at startup — never at
   door-open time. `load_multi_intercom_config({})` (the env key unset) returns an
   empty config and the ADR-0031 single path applies (back-compat).

2. **Secret-suppression on the dataclass.** A webhook `url` / `headers` / `body` and
   the DTMF `dtmf_code` may carry secrets (a bearer token, a door code), so each is
   `repr=False` on `Opening` — they never reach a repr or log line. Only the opening
   `name` and `type` are loggable. Webhook errors (`WebhookError`) carry only the HTTP
   status / failure class, never the url/headers/body. A DTMF-code config error reports
   only the offending character POSITION, never the code (mirroring ADR-0031).

3. **Per-call scoping in the adapter.** At inbound INVITE the adapter matches the
   caller-ID against the config (`MultiIntercomConfig.match`, exact or `*`-prefix) and,
   on a match, binds the `IntercomEntry` to the call (`_call_info[call_id]
   ["intercom_entry"]`). `open_entry(call_id, name)` is then **scoped to that
   intercom's set**: a `name` not in the set raises `ValueError` (a clear JSON tool
   error); `None` defaults to the sole opening (or raises asking the agent to choose
   when there are several). A non-intercom caller (no matched entry) reaches only the
   legacy single path, which is DISABLED by default — so it cannot open anything.

4. **Surface the NAMES, never the secrets.** The matched intercom's opening **names**
   are appended to the ADR-0033 inbound call-context block as a fixed TRUSTED system
   note (operator config, not caller-supplied, so not defanged), telling the agent
   which entries it may open via `open_entry(name=...)`. The codes/urls/tokens are
   never surfaced.

5. **The `open_entry` tool gains an OPTIONAL `name`** (string). The gate is unchanged:
   `open_entry` stays ELEVATED and grant-only (reachable only via the intercom group's
   `allowed_tools={open_entry}` sub-ceiling, ADR-0031 §1), so the multi-intercom
   feature widens *which entry* an already-authorized intercom call can open, never
   *who* can open one.

The webhook request uses the standard library (`urllib`) off the event loop
(`asyncio.to_thread`) — one rare request needs no third-party HTTP dependency
(AGENTS.md rule 40), the same posture as ADR-0031's relay.

## Consequences

- One door phone can drive several distinct entries, and a deployment can host many
  intercoms, each with its own opening set — all from one gitignored JSON file.
- Each opening picks its own mechanism (a DTMF code for an in-band door, a webhook for
  a smart-lock) without a global mode switch.
- The agent sees a concrete menu of named entries for the calling intercom and can only
  open one of them; a spoofed caller-ID reaching the intercom channel can still open
  nothing it is not authorized for (the gate is unchanged) and can never open an entry
  belonging to a different intercom (scoping).
- New surface to maintain: the JSON schema + `multi_intercom.py`. The ADR-0031 single
  path stays as the zero-config default (and the fallback for an unmatched caller).
- A misconfigured webhook (non-2xx / network error) raises `WebhookError`; the tool
  reports the door was NOT opened (never a silent success).

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| Extend the flat `HERMES_VOIP_INTERCOM_*` env scheme with indexed keys | A per-intercom, per-opening matrix of url/headers/body does not fit flat env vars; a JSON document is the natural shape and keeps secrets in one gitignored file. |
| Reuse the caller-groups JSON (add openings to a group) | Conflates trust-tier/channel routing (ADR-0021/0035) with physical-entry actuation; the opening set is keyed by the *device* caller-ID, a different concern, and would bloat the security-critical groups file. |
| Make `open_entry` take a free-form target instead of a named opening | A model-chosen target is an injection/abuse surface; a closed set of operator-named openings, scoped to the calling intercom, is least-privilege. |
| Let a webhook opening use `http://` | A webhook may carry a bearer token; cleartext would leak it. `https://` required at load, same as ADR-0031's relay. |
| Surface the codes/urls to the agent so it "knows" the action | The agent needs only the NAME to choose; the code/url are secrets and stay server-side (repr-suppressed, never rendered). |
