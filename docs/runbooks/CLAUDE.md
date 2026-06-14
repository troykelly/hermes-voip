# docs/runbooks — operational runbooks

One runbook per provisioned resource or operational procedure. Written AS YOU WORK
(AGENTS.md rule 42): the commit that provisions or changes a resource creates or updates its
runbook. Each captures: what it is and why; the exact command/API call used; the
resource id/name/binding; how to verify it; how to rotate/recreate/restore/roll back.
Present tense, executable, kept current — never aspirational (rule 27). Runbooks are the
HOW; ADRs are the WHY.

**Public-repo rule:** runbooks are tracked and public. Never write a real hostname, IP,
extension number, token, or any PII into a runbook — reference the env-var key or 1Password
item instead, and say where the value lives.
