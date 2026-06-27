# Changelog

All notable changes to `hermes-voip` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The package version is **single-sourced** from `pyproject.toml [project].version`
(see [docs/runbooks/0019-release-process.md](docs/runbooks/0019-release-process.md));
`hermes_voip.__version__` and the `plugin.yaml` manifest version track it and are
pinned equal by the test suite.

## [Unreleased]

## [0.1.1] - 2026-06-27

### Added

- **Automated publish on tag** — pushing a `vX.Y.Z` tag (including pre-releases
  like `v1.2.3-rc1`) runs `.github/workflows/publish.yml`: a `build` job guards the
  tag against `pyproject.toml [project].version`, builds + wheel-smokes the wheel +
  sdist, and uploads them; a `github-release` job attaches them (plus `SHA256SUMS`)
  to a GitHub Release; and an independent `pypi-publish` job publishes them to PyPI
  via OIDC Trusted Publishing with PEP 740 attestations and no stored token. See
  [docs/runbooks/0019-release-process.md](docs/runbooks/0019-release-process.md).
  (#235)
- **JitterBuffer accessors and SSRC-aware auto-reset** — `__len__`, `peek`, `flush`,
  and `reset` methods on `JitterBuffer`; automatic per-SSRC state reset on SSRC
  change so a re-invited call gets a clean buffer. (#221)
- **Plugin manifest admission knobs** — `HERMES_SIP_MAX_CALLS` and
  `HERMES_SIP_SHUTDOWN_DRAIN_SECS` declared in `plugin.yaml` `optional_env` so the
  config wizard surfaces them. (#222)
- **Manifest platform label and per-env prompt fields** — `label` and `prompt` fields
  on all `requires_env` / `optional_env` entries, aligned with the `hermes config`
  setup-wizard platform injector (ADR-0037). (#228)
- **`resample_frame` preserves `monotonic_ts_ns`** — `resample_frame(PcmFrame)`
  returns a `PcmFrame` with the original monotonic timestamp intact rather than
  silently zeroing it. (#233)
- **DTMF enum-tagged `feed()` result** — `DtmfDetector.feed()` now returns a typed
  `DtmfPress | DtmfNoPress` discriminated union instead of an untagged optional,
  collapsing the `_order` / `_seen` internal state into a single cleaner structure.
  (#232)
- **Provider input validation** — `PcmFrame`, `Transcript`, and `GuardResult`
  constructors validate their fields and reject malformed input; odd-length PCM
  chunks in the TTS send path are realigned before codec processing. (#225)
- **Scheduled supply-chain audit** — `.github/workflows/supply-chain.yml` gains a
  daily cron trigger and `workflow_dispatch` so the advisory audit runs automatically.
  (#224)

### Changed

- ⚠ **Registration enforces `sips:` AOR on TLS/WSS transports** — a `sip:` AOR
  supplied with a TLS or WSS transport now **raises `ConfigurationError` at config
  load** (it previously silently accepted a potentially downgrade-vulnerable AOR).
  The default AOR scheme is `sips:`. Deployers using an explicit `sip:` AOR on a
  TLS/WSS transport must update their configuration to use `sips:` before upgrading.
  Digest `nc`/`stale`/`qop` contract constraints are also pinned and tested.
  (#230)
- **SDP answerer-preference codec ordering** — the SDP answerer now preserves the
  peer's codec order on received 2xx answers, while still applying local preference
  when generating offers. (#234)

### Fixed

- **Non-audio re-INVITE returns 488** — a re-INVITE carrying a non-audio SDP (e.g.
  video-only) is now correctly answered 488 Not Acceptable Here instead of being
  misclassified as an offerless re-INVITE. (#229)
- **Transport skips malformed SIP message** — a message that cannot be parsed (bad
  framing, missing mandatory headers, etc.) is now skipped and logged; the transport
  connection and all active calls continue rather than the connection being dropped.
  (#231)
- **Intercom control-character rejection** — control characters in intercom
  configuration are rejected at config load; wrapped relay/webhook errors are
  sanitised before surfacing. (#226)
- **Docs reconciled** — stale `IMPLEMENTATION-PLAN` / `MULTIREG` plan documents
  updated; outbound CANCEL runbook coverage added. (#227)
- **DTMF mutation-hardening vectors** — encode() end-bit and volume edge cases, and
  bounded-window eviction, are now covered by deterministic mutation tests. (#223)
- **`Record-Route` header comma-splitting** — comma-combined `Record-Route` headers
  are now split per RFC 3261 §7.3.1 so multi-hop dialog routing works correctly.
  (#218)
- **Content-Length line-folding** — line-folded `Content-Length` values in the SIP
  stream framer are now unfolded before parsing. (#216)
- **RTP padding rejection** — malformed RTP padding is rejected; jitter-buffer depth
  semantics are pinned by tests. (#215)
- **Digest nc range + control-char validation** — `nc` is validated within the
  32-bit range and per-field control-character rejection is enforced. (#214)

## [0.1.0] - 2026-06-23

First tagged release of the `hermes-voip` Hermes plugin: two-way voice over
telephony for a Hermes agent on any RFC-compliant SIP-over-TLS or WebRTC voice
gateway.

### Added

- **SIP-over-TLS registration** as one or many extensions, with digest
  authentication across SHA-256, MD5, and the `-sess` variants (RFC 7616 / 8760)
  and automatic re-authentication on challenge.
- **Inbound calls** — answer incoming `INVITE`s with full SDP offer/answer
  (RFC 3261 / 3550 / 4566).
- **Outbound calls** — the agent places calls itself via the UAC originate flow
  (the `place_call` tool), with a per-call objective brief and asynchronous,
  cross-session result reporting (`report_call_result`).
- **Media security** — SDES-SRTP on the SIP-over-TLS path and DTLS-SRTP on the
  WebRTC path (RFC 3711); the SDES answer selects the strongest offered crypto
  suite for downgrade resistance.
- **RTP with media-quality resilience** — adaptive jitter buffer, packet-loss
  concealment, and stateful-codec concealment.
- **RTCP** — sender/receiver reports, SDES, and BYE, with reception statistics
  and `rtcp-mux`.
- **Best-available audio codecs, negotiated per call** — G.722 wideband first
  with G.711 fallback on the SIP path, and Opus on the WebRTC path.
- **DTMF** — RFC 4733 telephone-event as primary, SIP INFO as fallback, and
  in-band tone detection as last resort.
- **Two-way conversational bridge** — streaming speech-to-text → the Hermes agent
  → sentence-streamed text-to-speech. Each reply is delivered as one complete
  string and sentence-streamed into audio for fast first-audio; spoken output is
  cleaned up for the phone.
- **Conversational providers** — local, offline-by-default speech-to-text
  (sherpa-onnx) and text-to-speech (sherpa-onnx Kokoro-82M), with optional cloud
  providers (Deepgram speech-to-text, ElevenLabs text-to-speech, including the v3
  expressive tier and model-conditional audio tags) selectable by configuration.
- **Barge-in** — in-process acoustic echo cancellation (NLMS) and voice-activity
  endpointing let a caller interrupt the agent mid-sentence.
- **Offline prompt-injection guard** — an on-device model screens every caller
  utterance before it reaches the agent.
- **Caller recognition** — caller modes (allow / deny / grey classification) and
  caller groups (named trust tiers with per-group privilege, persona, and tool
  allowance).
- **In-call control and transfer** — hold/resume, blind transfer, and attended
  (consultative) transfer via REFER + Replaces.
- **Intercom mode + in-call DTMF actuation** — screen a door/gate visitor and
  buzz them in, locked down to only that action (`open_entry`, `send_dtmf`).
- **Call-progress detection** — fax-tone (CNG/CED) and answering-machine
  detection, with a leave-message protocol.
- **RFC 4028 session timers** — `Session-Expires` / `Min-SE` keep-alive refresh.
- **Production-safety lifecycle** — failure BYE, connection draining, an
  admission cap (concurrent-call limit), graceful shutdown, and log redaction.
- **Conversational UX** — an instant greeting on connect, dead-air comfort
  fillers, a caller-silence reprompt, and a spoken goodbye.
- **Automatic resilience** — registration reconnect, a watchdog that cleanly ends
  a silently-dropped call, and automatic text-to-speech failover from a cloud
  voice to the local voice mid-call.
- **The Hermes tool surface** — registers the `voip` platform plus **10 tools and
  1 hook**: `hang_up`, `hold_call`, `resume_call`, `list_registrations`,
  `place_call`, `report_call_result`, `send_dtmf`, `open_entry`, `transfer_blind`,
  and `transfer_attended`, all governed by a per-call `pre_tool_call`
  privilege-clamp hook. Bundled call-scenario skills ship as importable package
  data.
- **WebRTC support (experimental — live ICE not yet validated).** A WebRTC client
  is a first-class inbound caller with DTLS-SRTP media, ICE connectivity, and
  Opus audio (needs the `webrtc` extra + `libopus`); SIP-over-Secure-WebSocket
  signalling, outbound WebRTC origination over WSS, and a pre-encoded outbound
  WebRTC video stream are wired. These paths still want full live validation
  against a real gateway and client.
- **Apache-2.0 licensed**, with third-party attribution in
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and a [`NOTICE`](NOTICE) file.
- Release-process runbook
  ([docs/runbooks/0019-release-process.md](docs/runbooks/0019-release-process.md)):
  the exact, verified steps to cut a release — bump the version, run the
  version-sync tests, update this changelog, tag, `uv build`, and verify the wheel
  installs and ships the plugin manifest.
- This `CHANGELOG.md`.

### Changed

- Version is now single-sourced. `hermes_voip.__version__` derives from the
  installed distribution metadata (`importlib.metadata.version("hermes-voip")`,
  populated from `pyproject.toml [project].version`) instead of a hand-maintained
  literal. The test suite pins `pyproject.toml`, `__version__`, and the
  `plugin.yaml` manifest version equal, so a release is a single edit in
  `pyproject.toml`.

[Unreleased]: https://github.com/troykelly/hermes-voip/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/troykelly/hermes-voip/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/troykelly/hermes-voip/releases/tag/v0.1.0
