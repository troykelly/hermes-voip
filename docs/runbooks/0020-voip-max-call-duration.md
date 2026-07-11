# Runbook: VoIP per-call max-duration cap (`HERMES_VOIP_MAX_CALL_DURATION_SECS`)

**What it is.** A per-call ceiling on the ACTIVE (answered) phase of a call. A call
still up after this many seconds is force-torn-down (a graceful in-dialog BYE + media
stop), so a caller streaming continuous RTP cannot pin an admission slot (ADR-0059
`max_calls`) and its STT/LLM/TTS pipeline **forever** — a resource-exhaustion DoS that
neither the RTP-inactivity watchdog (silent media only) nor the RFC 4028 session timer
(dead dialog only) catches.

The WHY lives in **ADR-0113**. This runbook is the operational HOW for the operator
knob.

> **Public repo.** No secrets here — this is a plain numeric config value. Never put a
> real hostname / IP / extension / password into this file.

## The knob

| Item        | Value                                                              |
| ----------- | ----------------------------------------------------------------- |
| Env var     | `HERMES_VOIP_MAX_CALL_DURATION_SECS`                              |
| Type        | float (seconds)                                                   |
| Default     | `14400.0` (4 hours)                                               |
| `0`         | **disables** the cap (calls may run unbounded — opt-out)         |
| Read by     | `GatewayConfig.max_call_duration_secs` (`load_gateway_config`)   |
| Applied at  | `_run_call_loop` arms a per-call watchdog when the call goes ACTIVE |
| On expiry   | graceful `CallSession.hang_up()` (BYE + media stop); the Hermes session is hard-stopped (`/stop`), end reason `MAX_CALL_DURATION` |

Validation (fail-fast at startup — a bad value stops the gateway, it does not run with
a silently-wrong cap):

- Must be a **non-negative finite** number. Negative, `NaN`, `inf`, or a non-numeric
  string is rejected with a `ConfigError`.
- `0` is **accepted** and disables the cap. (This is the opposite of
  `HERMES_VOIP_RTP_TIMEOUT_SECS`, where `0` is rejected — that is a *safety* watchdog
  that must never be disabled via its knob; the duration cap is a policy ceiling.)

## How to set it

Set the env var wherever the gateway's environment is configured (systemd unit,
container env, `.env`). Example — cap active calls at 30 minutes:

```sh
HERMES_VOIP_MAX_CALL_DURATION_SECS=1800
```

Disable the cap entirely (a deployment that genuinely runs unbounded calls):

```sh
HERMES_VOIP_MAX_CALL_DURATION_SECS=0
```

Leaving it unset keeps the 4-hour default.

## How to verify

1. **Config parse (offline).** In a Python shell with the plugin importable:

   ```python
   from hermes_voip.config import load_gateway_config
   cfg = load_gateway_config({
       "HERMES_SIP_HOST": "pbx.example.test",
       "HERMES_SIP_EXTENSION": "1000",
       "HERMES_SIP_PASSWORD": "x",
       "HERMES_VOIP_MAX_CALL_DURATION_SECS": "1800",
   })
   assert cfg.max_call_duration_secs == 1800.0
   ```

   A negative or non-finite value raises `ConfigError` at this call.

2. **Behaviour (pytest).** The watchdog + config knob are covered by:

   ```sh
   uv run pytest tests/test_config.py -k max_call_duration
   uv run pytest tests/test_adapter_session_timers.py -k "max_duration or classify_end_reason_max"
   ```

3. **Live.** On a running gateway, place / receive a call and let it exceed the cap
   (set a small value like `60` for the test). At the cap the gateway sends a BYE and
   the origin session is stopped; the structured log line
   `event=max_call_duration_exceeded` records the `call_id` and `cap_secs`.

## Tuning guidance

- **Lower** (e.g. 900–1800 s) for a public / cost-sensitive line where no legitimate
  conversation should run long, to reclaim slots faster under abuse.
- **Higher** (or `0`) only when a use case genuinely needs long-lived calls (a
  monitored line, an always-on intercom). Prefer a finite ceiling over `0` — an
  uncapped active call reintroduces the exact DoS the cap exists to prevent.

## Rollback

Set `HERMES_VOIP_MAX_CALL_DURATION_SECS=0` to disable the cap without redeploying
code, or unset it to return to the 4-hour default. No state is persisted; the change
takes effect for calls that start after the gateway re-reads its environment.
