# ADR-0005: In-process media/transport: aiortc + SIP-over-TLS signalling + audioop-lts (build-vs-buy recorded)

- **Date:** 2026-06-14
- **Status:** Accepted
- **Deciders:** agent session (VoIP architecture, post-research)

## Context

ADR-0002 commits to a `kind: platform` plugin whose adapter `connect()` owns a persistent
voice registration and feeds the agent one discrete `MessageEvent` per turn, and ADR-0003
commits to a cascaded STT → Hermes → TTS path. Both run inside the **one** Hermes process and
event loop the adapter shares with the agent (the adapter↔agent handoff is a synchronous
`queue.Queue`). That single hard constraint forces this decision: the real-time SIP/RTP media
engine **must run in-process on that event loop** — there is no second process to escape to,
and no streaming bytes API to hand media off through (Hermes media is whole-file paths, per
ADR-0003). So the plugin owns a telephony media stack itself.

The stack must satisfy several bounding constraints at once:

- **Transport must be encrypted.** The plugin registers over **SIP-over-TLS** (SIPS, RFC 3261
  §26.2) with media keyed by **DTLS-SRTP** (RFC 5763/5764) — never plaintext SIP/UDP or
  unprotected RTP. The gitignored `.env` / 1Password test-gateway credentials and the CLAUDE.md
  invariant make cleartext signalling a non-starter.
- **Gateway-agnostic, standards-only.** Per CLAUDE.md the core carries no vendor quirks; it
  speaks RFC 3261 (SIP/SIPS), RFC 3264 (SDP offer/answer), RFC 5763/5764 (DTLS-SRTP), RFC 8445
  (ICE). Anything gateway-specific is a value in `HERMES_SIP_*`, not a code path.
- **Everything streams** (ADR-0008): the silence→first-audio budget is ~800 ms–1 s, so the
  media layer must deliver and emit audio frame-by-frame, not buffer whole utterances. It also
  owns the **clock**: 20 ms packetisation, an adaptive de-jitter buffer, and packet-loss
  concealment (PLC) so STT/VAD see a clean monotonic stream.
- **8 kHz reality.** Telephony default is G.711 at 8 kHz; the STT/VAD path (ADR-0006, ADR-0008)
  wants 16 kHz, and silero-VAD hard-rejects 8 kHz. So the layer must resample 8↔16 kHz and
  transcode G.711 ↔ PCM16. Python 3.13 **removed stdlib `audioop`**, so a replacement codec is
  required.
- **Rule 40/41 infra gate.** Introducing an external media server (a separate process/host that
  terminates SIP/SRTP and streams PCM to us) is *infrastructure* and requires explicit operator
  approval recorded in an ADR. That gate is why the convenient "buy" options are not the default
  — they are recorded here, not silently adopted.
- **Rules 23/24/26.** Every vendor/library latency figure cited in research is model-only or
  lab-only; on our 8 kHz, NAT-traversed, real-gateway path they are unverified until measured
  against the real test gateway.

DTMF is a sibling concern handled in ADR-0010 (RFC 4733 telephone-event rides this same RTP
path, which `aiortc` does not encode for us).

## Decision

**The plugin builds its telephony media engine in-process on `aiortc` (asyncio-native
RTP/SRTP, Opus/G.711, DTLS-SRTP, ICE) plus a thin SIP-over-TLS signalling layer, with
`audioop-lts` for G.711 transcode and 8↔16 kHz resampling, and a plugin-owned adaptive
de-jitter/PLC layer.** `aiortc` is the media/DTLS-SRTP/ICE engine but is **not** a SIP stack,
so the plugin supplies SIP/SDP signalling over TLS on top of it. **PJSIP/`pjsua2` is the
recorded fallback** if pure-Python SIP-over-TLS/SRTP interop against the real test gateway
proves brittle.

Concrete shape:

