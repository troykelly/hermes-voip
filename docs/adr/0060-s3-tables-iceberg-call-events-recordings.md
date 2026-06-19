# ADR-0060 — S3 Tables (Iceberg) for call events and recordings

**Status:** Proposed — Deferred (nice-to-have; requires operator approval per AGENTS rule 40)
**Date:** 2026-06-19
**Scope:** observability / data export (future)
**Relates to:** ADR-0010 (signalling/DTMF), ADR-0036 (DTMF mechanisms), ADR-0053 (SRTP media), ADR-0055 (signalling robustness), and runbook `docs/runbooks/0014-voip-slo-metrics.md` (SLO signal catalogue). The call-event **sink interface** this design relies on is proposed within this ADR (see §2); it would be specified in its own ADR if adopted.

---

## 1. Context

Runbook `0014-voip-slo-metrics.md` defines the observability signals we want from the VoIP plane —
registration uptime, inbound-INVITE / setup-success / setup-rejected counters, first-audio latency,
per-turn STT/LLM/TTS latency, RTP packet-loss / jitter, and the operator-relevant "did we read a
provider error aloud" counter (`voip.errors.spoken_to_caller`). Today most of those signals are marked
**NOT-YET-INSTRUMENTED**, and there is **no durable store** for them: nothing in the repository writes
Iceberg, S3, or any recording (verified by repo search — the closest artifacts are runbooks 0013/0014,
which describe signals, not storage). Call lifecycle facts are emitted only as transient log lines
across `adapter.py`, `manager.py`, and `media/engine.py`.

Two distinct durable-data needs motivate this ADR:

1. **Queryable call telemetry.** A durable, append-mostly fact table of one row per call (SIP outcome,
   latency budget, media/security parameters, conversational turn metrics) so SLOs and post-incident
   analysis can be answered with SQL over history instead of scraping logs.
2. **Recordings + their metadata.** When call recording is enabled, the audio itself needs durable
   object storage, and the *metadata about each recording* (consent basis, retention class, legal hold,
   KMS key reference, integrity digest, pointer to the blob) needs to be queryable and governed.

This ADR proposes a storage backend and a non-invasive integration shape for both. It is a **design
record, not an adoption**: AGENTS rule 40 forbids introducing any hosting platform, cloud, or external
SaaS — or any vendor/transport/provider lock-in — without explicit operator approval recorded in an
ADR. The plugin is currently a no-hosting Python package; this proposal MUST NOT be implemented until
the operator approves the cloud dependency and the ~dollar/month cost, at which point this ADR is
flipped to Accepted (see §8).

---

## 2. Decision (PROPOSED — not adopted)

Propose, as the durable analytics/recordings backend, **Amazon S3 Tables** — AWS's fully-managed
Apache Iceberg service (GA since 2024-12-03) built on the "table bucket" bucket type — hosting two
Iceberg tables in a `hermes_voip` namespace:

- **`call_events`** — one row per call (telemetry + nested per-turn / per-transfer / per-DTMF structs).
- **`recordings_metadata`** — one row per stored audio object, **pointing at** audio blobs that live in
  a **separate, plain S3 (general-purpose) bucket**. Iceberg is a tabular format and must never hold
  binary media; the table carries only a URI/key and governance columns.

Integration shape (the load-bearing part for rule-40 compliance):

- **Core stays dependency-free and cloud-agnostic.** The core package defines a generic, vendor-neutral
  **call-event sink interface** (an async `Protocol`, proposed here — see §2; to be specified in its
  own ADR if adopted) through which the adapter routes lifecycle events off the hot path. Core contains **no `boto3`, no AWS SDK, no Iceberg/PyIceberg
  import** — none of it. The sink registry is **empty / no-op by default** (default **OFF**).
- **The S3-Tables writer is an OPTIONAL extra**, not a core dependency. It lives behind an opt-in
  dependency group (e.g. `optional-aws-sinks` in `pyproject.toml`) and is enabled only by explicit
  configuration (e.g. `HERMES_VOIP_CALL_EVENT_SINKS=s3-tables`). Without the extra installed and
  enabled, the plugin behaves exactly as it does today.
