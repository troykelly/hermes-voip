# Runbook: devcontainer resource limits

**What it is.** The cgroup memory cap on the devcontainer `app` service, set via `mem_limit`
in `.devcontainer/docker-compose.yml`. CPU is unconstrained: there is no cgroup CPU quota
configured; the container sees all cores the host provides. This runbook covers how to
verify, change, and roll back the memory ceiling.

---

## Current value and rationale

`mem_limit: 64g`

The autonomous orchestration loop (`/orchestrate`, ADR-0072) fans out parallel
implementation lanes, each of which runs the full `uv run pytest` gate. With the `ml`
extra pulled in, a single gate load (onnxruntime + sherpa-onnx models) is approximately
1.5–2.5 GiB resident. Memory is the sole bottleneck on fan-out width because CPU is
unlimited.

The prior `8g` cap constrained concurrency to roughly 3–4 heavy lanes running
simultaneously. The fleet width formula is `min(16, cores−2)` heavy lanes; at full width
that peaks at roughly 40 GiB model resident plus overhead. `64g` comfortably supports the
full fleet width with margin. The host has far more RAM available than the container is
allotted; this limit exists only to prevent a runaway lane from starving the host.

---

## How to verify (after a rebuild)

Run these commands inside the running container:

```bash
# Memory ceiling — must equal 68719476736 (= 64 × 1024³)
cat /sys/fs/cgroup/memory.max

# CPU — must read "max <period>" (unconstrained)
cat /sys/fs/cgroup/cpu.max
```

Expected output:

```
68719476736
max 100000
```

Do **not** use `free -h` to check the container ceiling — it reports the **host's** total
RAM, not the cgroup limit (see Gotchas below).

---

## How to change / roll back

1. Edit `mem_limit` in `.devcontainer/docker-compose.yml` (one line, e.g. `mem_limit: 8g`
   to roll back to the previous value, or any other value).
2. Rebuild/recreate the container — the running container keeps the **old** value until
   recreated:
   - VS Code: **Dev Containers: Rebuild Container** (Command Palette).
   - CLI: `docker compose -f .devcontainer/docker-compose.yml up -d --force-recreate`
3. Re-verify with `cat /sys/fs/cgroup/memory.max` inside the new container.

---

## Gotchas

- **`free -h` shows host RAM, not the container limit.** Always read
  `/sys/fs/cgroup/memory.max` for the real ceiling. `free` is useful for gauging
  available headroom on the host but says nothing about the cgroup cap.

- **The running container is NOT updated until recreated.** Editing the compose file and
  running `docker compose up -d` without `--force-recreate` may leave the old limit in
  place. Always verify with `/sys/fs/cgroup/memory.max` after a rebuild.

- **OOM-kill signature.** When the limit is breached the kernel kills the offending process:
  exit code is **137** (SIGKILL), not 144. The kernel log shows `"Killed process <pid> …"`.
  Inside the container `dmesg | grep -i kill` or the Docker host's `journalctl -k` will
  surface it.

---

## Related

- [ADR-0072: autonomous orchestration loop](../adr/0072-autonomous-orchestration-loop.md) — why the loop needs wide fan-out and why memory is the binding constraint.
- [Runbook 0016: operating the orchestration loop](0016-orchestration-loop.md) — start/stop/observe/recover the loop; includes the full prerequisite gate.
