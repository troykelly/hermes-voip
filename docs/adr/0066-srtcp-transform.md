# ADR-0066: SRTCP transform (RFC 3711 §3.4)

- **Date:** 2026-06-19
- **Status:** Accepted
- **Deciders:** agent session (SRTCP transform lane)

## Context

ADR-0061 added the RTCP control channel (SR/RR/SDES/BYE, `src/hermes_voip/rtcp.py`)
and ADR-0063 wired it into the adapter — but **only on the cleartext plain-RTP
path**. On every *secured* media path (SDES SRTP per ADR-0053 Stage 1, DTLS-SRTP
per ADR-0053 Stage 2, and WebRTC) RTCP is deliberately **dormant**, because the
engine emits and parses *cleartext* RTCP only and `media/srtp.py` has **no SRTCP
transform**. Sending cleartext RTCP on an encrypted 5-tuple would leak the SSRC,
the SDES CNAME, and reception/timing metadata in the clear and violate the SAVP /
SAVPF / UDP-TLS-RTP-SAVP profile the call negotiated. The codex review of the RTCP
activation lane locked this in as a fail-closed gate (`_plan_rtcp_activation`
refuses to activate unless the answered profile is exactly `RTP/AVP`).

The live Grandstream test gateway uses SDES-SRTP, so in production RTCP is dormant
until SRTCP lands. RFC 3711 §3.4 defines the missing piece: the SRTCP transform
that protects (encrypt + authenticate) a compound RTCP packet, mirroring how SRTP
(ADR-0013) protects an RTP packet.

Constraints (AGENTS.md): fully-typed and clean under `mypy --strict` with no escape
hatches (rules 17/39); TDD with real vectors (rule 18); errors propagate (rule 37);
minimal in-scope diff (rule 28); the live transport/socket wiring is an adapter/engine
concern, not this module's (the ADR-0061/0063 boundary). The repo is PUBLIC, so test
key material must not be a literal that trips the gitleaks scan (CLAUDE.md invariant).

## Decision

Add `src/hermes_voip/media/srtcp.py` — the payload-level SRTCP transform per
RFC 3711 §3.4 — as a self-contained module that **reuses** the SRTP crypto
machinery and is **not yet wired into the engine**. Flipping the engine seam to
emit/ingest secured RTCP is a separate, named follow-on (it is not built here).

### 1. Reuse, don't duplicate, the SRTP primitives

SRTCP shares SRTP's master key/salt, the AES-CM keystream, HMAC-SHA1, the lazily
imported `cryptography` backend (the narrow-Protocol seam, ADR-0013), the two
supported suites (`AES_CM_128_HMAC_SHA1_80` / `_32` with 10/4-byte tags), the
session-key lengths, and the 64-packet replay-window constants. `srtcp.py` imports
these directly from `srtp.py` — **no edit to `srtp.py`** and no second copy of the
cipher plumbing. `_reject_session_params` (the MKI/lifetime rejection that never
echoes the key) is reused and re-raised as the module-typed `SrtcpError`.

### 2. The three RFC 3711 §3.4 differences from SRTP

