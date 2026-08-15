# docs/runbooks — operational runbooks

One runbook per provisioned resource/procedure, written AS YOU WORK (the commit that
provisions or changes a resource updates its runbook): what it is and why, the exact
command/API call, the resource id/binding, how to verify and roll back. Present tense,
executable, kept current. Runbooks are the HOW; ADRs are the WHY.

**Public-repo rule:** never write a real hostname, IP, extension, token, or PII into a
runbook — reference the env-var key or 1Password item and say where the value lives.