- **No Spark / no cluster.** Writes use **PyIceberg + PyArrow** in a single Python process against the
  S3 Tables **Iceberg REST Catalog** endpoint with SigV4 from the ambient AWS credential chain
  (`load_catalog(type="rest", warehouse="arn:aws:s3tables:<aws-region>:<aws-account-id>:bucket/<call-events-table-bucket>", uri="https://s3tables.<aws-region>.amazonaws.com/iceberg", **{"rest.sigv4-enabled": "true", "rest.signing-name": "s3tables", "rest.signing-region": "<aws-region>"})`).
  Rows are appended in **batches** (e.g. periodic flush), never one tiny file per call, to keep object
  counts and request volume low. (CTAS / `CREATE TABLE AS SELECT` is unsupported on the native
  `s3tables` endpoint; bulk patterns use `append`/`overwrite`. The Glue Iceberg REST endpoint is the
  AWS-recommended fallback if a write-path edge case appears, or where unified Lake-Formation
  governance is wanted.)

> All identifiers in this ADR are **placeholders** (this repo is PUBLIC). Real bucket names, account
> ids, ARNs, regions, KMS key ids — and the SIP host/extension and any PII — live ONLY in the
> gitignored `.env` / 1Password / per-deployment config and are read at runtime from env vars,
> mirroring the existing `HERMES_SIP_*` pattern (e.g. `HERMES_VOIP_ICEBERG_TABLE_BUCKET`,
> `HERMES_VOIP_RECORDINGS_BUCKET`, `HERMES_VOIP_RECORDINGS_KMS_KEY_ARN`).

---

## 3. Data model (proposed)

### 3.1 `call_events` (one row per call)

Iceberg primitive types in parentheses; `REQUIRED` marks non-null. Field-IDs are tracked by Iceberg so
all later schema changes are metadata-only.

**Identity / lifecycle**