- **KDF labels.** Session keys derive from the *same* master key/salt as SRTP but
  with labels `0x03` (RTCP encryption key), `0x04` (RTCP auth key), `0x05` (RTCP
  salt) — distinct from SRTP's `0x00`/`0x01`/`0x02`, so the RTCP and RTP keystreams
  provably differ (a unit test asserts all three derived keys differ from SRTP's).
- **Explicit 31-bit SRTCP index.** In place of SRTP's implicit ROC/SEQ index, every
  SRTCP packet carries a 31-bit index in a 4-byte trailer word whose MSB is the
  **E (encrypt) flag**. The index is the `i` in the AES-CM IV
  (`IV = (k_s||0000) XOR (SSRC<<64) XOR (index<<16)`). Index 0 is reserved unused:
  the first protected packet carries index 1, incrementing by one per sent packet,
  and the index is never reset on re-key. The index space **must not wrap** under a
  single master key — a wrapped index reproduces a prior IV and reuses the keystream
  (a two-time pad), so `protect()` **raises `SrtcpError` on exhaustion** (reaching
  `0x7fffffff`) instead of wrapping; continuing requires a re-key (a fresh
  `SrtcpSession`). At one RTCP compound every few seconds (RFC 3550 §6.2) this is a
  safety assertion, not a live limit.
- **Encrypt from the ninth octet; auth without ROC.** Only octets 9..end (the RTCP
  payload) are encrypted; octets 0..7 (the first report's header + sender SSRC) stay
  in the clear. The auth tag is HMAC-SHA1 over the **entire** packet plus the
  E-flag + index word (after encryption), with **no** ROC appended (unlike SRTP
  §4.2). On inbound the tag is verified **first**, constant-time
  (`hmac.compare_digest`), and decrypt only runs once it passes.

### 3. Session shape mirrors `SrtpSession`

`SrtcpSession` is a `@dataclass(slots=True)` with both keying paths — SDES
(`SrtcpSession(crypto)`) and DTLS-SRTP (`SrtcpSession.from_raw_keys(key, salt,
suite=)`). All key-bearing fields are `field(repr=False)` so key material never
appears in `repr` or tracebacks; auth-failure messages are structural only. It is
bound to **one SSRC** (RFC 3711 §3.2.3), fixed at construction or captured from the
first packet, and rejects a foreign SSRC *before* mutating index/replay state.
Replay protection keeps a **separate** 64-index replay list keyed on the explicit
SRTCP index (RFC 3711 §3.3.2): the receiver verifies auth, binds the SSRC, checks
the replay window, decrypts, then records the index.

The SDES inline-key decode is **self-defending** (defence in depth): although
`CryptoAttribute` already validates the `inline:` key on the normal path, this
module decodes with `base64` `validate=True`, rejects non-base64 input, and requires
exactly key(16)+salt(14)=30 octets — raising a typed `SrtcpError` (never a raw
`binascii`/index error or silently-malformed keys), and never echoing the corrupt
token. It does not *rely* on the upstream validator's invariant.

### 4. Always-encrypt on the send path (E=1)

`protect()` always sets the E flag and encrypts the payload. RFC 3711 §3.4 permits
unencrypted-but-authenticated SRTCP (E=0), and `unprotect()` **honours** an inbound
E=0 packet (returns the cleartext payload after the mandatory auth check) for
interoperability — but this plugin only ever serves the SAVP family, where leaving
RTCP cleartext defeats the purpose, so we never *originate* E=0.

## Consequences

- A future lane can flip the engine to secure RTCP on the SDES / DTLS-SRTP / WebRTC
  paths by constructing an `SrtcpSession` per direction (from the same master
  key/salt already derived for SRTP) and routing the engine's RTCP datagrams
  through `protect`/`unprotect`. Until then RTCP stays dormant on secured paths —
  unchanged, fail-closed behaviour.
- **Out of scope (deliberate, named follow-on):** wiring `srtcp.py` into
  `media/engine.py` and `adapter.py` (the secured-path RTCP activation), and any
  live validation against the gateway. This ADR records only the transform.
- Authentication is mandatory and verified before decrypt; a tampered or
  wrong-keyed packet raises `SrtcpError` and is never silently accepted (rule 37).

## Alternatives considered

- **Fold SRTCP into `SrtpSession`.** Rejected: the index is explicit (not ROC/SEQ),
  the encrypted region differs (octet 9 vs the RTP header boundary), and the auth
  input omits the ROC — overloading one class with two wire formats would muddy the
  hot path and the typing. A sibling module that reuses the primitives is cleaner
  and keeps each `protect`/`unprotect` single-purpose.
- **A third-party SRTP/SRTCP library (e.g. pylibsrtp).** Rejected for the same
  reasons ADR-0013 rejected it for SRTP: it pulls a native libsrtp dependency and
  vendor lock-in (AGENTS.md rule 40) where a small, fully-typed, KAT-tested
  transform over `cryptography` (already a dependency) suffices.

## Verification

TDD (rule 18): the RED test commit fails to import the missing module; the GREEN
commit implements to pass. `tests/test_media_srtcp.py` covers the SRTCP KDF labels
(and that they differ from SRTP's), the 31-bit-index IV (literal hand-derived KAT),
protect→unprotect round-trips (SR/RR/BYE, both tag lengths), the wire layout
(8-octet clear prefix, encrypted payload, E-flag + index trailer, length growth),
authentication over the whole packet + E + index with **no** ROC (reproduced
independently), tamper rejection (every region), wrong-key/wrong-suite rejection,
replay protection on the explicit index, per-SSRC binding + no-state-mutation on a
rejected foreign SSRC, malformed-input rejection, the DTLS `from_raw_keys` path, and
key-material redaction. Synthetic key material is **computed at runtime**
(`bytes(range(...))` / `hashlib` of a constant), not a base64/hex literal, so the
public-repo gitleaks scan sees nothing key-shaped — no `.gitleaks.toml` allowlist
entry is needed.
