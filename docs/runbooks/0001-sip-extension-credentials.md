# Runbook: SIP/WebRTC gateway credentials

**What it is.** The credentials the `hermes-voip` plugin uses to register as a SIP-over-TLS /
WebRTC extension on a voice gateway. The repository is public, so the gateway host, extension,
and password are **sensitive**: they live ONLY in the gitignored repo-root `.env` (keys
`HERMES_SIP_*`) and in 1Password — never in a tracked file, log, or commit.

## Where the values live

- **Runtime / local:** repo-root `.env` (gitignored; `git check-ignore .env` must print
  `.env`). Mounted into the devcontainer via `.devcontainer/docker-compose.yml`
  (`env_file: ../.env`). **Editing `.env` requires a devcontainer restart** to take effect.
- **Canonical store:** 1Password (rule 41). The `.env` is provisioned from 1Password by the
  operator's tooling; mirror any change there so a re-provision does not lose it.
- **Template:** `.env.example` (tracked) lists the `HERMES_SIP_*` keys with placeholder
  values — never real ones.

> **Which password is the SIP secret.** The SIP-extension 1Password item holds **two**
> passwords. `HERMES_SIP_PASSWORD` must be the item's **VoIP-section `Password`** field (the
> SIP-TLS digest secret) — **not** the item's **top-level portal `password`** (the operator
> web-app portal login). A live REGISTER returns `401` if the portal password is used on either the
> SIP-TLS or the Secure-WebSocket edge. The WSS/WebRTC edge authenticates with the **same**
> VoIP-section `Password`, so `HERMES_SIP_WS_PASSWORD` is left unset for this gateway (it falls
> back to `HERMES_SIP_PASSWORD`). This matches
> [`0002-voip-live-validation.md`](0002-voip-live-validation.md) §5 (selects the field by its
> `VoIP.Password` `<section>.<label>` id to disambiguate it from the portal field).

> **Provisioning env-var name aliases (both names work).** The 1Password-provisioned `.env`
> sets `HERMES_SIP_SERVER_HOST` and `HERMES_SIP_TLS_PORT`, while the canonical keys are
> `HERMES_SIP_HOST` and `HERMES_SIP_PORT` (default `5061`). The parser
> (`src/hermes_voip/config.py`) now accepts the provisioner names as **fallbacks**:
> `HERMES_SIP_SERVER_HOST` is used for the host when `HERMES_SIP_HOST` is unset, and
> `HERMES_SIP_TLS_PORT` for the port when `HERMES_SIP_PORT` is unset. The **canonical key
> wins** when both are set, so an explicit `HERMES_SIP_HOST` / `HERMES_SIP_PORT` always
> overrides the alias. Either name therefore loads the host/port — the provisioned `.env`
> registers as-is, and exporting the canonical names (as
> [`0002-voip-live-validation.md`](0002-voip-live-validation.md) §5 does) still works.

## Verify

**1. The keys load** into the running container, without printing any secret:
`printenv | grep -c '^HERMES_SIP_'` (expect ≥ 3 — `HOST`, `EXTENSION`, `PASSWORD`).

**2. The credentials are well-formed** for the plugin (no network; raises `ConfigError` on a
missing/malformed key). Print only non-sensitive facts — never the host, extension, or
password:

```bash
uv run python -c \
  "import os; from hermes_voip.config import load_gateway_config as g; c=g(os.environ); print('SIP config OK: transport', c.transport, 'port', c.port, '| registrations:', len(c.extensions))"
# → SIP config OK: transport tls port 5061 | registrations: 1
```

**3. The real check — a live `REGISTER` against the gateway.** The SIP client now exists, so
drive the adapter directly: it returns `registered=True` with a non-zero `expires` when the TLS
handshake + digest auth (`401` → `200 OK`) succeed. The exact, copy-paste registration-only
script (and how to read a failure — `401` repeating = bad digest, `403` = wrong realm, `404` =
unknown AOR, `423` = interval too brief) is **step 6 "Registration-only check"** of
[`0002-voip-live-validation.md`](0002-voip-live-validation.md). A wrong password shows up there
as a repeating `401`; rotate via the steps below and re-run it.

Under the **full gateway** (`hermes gateway run -vv`), each extension that logs in emits one
`INFO` line on the `hermes_voip.manager` logger — e.g. `SIP registration established (expires
300s)`, where the number is the granted lifetime (one per extension; the line carries only that
lifetime, never the host/extension/password — rule 34). Its presence is the live "registration
succeeded" signal; its absence means the login did not complete (read the SIP response code per
the script above).

## Rotate / restore

1. Change the extension's secret in the gateway's admin UI.
2. Update the 1Password item, then re-provision (or hand-edit) the repo-root `.env`.
3. Restart the devcontainer so the new value loads, then re-run **Verify**.

The `.env` is reconstructible from 1Password. If the extension itself is lost, recreate it in
the gateway and re-issue a secret, then rotate.