- `call_id` (string, REQUIRED) — app-minted **UUIDv7** surrogate stored as `string` (time-ordered →
  clusters with the day partition; does not expose the SIP `Call-ID`). Deliberately **not** the native
  Iceberg `uuid` type (uneven engine support; can't usefully transform-partition on it).
- `schema_version` (int, REQUIRED) — row-format version, gated by readers for forward-compat.
- `direction` (string, REQUIRED) — `inbound` | `outbound`.
- `hermes_channel` (string) — routed channel that handled it (`voip-unknown` / `voip-known` /
  `voip-operator` / `voip-intercom`; an existing concept in this repo).
- `started_at` (timestamptz, REQUIRED, µs UTC) — INVITE sent/received.
- `answered_at` (timestamptz) — NULL until answered.
- `ended_at` (timestamptz) — NULL until BYE/teardown.

**Latency** (all int milliseconds, nullable): `ring_latency_ms`, `answer_latency_ms`, `setup_latency_ms`,
`call_duration_ms`.

**SIP outcome**: `sip_final_status` (int, e.g. 200/486/603); `sip_reason_phrase` (string);
`disconnect_cause` (string: `caller_bye`|`callee_bye`|`timeout`|`rejected`|`transfer`|`error`|`cancel`);
`disconnect_initiator` (string: `local`|`remote`|`system`).

**Media / security**: `audio_codec` (string: `PCMU`|`PCMA`|`G722`|`opus`); `audio_ptime_ms` (int);
`srtp_crypto_suite` (string, NULL if cleartext RTP/AVP); `srtp_keying` (string: `sdes`|`dtls`|NULL);
`signalling_transport` (string: `tls`|`wss`|`tcp`|`udp`); `media_transport`
(string: `rtp_savp`|`rtp_avp`|`webrtc_dtls`).

**Quality summary** (call-level rollups): `packet_loss_pct` decimal(5,2); `jitter_ms_avg` decimal(7,2);
`jitter_ms_max` decimal(7,2); `rtcp_available` boolean.

**Conversational**: `turn_count` (int); `dtmf_digits` (string, ordered captured digits — **PII-lite**,
redactable by retention class); `error_spoken_to_caller` (boolean, REQUIRED, default false — the
operator-facing "did we read an LLM/provider error aloud" flag).

**Audit**: `plugin_version` (string); `ingested_at` (timestamptz, REQUIRED — write-time watermark,
distinct from `started_at`).

**Nested columns** keep a call as **one row** (so right-to-erasure is a single-row delete and per-call
analytics are join-free):

- `turns` list&lt;struct&gt;: `{ turn_index int; turn_started_at timestamptz; stt_latency_ms int;
  llm_latency_ms int; tts_latency_ms int; time_to_first_audio_ms int; barge_in boolean;
  stt_provider string; tts_provider string; llm_model string }`.
- `transfers` list&lt;struct&gt;: `{ transfer_index int; transfer_type string('blind'|'attended');
  transfer_at timestamptz; refer_to_present boolean; transfer_result
  string('completed'|'failed'|'cancelled'); notify_final_status int }`.
- `dtmf_events` list&lt;struct&gt;: `{ digit string; received_at timestamptz; mechanism
  string('rfc4733'|'sip_info'|'inband') }` — matches the three DTMF mechanisms shipped in ADR-0036.

**Partitioning:** hidden partitioning, `PARTITIONED BY (day(started_at))`. Queries filter the raw
`started_at` column and Iceberg prunes files automatically (no user-maintained partition column, no
accidental full scans). Partition spec can later evolve `day → hour` with **zero** SQL change because
partition columns are never exposed. Do **not** partition on high-cardinality `call_id` (small-files
problem) nor low-cardinality `direction`. Optional sort order on `(started_at)` for scan locality;
S3 Tables background compaction keeps file sizes healthy.

### 3.2 `recordings_metadata` (one row per stored audio object — blob NOT in Iceberg)

Audio bytes live in a **separate plain S3 bucket** (placeholder
`s3://<recordings-bucket>/yyyy/mm/dd/<recording_id>.<ext>`), referenced here:

- `recording_id` (string, REQUIRED, UUIDv7); `call_id` (string, REQUIRED — logical FK to
  `call_events.call_id`; Iceberg enforces no FKs); `schema_version` (int, REQUIRED).
- `s3_object_uri` (string, REQUIRED — placeholder only); `s3_version_id` (string, nullable — object
  version pin if bucket versioning is on).
- `audio_format` (string: `wav`|`ogg`|`mka`|`raw`); `audio_codec`
  (string: `pcm_s16le`|`opus`|`g711_ulaw`|`g711_alaw`); `sample_rate_hz` (int: 8000/16000/48000);
  `channels` (int: 1 mono | 2 dual-leg); `duration_ms` (int); `byte_size` (**long**, not int —
  recordings can exceed 2 GB over batches); `sha256_hex` (string, nullable integrity digest).
- `retention_class` (string, REQUIRED:
  `transient_30d`|`standard_1y`|`legal_7y`|`consent_revoked` — drives the TTL job); `consent_basis`
  (string, REQUIRED — GDPR Art.6: `consent`|`legitimate_interest`|`contract`|`legal_obligation`);
  `consent_captured` (boolean, REQUIRED — TRUE is a hard precondition for the row/object to exist);
  `consent_captured_at` (timestamptz, nullable); `all_party_consent` (boolean, REQUIRED — design
  **fails closed**: missing/false ⇒ no recording).
- `legal_hold` (boolean, REQUIRED, default false — blocks TTL and erasure until released);
  `kms_key_arn` (string, REQUIRED — *reference* only, placeholder
  `arn:aws:kms:<aws-region>:<aws-account-id>:key/<kms-key-id>`, never key material);
  `encryption_context` (string, nullable — KMS encryption-context bound to `call_id`).
- `created_at` (timestamptz, REQUIRED); `expires_at` (timestamptz, nullable — precomputed TTL deadline
  the deletion job scans); `erased_at` (timestamptz, nullable — set when blob+row were erased, proving
  erasure for audit).

**Partitioning:** `PARTITIONED BY (day(created_at))`, same hidden-partitioning rationale.

### 3.3 Schema evolution (both tables)

Rely on Iceberg field-IDs so every change is **metadata-only**: **add** optional columns freely (old
files read NULL); for an added REQUIRED column set both initial- and write-default. **Widen-only**
promotions (`int→long`, `float→double`, increase decimal precision) — and only these; `int↔long` hash
equality preserves partition values. **Rename** is metadata-only but breaks consumers on the old name —
prefer add-new + dual-write + backfill + drop-old. **Drop** is a soft delete; never reuse a dropped
name with new meaning. **Forbidden/breaking:** narrowing, tightening nullable→required on existing
data, dropping a column a consumer depends on. Bump the row-level `schema_version` on every shape
change; record each change in a follow-up ADR (rule 30).

---

## 4. Pricing & efficiency

S3 Tables is a managed Iceberg layer at a ~15% storage premium over plain S3
($0.0265 vs $0.023 /GB-mo, US-East/West list, June 2026), plus a per-object **monitoring** fee
($0.025 / 1,000 objects-mo), standard S3 request rates ($0.005 / 1,000 PUT, $0.0004 / 1,000 GET), and
optional **compaction** ($0.002 / 1,000 objects + $0.005 / GB processed). Table buckets are free to
create; **there is no free tier**. AWS's own 1 TB worked example totals ~$28.54/mo with storage
dominating and the S3-Tables-specific fees (monitoring + compaction) only ~$1 combined.

**Order-of-magnitude estimate for THIS workload** (assumptions stated for rule 23 — *not* independently
benchmarked): a single SIP extension at a generous upper bound of ~1,000 calls/month (~33/day),
~40 telemetry fields/call, one ~3 MB recording/call, **batched** writes (~5 data files/day), ~50k
metadata GETs and a few hundred analytical queries/month:

- `call_events` table on S3 Tables: **~$0.05–$1 / month**.
- Recording blobs in **plain S3** (mid-year ~18 GB accumulation): **~$0.40–$1 / month**.
- **Net: order ~$1 / month.** At this volume **cost is not the deciding factor** — operational burden,
  lock-in, and the act of introducing a cloud dependency are.

**Efficiency notes (AGENTS rule 22):** the cost/efficiency risk at low volume is the
small-object/many-files pattern — monitoring and compaction scale with **file count**, not data size —
so the sink **must batch** writes (periodic flush), not emit one file per call. The sink runs **off the
20 ms media hot path** (async/scheduled), adding no per-frame latency. Recordings are stored as plain
objects, never inside a table (a few-MB blob in an Iceberg data file bloats compaction; in DynamoDB it
would exceed the 400 KB item limit).

---

## 5. Privacy, retention & compliance

- **Consent is a hard precondition, not an after-the-fact column.** No recording is written and no
  `recordings_metadata` row is created unless `consent_captured = TRUE` with a valid `consent_basis`;
  in all-party-consent jurisdictions `all_party_consent` must be TRUE. The pipeline **fails closed** —
  no consent ⇒ no capture. Recordings, transcripts, and DTMF are personal data under GDPR requiring an
  Art.6 lawful basis; note that QA-consent ≠ AI-training-consent (purpose limitation).
- **Retention TTL:** `retention_class` → `expires_at`; a scheduled job deletes expired rows + blobs
  unless `legal_hold = TRUE`.
- **Right-to-erasure (GDPR Art.17) is a full, ordered pipeline:** (1) delete the S3 audio object(s)
  (all versions if versioning is on); (2) Iceberg **row-level DELETE** of the matching
  `recordings_metadata` / `call_events` rows — or column-scrub PII (NULL `dtmf_digits` / transcript /
  caller-identifier fields) if a de-identified row must survive for aggregate metrics; (3) **expire
  snapshots** so pre-delete snapshots no longer reference the old data files (otherwise time-travel
  still exposes "forgotten" data); (4) **remove orphan/unreferenced files** so the bytes are physically
  gone. Stamp `erased_at` for audit. On S3 Tables this maps to managed snapshot management (short
  `maxSnapshotAgeHours` + `minSnapshotsToKeep`) + unreferenced-file removal.
- **Sharp edge to document in the runbook:** S3 Tables managed snapshot expiry is **silently disabled**
  if retention is set via Iceberg `TBLPROPERTIES` (`history.expire.max-snapshot-age-ms` /
  `min-snapshots-to-keep`) or branch/tag retention. Keep ALL retention in the
  `PutTableMaintenanceConfiguration` API, **not** `ALTER TABLE TBLPROPERTIES`, or expiry won't run and
  erasure leaks — add a monitoring alert when snapshot age exceeds policy.
- **Encryption at rest:** SSE-KMS with a customer-managed CMK on **both** the table bucket and the
  recordings blob bucket; store only the `kms_key_arn` reference (placeholder), never key material; bind
  KMS encryption-context to `call_id`.
- **Least-privilege IAM (rule 41):** separate scoped roles — a **write-only ingest** role
  (`PutObject` + Iceberg append; no read/delete), a **read-only analytics** role (no delete; column/row
  scoping to deny PII columns), and a tightly-held **erasure** role (the only principal with S3 delete +
  Iceberg delete + maintenance/expire). Never log SRTP keys, `Authorization` headers, transcripts, or
  recording URIs — consistent with this repo's existing redaction invariants.

---

## 6. Alternatives considered

- **Self-managed Iceberg on plain S3.** Cheaper storage ($0.023/GB, no monitoring fee) and stays in
  fully-open formats we control (best fit for the repo's "no lock-in without an ADR" posture). But **we**
  own compaction scheduling, snapshot/orphan-file cleanup, and a catalog (Glue or self-run). At our
  volume the dollar delta is sub-cent; the operational-burden delta is large — which is exactly what
  S3 Tables removes.
- **Plain Parquet on S3 + Athena.** Cheapest storage and no compaction/monitoring fees; query at
  $5/TB scanned (10 MB min, ~$0.01/mo here). But we manage file layout/partitioning (small-file
  problem) and a catalog, and there are no row-level updates/deletes without Iceberg — which would make
  GDPR erasure a file-rewrite chore. Good for append-only logs queried occasionally; weak for mutations.
- **DynamoDB.** Cheapest and lowest-ops managed option (~$0.001/mo; storage free under 25 GB; no
  minimum charge; zero ops). Best for point lookups by `Call-ID` and operational state; weak for ad-hoc
  analytical SQL across calls (needs a GSI design or export to Athena). A strong candidate if access is
  purely point-lookup rather than analytical.
- **Amazon Timestream for LiveAnalytics. DISQUALIFIED** — closed to **new customers** since 2025-06-20
  (maintenance/wind-down), and carries a ~100 GB/region magnetic-store **minimum (~$3/mo floor)** that
  makes it the most expensive option here. Adopting a closed product is a strategic dead end.
- **Self-hosted Postgres / ClickHouse.** $0 in AWS fees, no vendor lock-in, full SQL (PG) or fast
  columnar analytics (CH), can co-locate with the plugin. But it adds a long-lived server to provision,
  patch, secure, monitor, and back up (rule 41 → as code), plus durability risk for call records.
  Sensible only if a host already exists; otherwise it trades a ~$1/mo managed bill for real toil.

**Why S3 Tables is the proposed option:** it gives durable, ACID, time-travel-capable Iceberg tables in
an **open format**, with **managed compaction / snapshot-expiry / orphan-file removal** that directly
implement the GDPR-erasure pipeline we need, a **Spark-free PyIceberg write path** that fits a plain
Python plugin, and a tiny (~$1/mo) bill — while the open Iceberg format keeps the **data** portable
(self-managed Iceberg / GCS / local MinIO for dev) so the *vendor* decision, not the schema, is the
load-bearing approval.

---

## 7. Consequences

- **Positive (if adopted):** a durable, queryable home for the SLO signals runbook 0014 already
  defines; SQL post-incident analysis over call history; governed recordings metadata with first-class
  retention/erasure; near-zero maintenance burden; tiny cost.
- **Cost / lock-in:** introduces an AWS dependency and Iceberg-on-S3-Tables coupling. Mitigated by the
  open Iceberg format and by isolating all cloud code in an optional extra (core stays portable).
- **Honesty caveat (rule 27):** several columns (`error_spoken_to_caller`, per-turn STT/LLM/TTS
  latency, packet-loss/jitter) are **not yet emitted** by the code today. Adopting this model therefore
  implies an **instrumentation workstream**, not just a `CREATE TABLE`; the schema must not claim data
  the plugin doesn't yet produce (columns are nullable / documented as forthcoming until instrumented).

### What adoption requires (the gate)

This ADR is implementable **only after** ALL of the following:

1. **Operator approval** of the cloud dependency **and** the ~dollar/month cost, recorded by flipping
   this ADR's status to **Accepted** with the operator named as a decider (AGENTS rule 40).
2. **A runbook** under `docs/runbooks/` created in the **same change** that provisions the table
   bucket, the plain recordings bucket, the tables, the KMS CMK, and the scoped least-privilege tokens
   — capturing what/why, exact commands/API calls, resource ids/ARNs, how to verify, and how to
   rotate/recreate/restore/roll back (rule 42). It must call out the snapshot-expiry-via-`TBLPROPERTIES`
   sharp edge (§5).
3. **Infrastructure-as-code** for all of the above with least-privilege scoped credentials minted into
   1Password and deployed where used (rule 41); secrets never echoed/logged/committed (rule 34).
4. **No core dependency** is added until then: `boto3`/PyIceberg/AWS SDK stay out of
   `src/hermes_voip/*.py` and live only in the optional extra (+ tests). The default install and runtime
   behaviour are unchanged.

---

## 8. Status: NOT IMPLEMENTED — deferred nice-to-have

Nothing in this ADR is built. The repository does not write Iceberg, S3, or recordings, and this
proposal adds no code and no dependency. It is recorded as a researched, ready-to-adopt design so that
*if and when* the operator wants durable call-events/recordings analytics, the decision, schema,
privacy posture, and provisioning checklist already exist. Until the §7 gate is satisfied, treat this
as a deferred nice-to-have, not a commitment.

---

## References

- AWS S3 Tables overview / tables / maintenance / open-source integration / quotas:
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables.html
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-tables.html
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-maintenance-overview.html
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-maintenance.html
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-table-buckets-maintenance.html
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-open-source.html
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-client-catalog.html
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-regions-quotas.html
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/working-with-apache-iceberg-v3.html
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-considerations.html
- Glue / Lake Formation federation:
  - https://docs.aws.amazon.com/glue/latest/dg/glue-federation-s3tables.html
  - https://docs.aws.amazon.com/glue/latest/dg/enable-s3-tables-catalog-integration.html
  - https://docs.aws.amazon.com/lake-formation/latest/dg/create-s3-tables-catalog.html
- Apache Iceberg (format, partitioning, evolution, spec, GDPR):
  - https://iceberg.apache.org/spec/
  - https://iceberg.apache.org/docs/latest/partitioning/
  - https://iceberg.apache.org/docs/latest/evolution/
  - https://aws.amazon.com/what-is/apache-iceberg/
  - https://www.dremio.com/blog/apache-iceberg-and-the-right-to-be-forgotten/
  - https://www.ryft.io/blog/gdpr-compliance-with-apache-iceberg-a-practical-guide
- PyIceberg / DuckDB write paths:
  - https://py.iceberg.apache.org/api/
  - https://py.iceberg.apache.org/configuration/
  - https://aws.amazon.com/blogs/storage/access-data-in-amazon-s3-tables-using-pyiceberg-through-the-aws-glue-iceberg-rest-endpoint/
  - https://duckdb.org/docs/stable/core_extensions/iceberg/amazon_s3_tables
- Pricing & alternatives:
  - https://aws.amazon.com/s3/pricing/
  - https://www.vantage.sh/blog/amazon-s3-tables
  - https://aws.amazon.com/dynamodb/pricing/on-demand/
  - https://aws.amazon.com/timestream/pricing/
  - https://docs.aws.amazon.com/timestream/latest/developerguide/AmazonTimestreamForLiveAnalytics-availability-change.html
  - https://cloudburn.io/blog/amazon-athena-pricing
- Call-recording consent law (US one/two-party + GDPR):
  - https://www.sembly.ai/blog/call-recording-laws-one-party-vs-two-party-consent/
  - https://gdprlocal.com/gdpr-recording-calls/
  - https://iapp.org/news/a/can-call-centers-rely-on-legitimate-interests-for-audio-recordings
- Repo grounding: `docs/runbooks/0014-voip-slo-metrics.md`, `docs/runbooks/0013-voip-incident-oncall.md`,
  `docs/adr/0036-dtmf-sip-info-and-in-band.md`, `docs/adr/0055-sip-signalling-robustness-refresh-recovery-and-cancel.md`,
  `AGENTS.md` (rules 30, 40, 41, 42).