- **Dependencies** (declared in `pyproject.toml`, locked in `uv.lock`, installed `uv sync
  --frozen` per rule 38):
  - `aiortc` — RTP/SRTP, DTLS-SRTP (RFC 5763/5764), ICE (RFC 8445), Opus + G.711 (PCMU/PCMA)
    media transport on the asyncio loop.
  - `audioop-lts` — the maintained replacement for the removed stdlib `audioop`; supplies
    `lin2ulaw`/`ulaw2lin`/`lin2alaw`/`alaw2lin` (G.711 transcode) and `ratecv` (8↔16 kHz
    resampling). PSF-licensed (CPython stdlib backport); CPU-only; no native model.
  - A SIP-over-TLS signalling layer: a thin RFC 3261/3264 SIPS client over `asyncio` TLS for
    REGISTER / INVITE / re-INVITE / BYE and SDP offer/answer, wiring negotiated codec + DTLS-SRTP
    fingerprints into the `aiortc` session.

- **Codec negotiation by capability, not by hardcoded assumption** (RFC 3264). The SDP offer
  advertises, in preference order, **Opus** then **G.711 µ-law (PCMU)** then **G.711 A-law
  (PCMA)**; the session uses whatever the gateway answers, defaulting to G.711 (universal, 8 kHz)
  and preferring Opus when offered. No vendor-specific payload-type or fmtp assumptions in core.

- **Internal media contract** (the audio shape ADR-0006/0008 consume): the media layer exposes
  a frame stream of 16-bit mono PCM with explicit sample rate and a monotonic timestamp, and
  accepts the same shape for TTS-out. Transcode/resample is centralised here so STT/VAD/TTS never
  touch G.711 or 8 kHz framing directly.

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class NarrowbandCodec(Enum):
    """RTP audio codecs this engine negotiates (RFC 3264 offer/answer)."""

    PCMU = "PCMU"  # G.711 mu-law, 8 kHz (telephony default, universal)
    PCMA = "PCMA"  # G.711 A-law, 8 kHz
    OPUS = "opus"  # preferred when the gateway offers it


@dataclass(frozen=True, slots=True)
class PcmFrame:
    """One decoded mono PCM16 audio frame on the internal media contract."""

    samples: bytes          # signed 16-bit little-endian mono PCM
    sample_rate_hz: int     # 8000 off the wire, 16000 into STT/VAD
    monotonic_ts_ns: int    # de-jittered, gap-free presentation clock


class MediaSession(Protocol):
    """In-process media engine for one established call.

    Owns the aiortc RTP/SRTP transport, the de-jitter/PLC buffer, the 20 ms
    packetisation clock, and G.711<->PCM16 + 8<->16 kHz transcode (audioop-lts).
    Inbound frames are emitted at 16 kHz for STT/VAD (ADR-0006/0008); outbound
    TTS PCM16 is transcoded back to the negotiated wire codec.
    """

    codec: NarrowbandCodec

    def inbound_pcm(self) -> AsyncIterator[PcmFrame]: ...

    async def send_pcm(self, frame: PcmFrame) -> None: ...

    async def close(self) -> None: ...
