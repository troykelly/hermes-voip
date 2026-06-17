# Runbook: VoIP RTP-inactivity watchdog (`HERMES_VOIP_RTP_TIMEOUT_SECS`)

**What it is.** The RTP-inactivity watchdog ends a call whose inbound media has gone silent,
so a dropped media/network path no longer hangs the call forever. When no inbound RTP datagram
arrives within the configured window, the media engine ends the call as a `MEDIA_TIMEOUT`
(ADR-0026), which the adapter signals to the Hermes session as a `/stop` (a failure end). The
deadline re-arms on every inbound datagram, so a live call with continuous media is never
affected.

The WHY lives in **ADR-0026** (call-termination → Hermes session signal). This runbook is the
operational HOW for the operator knob.

> **Public repo.** No secrets here — this is a plain integer config value.

## The knob

| Item | Value |
| --- | --- |
| Env var | `HERMES_VOIP_RTP_TIMEOUT_SECS` |
| Type | integer **seconds**, range `[1, 300]` |
| Default | `20` |
| Read by | `hermes_voip.config.load_media_config` → `MediaConfig.media_timeout_secs` |
| Applied at | `RtpMediaTransport(media_timeout_secs=…)` for every inbound and outbound call |
| On expiry | call ends as `CallEndReason.MEDIA_TIMEOUT` → `/stop` to the Hermes session |

Validation (fail-fast at startup, `MediaConfig.__post_init__`):

- a value outside `[1, 300]` raises `ConfigError` — **not** silently clamped;
- `0` is **rejected** (the operator knob does not expose disabling the safety watchdog — a
  disabled watchdog reintroduces the infinite-hang bug). The engine itself treats
  `media_timeout_secs=0` as "disabled", but the config layer never produces `0`.

## How to set it

Set the env var in the same place the rest of the `HERMES_VOIP_*` config lives (the gitignored
`.env` the Hermes runtime loads, or the process environment for `hermes gateway run`). Example
(gitignored `.env`, value only — no secret):

```
HERMES_VOIP_RTP_TIMEOUT_SECS=30
```

Then redeploy/restart the gateway so the plugin re-reads its config (the value is read at
`connect()` time per call config load; a running call keeps the window it started with).

## How to verify

1. **Config parse (offline, deterministic):**

   ```
   uv run python -c "from hermes_voip.config import load_media_config; \
     print(load_media_config({'HERMES_VOIP_RTP_TIMEOUT_SECS':'30'}).media_timeout_secs)"
   ```

   Prints `30`. An out-of-range value (`0` or `301`) raises `ConfigError` — confirm:

   ```
   uv run python -c "from hermes_voip.config import load_media_config; \
     load_media_config({'HERMES_VOIP_RTP_TIMEOUT_SECS':'301'})"
   ```

   exits non-zero with `media_timeout_secs must be in [1, 300], got 301`.

2. **Behaviour (covered by the test suite):**
   `uv run pytest tests/test_media_watchdog.py` — proves a silent inbound stream ends the
   call (no infinite hang) and sets `engine.media_timed_out`, that arriving datagrams re-arm
   the deadline (no false kill of a live call), and that `connection_lost`/`error_received`
   end the call too. `uv run pytest tests/test_config.py -k rtp_timeout` proves the bounds.

3. **Live:** on a real call, kill the media path (e.g. block the RTP port mid-call) and
   confirm the operator log shows, within the window:
   `rtp: no inbound media for <N>.0s — ending call (MEDIA_TIMEOUT)` followed by
   `call <id> ended (MEDIA_TIMEOUT, failure=True); signalling Hermes session: '/stop'`.

## Tuning guidance

- **Lower** (e.g. 10 s) cleans up a dropped call faster, but risks ending a call during a long
  legitimate silence or a brief network hiccup. The default 20 s rides out a short hiccup and
  a held call's silence while still cleaning up a real drop promptly.
- **Higher** (up to 300 s) tolerates longer silences but lets a genuinely wedged call persist
  that much longer before it is reclaimed. 300 s is the hard cap on how long a wedged call can
  occupy a registration.

## Rollback

Unset `HERMES_VOIP_RTP_TIMEOUT_SECS` (or set it to `20`) and redeploy to return to the default
window. There is no way to disable the watchdog via config (by design); the only "off" is in
the engine constructor (`media_timeout_secs=0`), which the adapter never passes.
