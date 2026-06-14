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

> The operator supplied the test gateway's password URL-encoded; `.env` stores the **decoded**
> value as `HERMES_SIP_PASSWORD`, keeping the encoded original in a comment as a fallback.

## Verify

Confirm the keys load into the running container without printing any secret:
`printenv | grep -c '^HERMES_SIP_'` (expect non-zero). The real check — a successful SIP
`REGISTER` against the gateway — lands when the SIP/WebRTC client exists; record the exact
command here then (rule 42).

## Rotate / restore

1. Change the extension's secret in the gateway's admin UI.
2. Update the 1Password item, then re-provision (or hand-edit) the repo-root `.env`.
3. Restart the devcontainer so the new value loads, then re-run **Verify**.

The `.env` is reconstructible from 1Password. If the extension itself is lost, recreate it in
the gateway and re-issue a secret, then rotate.
