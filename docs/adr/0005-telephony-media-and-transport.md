# ADR-0005: In-process media/transport — aiortc-behind-SIP-over-TLS as leading candidate (spike-confirmed), audioop-lts

- **Date:** 2026-06-14
- **Status:** Accepted (amended by ADR-0022; codec-order selection superseded in part by ADR-0078)
- **Deciders:** agent session (VoIP architecture, post-research)

> **Amendment (ADR-0022, 2026-06-17):** the "negotiate by capability, prefer wideband"
> mandate is now realised on the SIP path. The advertised codec menu
> (`adapter._SUPPORTED_ENCODINGS`) and the outbound offer order are **G.722 (wideband, 16 kHz)
> first, then PCMU/PCMA (G.711 fallback), then telephone-event**; the engine carries G.722 via
> a vendored public-domain pure-Python codec. RFC 3264 negotiation falls back to G.711 when
> G.722 is not offered. See ADR-0022.
>
> **Superseded in part (ADR-0078, 2026-06-26):** the clause that negotiation "honours the
> peer's order" no longer holds — `negotiate_audio` now orders the answer by OUR menu
> preference (the answerer's preference, RFC 3264 §6.1), so a peer that offers PCMU before
> our preferred wideband codec is still answered wideband. The wideband-preferred menu and
> G.711 fallback are unchanged. See ADR-0078.

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

- **Signalling must be encrypted.** The plugin registers over **SIP-over-TLS** (SIPS, RFC 3261
  §26.2) — never plaintext SIP/UDP. The gitignored `.env` / 1Password test-gateway credentials
  and the CLAUDE.md invariant make cleartext signalling a non-starter. SIPS secures the
  **signalling** channel; it does **not** by itself imply any particular media-security profile.
- **Media security is negotiated, not assumed.** SIP-over-TLS does **not** imply DTLS-SRTP or
  ICE: many RFC-compliant gateways key media with **SDES-SRTP** (RFC 4568, keys in the
  TLS-protected SDP) or run **RTP/AVP** without ICE at all; DTLS-SRTP (RFC 5763/5764) is the
  WebRTC-style profile and is only one option. Which media-security profile and which transport
  stack we use is therefore a **capability-negotiation outcome the spike must establish against
  the real test gateway**, not an a-priori mandate. (Encrypted media is strongly preferred where
  the gateway offers SAVP/SAVPF; the exact keying — SDES vs DTLS — is what the spike resolves.)
- **Gateway-agnostic, standards-only.** Per CLAUDE.md the core carries no vendor quirks; it
  speaks RFC 3261 (SIP/SIPS), RFC 3264 (SDP offer/answer), and — depending on what the gateway
  negotiates — RFC 4568 (SDES-SRTP), RFC 5763/5764 (DTLS-SRTP), and RFC 8445 (ICE). Anything
  gateway-specific is a value in `HERMES_SIP_*`, not a code path.
- **Everything streams** (ADR-0008): the silence→first-audio budget is ~800 ms–1 s, so the
  media layer must deliver and emit audio frame-by-frame, not buffer whole utterances. It also
  owns the **clock**: 20 ms packetisation, an adaptive de-jitter buffer, and packet-loss
  concealment (PLC) so STT/VAD see a clean monotonic stream.
- **8 kHz reality.** Telephony default is G.711 at 8 kHz; the STT path (ADR-0006) requires
  16 kHz, so the layer must resample 8↔16 kHz and transcode G.711 ↔ PCM16. silero-vad
  (ADR-0008) runs natively at **either** 8 kHz or 16 kHz, so VAD does **not** force the
  resample; we feed VAD the same 16 kHz stream as STT purely to share one resample across both
  consumers (a design choice, not a VAD constraint). Python 3.13 **removed stdlib `audioop`**,
  so a replacement codec is required.
- **Rule 40/41 infra gate.** Introducing an external media server (a separate process/host that
  terminates SIP/SRTP and streams PCM to us) is *infrastructure* and requires explicit operator
  approval recorded in an ADR. That gate is why the convenient "buy" options are not the default
  — they are recorded here, not silently adopted.
- **Rules 23/24/26.** Every vendor/library latency figure cited in research is model-only or
  lab-only; on our 8 kHz, NAT-traversed, real-gateway path they are unverified until measured
  against the real test gateway.

DTMF is a sibling concern handled in ADR-0010 (RFC 4733 telephone-event rides this same RTP
path; the `aiortc` candidate does not encode it for us, whereas the `pjsua2` candidate provides
DTMF natively — another input to the spike's transport choice).

## Decision

**The plugin builds its telephony media engine in-process, with `audioop-lts` for G.711
transcode and 8↔16 kHz resampling and a plugin-owned adaptive de-jitter/PLC layer. The
*leading candidate* for the transport itself is `aiortc` (asyncio-native RTP/SRTP, Opus/G.711)
behind a thin SIP-over-TLS signalling layer — to be CONFIRMED by the de-risking spike, not
asserted here.** `aiortc` is a WebRTC/ORTC stack, **not** a SIP user-agent, and its SRTP keying
is **DTLS-SRTP**; whether that interoperates with the real gateway depends on what the gateway
negotiates. **PJSIP/`pjsua2` is a first-class candidate** — and is likely the better fit for
SIP-side **SDES-SRTP** (RFC 4568) interop, which many gateways prefer. The spike picks the
stack from the capability matrix below; both candidates implement ADR-0004's `MediaTransport`
seam, so the choice does not leak upward.

**Capability matrix the spike MUST fill against the real test gateway** (the result, not this
table, decides the transport):

| Dimension | Options the spike records |
| --------- | ------------------------- |
| Signalling | SIPS (SIP-over-TLS) — required |
| Media security | SDES-SRTP (RFC 4568) \| DTLS-SRTP (RFC 5763/5764) \| RTP/AVP (unencrypted, gateway-permitting) |
| ICE | none \| ICE-lite \| full ICE (RFC 8445) |
| RTP profile | AVP \| AVPF \| SAVP \| SAVPF |
| DTMF | RFC 4733 telephone-event \| SIP INFO \| in-band (ADR-0010) |

The selected stack is whichever interoperates with the gateway's actual answer across that
matrix: `aiortc` covers SAVP/SAVPF + DTLS-SRTP (+ ICE) cleanly; `pjsua2` covers SDES-SRTP and
AVP/AVPF without ICE more naturally. Encrypted media (SAVP/SAVPF) is preferred wherever the
gateway offers it.

Concrete shape:

- **Dependencies** (declared in `pyproject.toml`, locked in `uv.lock`, installed `uv sync
  --frozen` per rule 38; the media stack is pinned once the spike selects it):
  - `aiortc` (leading-candidate transport) — RTP/SRTP, DTLS-SRTP (RFC 5763/5764), ICE
    (RFC 8445), Opus + G.711 (PCMU/PCMA) media on the asyncio loop. WebRTC/ORTC, not a SIP
    stack, so a SIP-over-TLS signalling layer sits on top of it.
  - `pjsua2`/PJSIP (first-class candidate) — a mature SIP user-agent with native SIP-over-TLS,
    SDES-SRTP (RFC 4568), AVP/AVPF, and DTMF; selected if the gateway negotiates SDES-SRTP or
    declines ICE/DTLS-SRTP. Trade-off recorded in Consequences (native build + thread bridge).
  - `audioop-lts` — the maintained replacement for the removed stdlib `audioop`; supplies
    `lin2ulaw`/`ulaw2lin`/`lin2alaw`/`alaw2lin` (G.711 transcode) and `ratecv` (8↔16 kHz
    resampling). PSF-licensed (CPython stdlib backport); CPU-only; no native model. Pinned
    regardless of which transport the spike selects.
  - A SIP-over-TLS signalling layer (only needed for the `aiortc` path): a thin RFC 3261/3264
    SIPS client over `asyncio` TLS for REGISTER / INVITE / re-INVITE / BYE and SDP offer/answer,
    wiring the negotiated codec and the negotiated media-keying (DTLS-SRTP fingerprints **or**
    SDES crypto attributes, per the gateway's answer) into the session. With the `pjsua2` path
    this signalling is the library's, not ours.

- **Codec negotiation by capability, not by hardcoded assumption** (RFC 3264). The SDP offer
  advertises, in preference order, **Opus** then **G.711 µ-law (PCMU)** then **G.711 A-law
  (PCMA)**; the session uses whatever the gateway answers, defaulting to G.711 (universal, 8 kHz)
  and preferring Opus when offered. No vendor-specific payload-type or fmtp assumptions in core.

- **Internal media contract** (the audio shape ADR-0006/0008 consume): the media layer
  implements **ADR-0004's canonical `MediaTransport` Protocol** (`inbound_audio()` /
  `send_audio()` / `inbound_sample_rate`) and speaks **ADR-0004's canonical `PcmFrame`** — PCM16
  at a declared `sample_rate` carrying the `monotonic_ts_ns` de-jittered presentation clock.
  This ADR does **not** redefine `PcmFrame` or introduce a second media protocol: the timestamp
  field now lives on the canonical `PcmFrame` in ADR-0004 precisely so transport and providers
  share one type. Transcode/resample is centralised here so STT/VAD/TTS never touch G.711 or
  8 kHz framing directly. The only transport-local type is the negotiated wire codec:

```python
from __future__ import annotations

from enum import Enum

# PcmFrame and the MediaTransport Protocol are imported from ADR-0004's canonical
# module; they are NOT redefined here:
#   from hermes_voip.providers.audio import PcmFrame
#   from hermes_voip.providers.transport import MediaTransport


class NarrowbandCodec(Enum):
    """RTP audio codecs this engine negotiates (RFC 3264 offer/answer)."""

    PCMU = "PCMU"  # G.711 mu-law, 8 kHz (telephony default, universal)
    PCMA = "PCMA"  # G.711 A-law, 8 kHz
    OPUS = "opus"  # preferred when the gateway offers it
```

The in-process engine for one established call is a concrete `MediaTransport` (ADR-0004): it
owns the RTP/SRTP transport (aiortc or pjsua2, per the spike), the de-jitter/PLC buffer, the
20 ms packetisation clock, the negotiated `NarrowbandCodec`, and G.711↔PCM16 + 8↔16 kHz
transcode (`audioop-lts`). `inbound_audio()` emits 16 kHz `PcmFrame`s for STT/VAD
(ADR-0006/0008); `send_audio()` transcodes outbound TTS PCM16 back to the negotiated wire
codec.

- **Configuration / secrecy.** All gateway-specific connection facts are read at runtime from
  `HERMES_SIP_*` environment variables (host, port, transport, registration identity,
  credential) — sourced from the gitignored `.env` and 1Password via the `op` CLI, never tracked.
  Tests and examples use obvious fakes only: host `pbx.example.test`, extension `1000`. No real
  host/extension/IP/credential appears in code, fixtures, or this ADR.

- **De-risking spike** (precedes hardening AND confirms the transport choice, per rules 23/26):
  one **real inbound SIP-over-TLS call** from the test gateway, answered, **half-duplex**
  (matching Hermes voice mode, ADR-0008), audio decoded → STT → TTS → back to the caller. The
  spike **fills the capability matrix above** — recording the gateway's negotiated media-security
  profile (SDES-SRTP vs DTLS-SRTP vs RTP/AVP), ICE mode, RTP profile, and DTMF mode — and on
  that result **confirms aiortc-behind-SIP-over-TLS or selects PJSIP/`pjsua2`** for SDES-SRTP
  interop. It proves REGISTER/INVITE/SDP + whatever media-keying the gateway negotiated, plus
  end-to-end audio on the real 8 kHz path. Every latency number (endpointing, first-audio,
  jitter) is **measured on this path** and recorded — no research figure is carried forward
  unverified. Until the spike completes, the transport is a **leading candidate**, not an
  accepted default.

- **Build-vs-buy spectrum, recorded (not adopted).** A media-server "front door" — **LiveKit
  Agents** (Apache-2.0) with its self-hosted SIP bridge, or **Asterisk AudioSocket** /
  **FreeSWITCH** / **jambonz** terminating SIP/SRTP and streaming us PCM — would offload
  SIP/SRTP interop and jitter/PLC. These are **general, well-known OSS SIP/telephony projects
  under evaluation as build-vs-buy options — none is the project's test gateway**, so naming
  them is not a secrecy concern (the test gateway's vendor/product stays only in the gitignored
  `.env` / 1Password). Each introduces a separate process/service = **infrastructure**, which
  under rules 40/41 requires **explicit operator approval in its own ADR** before adoption. It is
  therefore **not the default**; it remains the sanctioned escalation if in-process interop
  proves uneconomic, and its adoption would supersede this ADR's transport decision.

The HOW — standing up the spike, building the selected transport (aiortc or pjsua2), the spike
runbook, the capability-matrix + codec-negotiation test matrix, jitter-buffer tuning, and the
aiortc↔PJSIP switch procedure — lives in `docs/runbooks/`, not here.

## Consequences

- **Easier:** zero IPC and zero new infrastructure — the media engine lives on the agent's own
  event loop, so there is nothing to deploy, secure, or operate beyond the plugin itself (rule 40
  satisfied by construction). The `aiortc` candidate is asyncio-native, so SRTP/DTLS/ICE compose
  cleanly without thread bridges *if* the gateway negotiates that profile; capability-based codec
  and media-security negotiation keeps the core gateway-agnostic, with quirks confined to
  `HERMES_SIP_*`. Both candidate transports sit behind ADR-0004's `MediaTransport`, so switching
  between them after the spike is a registry change, not a core rewrite.
- **Harder / committed to maintain:** we own an adaptive de-jitter/PLC layer and — on the
  `aiortc` path — a SIP/SDP signalling layer (REGISTER refresh, INVITE/re-INVITE/BYE,
  offer/answer, and wiring whichever media-keying the gateway negotiated: DTLS-SRTP fingerprints
  **or** SDES crypto attributes). That is real telephony-stack surface area — NAT/ICE edge cases,
  clock drift, SRTP keying interop — that must be tested against a real gateway, not just
  unit-mocked, and is exactly why the transport is spike-confirmed rather than asserted.
  `audioop-lts` is an extra pinned dependency tracking the removed stdlib module; G.711 default
  means an 8↔16 kHz resample on every frame in both directions (a measured CPU cost on the hot
  path, re-checked per ADR-0008's efficiency pass).
- **Latency/operational:** in-process media means call audio shares the event loop with STT/TTS
  orchestration; any blocking work on that loop directly degrades call quality, so all heavy
  inference stays off-loop (ADR-0006/0007). Latency targets are budgets to be *measured* on the
  real path, never assumed (rules 23/26).
- **Lock-in / cost:** **no vendor or platform lock-in and no recurring cost** — `aiortc` +
  `audioop-lts` are OSS libraries pinned in `uv.lock`, not a service. Upgrade cadence follows
  those libraries' releases under our normal dependency-bump + license/advisory gate (rule 35).
- **PJSIP candidate cost (recorded):** selecting **PJSIP/`pjsua2`** (a first-class candidate,
  likely the better fit for SIP-side SDES-SRTP interop) trades pure-Python simplicity for a
  **native build** in the image/CI and a **thread→asyncio bridge** (pjsua2 runs its own threads;
  the precedent `agent.async_utils.safe_schedule_threadsafe` exists but its signature is
  unverified). If the spike's capability matrix shows the gateway prefers SDES-SRTP or declines
  ICE/DTLS-SRTP, this is the selected stack rather than a later fallback; the choice is recorded
  when the spike resolves it.
- **Escalation cost (recorded):** a media-server front door would *reduce* our interop surface but
  *add* an operated service (deploy, secure, monitor, rotate) and require operator approval per
  rules 40/41 — a deliberate trade we do not pre-commit to.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Plain SIP/UDP + cleartext signalling | Violates the encryption invariant: registration carries gateway credentials, so SIPS (SIP-over-TLS, RFC 3261 §26.2) is mandatory. Cleartext **signalling** is never an option. (Media-security keying — SDES-SRTP vs DTLS-SRTP vs, where a gateway only offers it, RTP/AVP — is a negotiated capability the spike records, not a fixed mandate; encrypted media is preferred wherever the gateway offers SAVP/SAVPF.) |
| Assert DTLS-SRTP + ICE as the required media path | Over-broad: SIP-over-TLS secures signalling only and does **not** imply DTLS-SRTP or ICE. Many RFC-compliant gateways key media with SDES-SRTP (RFC 4568) or run RTP/AVP without ICE. Mandating the WebRTC profile would exclude compliant gateways; the profile is decided by capability negotiation in the spike. |
| Commit `aiortc`-behind-SIP as the **accepted default** before the spike | `aiortc` is a WebRTC/ORTC stack, not a SIP user-agent, and keys media with DTLS-SRTP; whether that interoperates with a given gateway's SDP answer is unproven until measured. It is the **leading candidate**, confirmed (or replaced by `pjsua2`) by the de-risking spike, not asserted. |
| PJSIP / `pjsua2` ruled out | A **first-class candidate**, not ruled out: mature SIP/SRTP with native SIP-over-TLS, **SDES-SRTP**, AVP/AVPF, and DTMF — likely the better fit if the gateway prefers SDES-SRTP or declines ICE/DTLS-SRTP. Its cost (native build in image/CI, thread→asyncio bridge) is recorded; the spike's matrix decides between it and `aiortc`. |
| Run the media stack out-of-process | Impossible without new infrastructure: the adapter shares one process/event loop with the agent via a synchronous `queue.Queue` (ADR-0002), and Hermes hands media as file paths, not streams (ADR-0003). Any external media process is infra gated by rules 40/41. |
| A self-hosted SIP media-server "front door" (e.g. LiveKit Agents, Asterisk AudioSocket, FreeSWITCH, jambonz — general OSS projects under evaluation, **not** the test gateway) | Each would offload SIP/SRTP/jitter but is a separate operated service = infrastructure requiring explicit operator approval per rules 40/41, and reintroduces PBX-specific surface the core avoids. Recorded as a sanctioned escalation, not the default. |
| Full WebRTC-only (no SIP) | The first **test** target and the broad "any RFC-compliant SIP-over-TLS **or** WebRTC gateway" mandate require SIP registration as an extension; dropping SIP would abandon the primary integration path. The `aiortc` candidate already covers the WebRTC/DTLS-SRTP side, so SIP signalling is additive, not exclusive. |