```

- **Configuration / secrecy.** All gateway-specific connection facts are read at runtime from
  `HERMES_SIP_*` environment variables (host, port, transport, registration identity,
  credential) — sourced from the gitignored `.env` and 1Password via the `op` CLI, never tracked.
  Tests and examples use obvious fakes only: host `pbx.example.test`, extension `1000`. No real
  host/extension/IP/credential appears in code, fixtures, or this ADR.

- **De-risking spike** (precedes hardening, per rules 23/26): one **real inbound SIP-over-TLS
  call** from the test gateway, answered, **half-duplex** (matching Hermes voice mode, ADR-0008),
  audio decoded → STT → TTS → back to the caller, proving REGISTER/INVITE/SDP/DTLS-SRTP interop
  and end-to-end audio on the real 8 kHz path. Every latency number (endpointing, first-audio,
  jitter) is **measured on this path** and recorded — no research figure is carried forward
  unverified.

- **Build-vs-buy spectrum, recorded (not adopted).** A media-server "front door" — **LiveKit
  Agents** (Apache-2.0) with its self-hosted SIP bridge, or **Asterisk AudioSocket** /
  **jambonz** terminating SIP/SRTP and streaming us PCM — would offload SIP/SRTP interop and
  jitter/PLC. Each introduces a separate process/service = **infrastructure**, which under rules
  40/41 requires **explicit operator approval in its own ADR** before adoption. It is therefore
  **not the default**; it remains the sanctioned escalation if in-process interop proves
  uneconomic, and its adoption would supersede this ADR's transport decision.

The HOW — building `aiortc`, the spike runbook, codec-negotiation test matrix, jitter-buffer
tuning, and a PJSIP-fallback switch procedure — lives in `docs/runbooks/`, not here.

## Consequences

- **Easier:** zero IPC and zero new infrastructure — the media engine lives on the agent's own
  event loop, so there is nothing to deploy, secure, or operate beyond the plugin itself (rule 40
  satisfied by construction). `aiortc` is asyncio-native, so SRTP/DTLS/ICE compose cleanly with
  the rest of the plugin without thread bridges. Capability-based codec negotiation keeps the core
  gateway-agnostic; gateway quirks stay confined to `HERMES_SIP_*`.
- **Harder / committed to maintain:** we now own a SIP/SDP signalling layer (REGISTER refresh,
  INVITE/re-INVITE/BYE, offer/answer, DTLS-SRTP key wiring) and an adaptive de-jitter/PLC layer —
  real telephony-stack surface area, including NAT/ICE edge cases and clock drift, that must be
  tested against a real gateway, not just unit-mocked. `audioop-lts` is an extra pinned dependency
  tracking the removed stdlib module; G.711 default means an 8↔16 kHz resample on every frame in
  both directions (a measured CPU cost on the hot path, re-checked per ADR-0008's efficiency pass).
- **Latency/operational:** in-process media means call audio shares the event loop with STT/TTS
  orchestration; any blocking work on that loop directly degrades call quality, so all heavy
  inference stays off-loop (ADR-0006/0007). Latency targets are budgets to be *measured* on the
  real path, never assumed (rules 23/26).
- **Lock-in / cost:** **no vendor or platform lock-in and no recurring cost** — `aiortc` +
  `audioop-lts` are OSS libraries pinned in `uv.lock`, not a service. Upgrade cadence follows
  those libraries' releases under our normal dependency-bump + license/advisory gate (rule 35).
- **Fallback cost (recorded):** choosing **PJSIP/`pjsua2`** later trades pure-Python simplicity
  for a **native build** in the image/CI and a **thread→asyncio bridge** (pjsua2 runs its own
  threads; the precedent `agent.async_utils.safe_schedule_threadsafe` exists but its signature is
  unverified) — accepted only if pure-Python SIPS/SRTP interop against the test gateway proves too
  brittle, and recorded as a new ADR when taken.
- **Escalation cost (recorded):** a media-server front door would *reduce* our interop surface but
  *add* an operated service (deploy, secure, monitor, rotate) and require operator approval per
  rules 40/41 — a deliberate trade we do not pre-commit to.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Plain SIP/UDP + unprotected RTP | Violates the encryption invariant: registration carries gateway credentials and media is a voice call; SIPS + DTLS-SRTP are mandatory (RFC 3261 §26.2, RFC 5763/5764). Cleartext is never an option. |
| Run the media stack out-of-process | Impossible without new infrastructure: the adapter shares one process/event loop with the agent via a synchronous `queue.Queue` (ADR-0002), and Hermes hands media as file paths, not streams (ADR-0003). Any external media process is infra gated by rules 40/41. |
| PJSIP / `pjsua2` as the **primary** stack | Mature SIP/SRTP, but pulls a native build into image/CI and runs its own threads needing an unverified thread→asyncio bridge onto the shared loop — added complexity we take only as a fallback if pure-Python interop is brittle, not as the default. |
| LiveKit Agents (self-host SIP) front door | Apache-2.0 and would offload SIP/SRTP/jitter, but is a separate operated service = infrastructure requiring explicit operator approval per rules 40/41. Recorded as a sanctioned escalation, not the default. |
| Asterisk AudioSocket / FreeSWITCH / jambonz front door | Same infra gate (rules 40/41): a media server to deploy, secure, and operate; also reintroduces gateway/PBX-specific surface the core is meant to avoid. Escalation only, operator-approved, never silent. |
| Full WebRTC-only (no SIP) | The first **test** target and the broad "any RFC-compliant SIP-over-TLS **or** WebRTC gateway" mandate require SIP registration as an extension; dropping SIP would abandon the primary integration path. `aiortc` already covers the WebRTC/DTLS-SRTP side, so SIP signalling is additive, not exclusive. |

