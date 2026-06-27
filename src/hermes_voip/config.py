"""Parse the ``HERMES_SIP_*`` environment scheme into a typed gateway config.

The plugin registers one or more extensions on a single SIP-over-TLS / WebRTC
gateway (ADR-0011). Connection details live only in the gitignored ``.env`` and
are read by the runtime into a mapping; this module is the **pure** parser that
turns that mapping into a validated :class:`GatewayConfig` plus a tuple of
per-extension :class:`ExtensionConfig`. It reads no process environment itself —
callers pass ``os.environ`` (or ``PlatformConfig.extra``) explicitly — so the
parse is deterministic and unit-testable against fakes.

Two extension schemes are supported, and they MUST NOT be mixed:

* **Single (back-compatible):** ``HERMES_SIP_EXTENSION`` + ``HERMES_SIP_PASSWORD``
  (optional ``HERMES_SIP_USERNAME``). This is index ``0``.
* **Indexed (multiple registrations):** ``HERMES_SIP_EXTENSION_<n>`` +
  ``HERMES_SIP_PASSWORD_<n>`` (optional ``HERMES_SIP_USERNAME_<n>``) for each
  non-negative integer ``<n>``.

Shared gateway settings: ``HERMES_SIP_HOST`` (required), ``HERMES_SIP_PORT``,
``HERMES_SIP_TRANSPORT`` (``tls`` | ``wss``), ``HERMES_SIP_EXPIRES``,
``HERMES_SIP_USER_AGENT``, and ``HERMES_SIP_DEFAULT_EXTENSION`` (the inbound
fallback registration; defaults to the lowest-index extension).

A :class:`GatewayConfig` carries everything env can supply; the transport-derived
``Contact`` and Via ``sent-by`` are not knowable until the socket is up, so
:meth:`GatewayConfig.registration_config` completes a per-extension
:class:`~hermes_voip.registration.RegistrationConfig` from those live inputs.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from hermes_voip.registration import RegistrationConfig, ViaTransport

#: Deny enforcement style for a declined-group caller (ADR-0020 §5/§6). ``reject``
#: sends a hard ``603 Decline`` (Phase 1); ``decline`` answers + speaks one TTS line
#: + BYEs (Phase 2). The runtime counterpart is ``_DENY_MODES``, kept in sync by hand.
type DenyMode = Literal["reject", "decline"]

__all__ = [
    "DEFAULT_GREETING",
    "DEFAULT_ICE_STUN_URLS",
    "ConfigError",
    "DenyMode",
    "ExtensionConfig",
    "GatewayConfig",
    "MediaConfig",
    "load_gateway_config",
    "load_media_config",
    "parse_keepalive_interval",
]

#: The opening line the agent speaks the instant an inbound call is answered,
#: unless ``HERMES_VOIP_GREETING`` overrides it (ADR-0002 §"NAT / symmetric-RTP
#: latching"). Speaking on answer makes the plugin send RTP first, which both
#: lets the caller hear something immediately and gives a symmetric-RTP gateway
#: behind NAT a source tuple to latch onto so the return media path opens. An
#: explicitly-empty override disables the greeting entirely.
DEFAULT_GREETING = (
    "Hello, you're through to the Hermes voice assistant. How can I help?"
)

# Scheme tokens accepted for HERMES_SIP_TRANSPORT, mapped to their Via transport
# tokens (RFC 3261 §7.1 / RFC 7118). Only the two sanctioned transports.
_VIA_TRANSPORT: dict[str, ViaTransport] = {"tls": "TLS", "wss": "WSS"}
_DEFAULT_PORT: dict[str, str] = {"tls": "5061", "wss": "443"}

_DEFAULT_TRANSPORT = "tls"
_DEFAULT_EXPIRES = 300
_DEFAULT_USER_AGENT = "hermes-voip/0"

# WebSocket signalling (SIP-over-WSS, RFC 7118 / ADR-0016 §6 / ADR-0038). Only
# meaningful when HERMES_SIP_TRANSPORT=wss; harmlessly defaulted otherwise.
_DEFAULT_WS_PATH = "/ws"

_MIN_PORT = 1
_MAX_PORT = 65535

_HOST_KEY = "HERMES_SIP_HOST"
_PORT_KEY = "HERMES_SIP_PORT"
# Provisioning aliases (launch-blocker fix; runbook 0001). The 1Password-provisioned
# .env emits HERMES_SIP_SERVER_HOST / HERMES_SIP_TLS_PORT, but the canonical keys this
# parser reads are HERMES_SIP_HOST / HERMES_SIP_PORT. Accept the provisioner names so a
# first live launch from the sanctioned secret registers instead of failing
# "HERMES_SIP_HOST is required". For the HOST the canonical key wins when both are set
# (_require_host). The TLS PORT is transport-aware: HERMES_SIP_TLS_PORT is the SIP-TLS
# port and on the tls transport it takes PRECEDENCE over HERMES_SIP_PORT (which a
# provisioner sets to the cleartext 5060) — see _parse_port — because the TLS handshake
# must target the TLS port. On wss the TLS alias is not consulted.
_SERVER_HOST_KEY = "HERMES_SIP_SERVER_HOST"
_TLS_PORT_KEY = "HERMES_SIP_TLS_PORT"
_TRANSPORT_KEY = "HERMES_SIP_TRANSPORT"
_EXPIRES_KEY = "HERMES_SIP_EXPIRES"
_USER_AGENT_KEY = "HERMES_SIP_USER_AGENT"
_DEFAULT_EXTENSION_KEY = "HERMES_SIP_DEFAULT_EXTENSION"
_WS_PATH_KEY = "HERMES_SIP_WS_PATH"
# S105 noqa: this is the env-var NAME, not a password value — the SEPARATE WSS
# digest credential is read from this key at runtime (ADR-0038 §3), never hardcoded.
_WS_PASSWORD_KEY = "HERMES_SIP_WS_PASSWORD"  # noqa: S105

# Admission-control + graceful-shutdown drain (ADR-0059). Both are gateway-level
# SIP-lifecycle knobs, parsed from the HERMES_SIP_* scheme. ``MAX_CALLS`` caps the
# number of concurrent active calls (a new inbound INVITE at capacity is rejected
# 486 Busy Here before any per-call media/pipeline is built — protecting a 24/7
# line from a burst/flood OOM). ``SHUTDOWN_DRAIN_SECS`` bounds how long ``disconnect``
# waits for in-flight calls to BYE-drain before forcing teardown on shutdown.
_MAX_CALLS_KEY = "HERMES_SIP_MAX_CALLS"
_DEFAULT_MAX_CALLS = 8
_SHUTDOWN_DRAIN_SECS_KEY = "HERMES_SIP_SHUTDOWN_DRAIN_SECS"
_DEFAULT_SHUTDOWN_DRAIN_SECS = 5.0
# Deny enforcement style for a declined-group caller (ADR-0020 §5/§6). ``reject``
# (the default, Phase 1) sends a hard ``603 Decline`` in the pre-200-OK window —
# cheapest, gives the caller no agent surface. ``decline`` (Phase 2) instead ANSWERS
# the call (200 OK), speaks one short TTS line (``decline_phrase``), then BYEs — a
# polite decline that trains a spammer less than a hard 603. The mode lives on the
# gateway scheme (``HERMES_VOIP_DENY_MODE``) because it governs the SIP-level
# inbound-call disposition; the phrase it speaks lives on MediaConfig with the other
# spoken-UX lines (greeting/goodbye).
_DENY_MODE_KEY = "HERMES_VOIP_DENY_MODE"
_DENY_MODES = frozenset({"reject", "decline"})
_DEFAULT_DENY_MODE: DenyMode = "reject"

_BARE_EXTENSION = "HERMES_SIP_EXTENSION"
_BARE_PASSWORD = "HERMES_SIP_PASSWORD"  # noqa: S105 — env var name, not a secret
_BARE_USERNAME = "HERMES_SIP_USERNAME"

_EXTENSION_PREFIX = "HERMES_SIP_EXTENSION_"
_PASSWORD_PREFIX = "HERMES_SIP_PASSWORD_"  # noqa: S105 — env var name, not a secret
_USERNAME_PREFIX = "HERMES_SIP_USERNAME_"

_INDEX_RE = re.compile(r"[0-9]+")

# --- media / provider / feature scheme (ADR-0006..0010) ---------------------
#
# A second env scheme, parsed independently of the gateway/extension scheme
# above. Selection is config-only: every key has a safe default so a bare
# install runs the fully-offline self-host path (sherpa-onnx STT, Kokoro TTS,
# in-process ONNX injection guard). Cloud provider API keys are read *by
# reference only* and never logged (their dataclass fields are repr-suppressed).

# STT (ADR-0006 §"Configuration surface").
_STT_PROVIDER_KEY = "HERMES_VOIP_STT_PROVIDER"
_STT_MODEL_DIR_KEY = "HERMES_VOIP_STT_MODEL_DIR"
_DEFAULT_STT_PROVIDER = "sherpa-onnx"
_STT_PROVIDERS = frozenset({"sherpa-onnx", "deepgram"})

# TTS (ADR-0007 §"Configuration surface").
_TTS_PROVIDER_KEY = "HERMES_VOIP_TTS_PROVIDER"
_TTS_MODEL_KEY = "HERMES_VOIP_TTS_MODEL"
_TTS_VOICE_KEY = "HERMES_VOIP_TTS_VOICE"
_DEFAULT_TTS_PROVIDER = "sherpa-kokoro"
_TTS_PROVIDERS = frozenset(
    {"sherpa-kokoro", "piper", "kittentts", "kyutai", "cartesia", "aura2", "elevenlabs"}
)

# Automatic TTS failover (ADR-0025). When the primary TTS raises during synthesis
# (HTTP 400 like the live incident, a timeout, a connection error, or any exception
# from the stream), the system falls back to a self-host synthesiser so the call
# still gets audio instead of dropping silent. ``HERMES_VOIP_TTS_FALLBACK`` selects
# the fallback provider token; ``none`` (or empty) disables failover. The DEFAULT
# follows the primary: a CLOUD primary (which can fail transiently or 400) gets the
# self-host ``sherpa-kokoro`` fallback; a self-host primary is already the safe local
# path, so it defaults to no fallback. ``MediaConfig.tts_fallback`` is the resolved
# token (or ``None`` when failover is off). The fallback must be a known TTS provider
# and must differ from the primary (a same-provider fallback is useless).
_TTS_FALLBACK_KEY = "HERMES_VOIP_TTS_FALLBACK"
# The fallback's OWN model directory (sherpa-kokoro). The shared HERMES_VOIP_TTS_MODEL
# is the ElevenLabs model id for a cloud primary, NOT a Kokoro directory, so the Kokoro
# fallback needs its own dir to be loadable on demand. Required (fail loud at startup)
# when the fallback is sherpa-kokoro, so a primary failure never finds an unbuildable
# fallback and dies silent.
_TTS_FALLBACK_MODEL_KEY = "HERMES_VOIP_TTS_FALLBACK_MODEL"
_TTS_FALLBACK_NONE = "none"
# Cloud TTS providers that benefit from a self-host fallback by default (they reach
# a remote API that can 400 / time out / drop). A self-host primary has none.
_CLOUD_TTS_PROVIDERS = frozenset({"elevenlabs", "cartesia", "aura2"})
_DEFAULT_CLOUD_TTS_FALLBACK = "sherpa-kokoro"
# Fallback providers that need a model directory (their factory reads tts_model as a
# path): a configured fallback of one of these requires HERMES_VOIP_TTS_FALLBACK_MODEL.
_MODEL_DIR_TTS_PROVIDERS = frozenset({"sherpa-kokoro", "piper", "kittentts", "kyutai"})

# ElevenLabs dynamic-voice tuning (ADR-0007 amendment, 2026-06-17). Optional knobs
# that let the operator A/B voice dynamism on live calls WITHOUT a code change:
# they map onto the ElevenLabs request's ``voice_settings`` object + the
# ``optimize_streaming_latency`` query param. Each is provider-agnostic at this
# layer — unset (``None``) means "the ElevenLabs provider applies its dynamic
# default" (a lower-than-flat stability), so the default install is already livelier
# than ElevenLabs' monotone 0.5; a self-host provider simply ignores them.
_TTS_STABILITY_KEY = "HERMES_VOIP_TTS_STABILITY"
_TTS_STYLE_KEY = "HERMES_VOIP_TTS_STYLE"
_TTS_SIMILARITY_KEY = "HERMES_VOIP_TTS_SIMILARITY"
_TTS_SPEAKER_BOOST_KEY = "HERMES_VOIP_TTS_SPEAKER_BOOST"
_TTS_STREAMING_LATENCY_KEY = "HERMES_VOIP_TTS_STREAMING_LATENCY"
# The ElevenLabs voice_settings floats are 0.0-1.0; optimize_streaming_latency is
# an int in [0, 4] (0 = none ... 4 = max, text-normaliser off).
_MIN_TTS_SETTING = 0.0
_MAX_TTS_SETTING = 1.0
_MIN_TTS_STREAMING_LATENCY = 0
_MAX_TTS_STREAMING_LATENCY = 4

# Cloud credentials, consumed by the cloud providers when selected. These are
# the env-var *names* (not secrets); the values are read by reference only and
# never logged (see MediaConfig repr-suppressed fields).
_ELEVENLABS_API_KEY = "ELEVENLABS_API_KEY"
_DEEPGRAM_API_KEY = "DEEPGRAM_API_KEY"
_CARTESIA_API_KEY = "HERMES_VOIP_CARTESIA_API_KEY"
# A selected cloud provider must have its key set (fail-fast, ADR-0006/0007).
_STT_REQUIRED_KEY = {"deepgram": _DEEPGRAM_API_KEY}
_TTS_REQUIRED_KEY = {
    "elevenlabs": _ELEVENLABS_API_KEY,
    "cartesia": _CARTESIA_API_KEY,
    "aura2": _DEEPGRAM_API_KEY,
}

# VAD / endpointing / duplex (ADR-0008). Full-duplex barge-in is a deferred
# Phase-2 design; the enum still accepts the token so config can opt in once the
# capability lands, but the default is the shipped Phase-1 half-duplex path.
_VAD_THRESHOLD_KEY = "HERMES_VOIP_VAD_THRESHOLD"
_ENDPOINT_SILENCE_MS_KEY = "HERMES_VOIP_ENDPOINT_SILENCE_MS"
_DUPLEX_MODE_KEY = "HERMES_VOIP_DUPLEX_MODE"
_DEFAULT_VAD_THRESHOLD = 0.5
_DEFAULT_ENDPOINT_SILENCE_MS = 500
_DEFAULT_DUPLEX_MODE = "half"
_DUPLEX_MODES = frozenset({"half", "full"})
_MIN_VAD_THRESHOLD = 0.0
_MAX_VAD_THRESHOLD = 1.0

# Opening greeting spoken on inbound-call answer (ADR-0002 NAT-latch). Absent →
# the friendly DEFAULT_GREETING; present-but-empty (or whitespace) → no greeting.
_GREETING_KEY = "HERMES_VOIP_GREETING"

# Echo-robust barge-in (ADR-0023). The gateway can reflect the agent's own TTS
# back on the inbound path (no echo cancellation), and the VAD/ASR transcribe it
# as the caller — a single ONSET then barged the agent in, ending its own turn (a
# self-interruption loop). Mode `gated` (default) requires a SUSTAINED voiced run
# while the agent's TTS plays (and for a short tail after) before a barge-in
# counts, so short echo blips never interrupt but a genuine interruption still
# does. `full` is the legacy immediate barge-in (for echo-cancelled gateways);
# `off` disables barge-in entirely.
_BARGE_IN_MODE_KEY = "HERMES_VOIP_BARGE_IN_MODE"
_BARGE_IN_MIN_SPEECH_MS_KEY = "HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS"
_BARGE_IN_TAIL_MS_KEY = "HERMES_VOIP_BARGE_IN_TAIL_MS"
# Clean-stop fade (ADR-0028): a short linear fade-out on the final outbound frames
# when a barge-in flushes the agent's queued audio, so the cut is click-free. 30 ms
# is long enough to remove the click without an audible lingering tail; 0 disables
# it (instant hard cut, for an operator who prefers the abrupt stop).
_BARGE_IN_FADE_MS_KEY = "HERMES_VOIP_BARGE_IN_FADE_MS"
_DEFAULT_BARGE_IN_FADE_MS = 30

# In-process acoustic echo cancellation (ADR-0033). The gateway reflects the agent's
# own TTS back on the inbound leg; the canceller subtracts the KNOWN outbound
# reference from each inbound frame before the VAD/ASR see it, so the reflected echo
# cannot false-trigger barge-in — which lets the barge-in sustained threshold drop
# (aggressive barge-in) without re-opening ADR-0023's self-interruption loop. On by
# default; `false` reverts to the sustained-gate-only behaviour. The filter is a
# pure-stdlib NLMS adaptive filter (no new dependency) running at the analysis rate.
_AEC_ENABLED_KEY = "HERMES_VOIP_AEC_ENABLED"
_AEC_FILTER_MS_KEY = "HERMES_VOIP_AEC_FILTER_MS"
_AEC_BULK_DELAY_MS_KEY = "HERMES_VOIP_AEC_BULK_DELAY_MS"
_AEC_MU_KEY = "HERMES_VOIP_AEC_MU"
_DEFAULT_AEC_ENABLED = True
# 64 ms of adaptive taps so the window spans the realistic echo-RETURN delay, not
# just the impulse response: a broadband (speech) echo delayed by the round-trip
# (our ~2-packet jitter buffer + gateway processing ≈ tens of ms) is ONLY cancelled
# if the filter reaches that far back (verified — a 16 ms filter leaves a 40 ms-
# delayed broadband echo essentially uncancelled). At 8 kHz this is 512 taps ≈
# 6.9 ms/frame (34% of the 20 ms ptime). At 16 kHz the engine CAPS the tap count
# (_AEC_MAX_TAPS) to stay within the per-frame budget, so 16 kHz covers ~32 ms of
# delay; a longer 16 kHz echo needs HERMES_VOIP_AEC_BULK_DELAY_MS tuning.
_DEFAULT_AEC_FILTER_MS = 64
# No fixed bulk delay by default: the adaptive taps cover the echo-return delay.
_DEFAULT_AEC_BULK_DELAY_MS = 0
# NLMS step size in (0, 2): 0.30 converges briskly with a low steady-state residual.
_DEFAULT_AEC_MU = 0.30
_MIN_AEC_MU = 0.0
_MAX_AEC_MU = 2.0
# The barge-in sustained threshold (ms) when AEC is ON: with the echo cancelled the
# 600 ms echo-safety margin is unnecessary, so the threshold drops to a responsive
# 200 ms (≈ 7 VAD windows) — still long enough that a single spurious VAD blip does
# not barge in. Applied only when HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS is unset; an
# explicit value always wins. AEC off → the 600 ms default (ADR-0023) is restored.
_DEFAULT_BARGE_IN_MIN_SPEECH_MS_AEC = 200

# Dead-air comfort filler (ADR-0030, extended ADR-0054). On a slow turn there is a gap
# of pure silence between the caller finishing and the agent's first audio (LLM think
# time + TTS first-audio latency). On a phone call that reads as a dropped line. When
# enabled, the call loop emits a short, natural human filler ("One moment please.",
# "Bear with me.") on the gap once it exceeds the delay, then RE-EMITS a fresh random
# phrase every repeat interval until the reply audio starts, so a long wait does not
# leave a long silence — cancelled the instant the reply audio or a barge-in arrives.
# ON by default (ADR-0054); set HERMES_VOIP_TTS_COMFORT_FILLER=false for exactly the
# pre-filler behaviour (no filler task created). The filler routes through the normal
# speak()/TTS path so it is flushable (ADR-0028) and model-conditional-tag-aware
# (ADR-0027).
_COMFORT_FILLER_KEY = "HERMES_VOIP_TTS_COMFORT_FILLER"
_COMFORT_FILLER_DELAY_MS_KEY = "HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS"
_COMFORT_FILLER_REPEAT_MS_KEY = "HERMES_VOIP_TTS_COMFORT_FILLER_REPEAT_MS"
_COMFORT_FILLER_PHRASES_KEY = "HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES"
# ON by default (ADR-0054): the operator wants a slow turn to never leave the caller
# in silence. The off path is still exactly the pre-filler behaviour (no filler task
# created) for an operator who sets HERMES_VOIP_TTS_COMFORT_FILLER=false.
_DEFAULT_COMFORT_FILLER = True
# 900 ms ≈ long enough that a brisk reply (a few hundred ms) never triggers the
# filler, short enough that a genuinely slow turn does not leave the caller in
# silence wondering whether the line dropped.
_DEFAULT_COMFORT_FILLER_DELAY_MS = 900
# The PERIODIC repeat interval (ADR-0054): on a sustained dead-air gap a fresh filler
# fires every this-many ms until the reply audio starts, so a single ~1 s phrase does
# not leave a 10 s LLM wait mostly silent. Defaults to the dead-air delay (one cadence
# to reason about); overridable independently. Must be > 0.
_DEFAULT_COMFORT_FILLER_REPEAT_MS = _DEFAULT_COMFORT_FILLER_DELAY_MS
_COMFORT_FILLER_PHRASE_SEP = "|"
# The active conversation language (ADR-0054, ADR-0084), selecting the built-in
# comfort-filler phrase set.  Any well-formed BCP-47 primary subtag is accepted
# (structural validation via _LANGUAGE_RE); languages without a built-in phrase set
# fall back to the English default.  Adding a language is a data-only change here.
_LANGUAGE_KEY = "HERMES_VOIP_LANGUAGE"
_DEFAULT_LANGUAGE = "en"
# Built-in comfort-filler phrase sets, keyed by language code. Each phrase reads
# naturally on EVERY TTS model (no bracket tag), so the default never depends on v3
# tag rendering. An operator running v3 may override with a phrase that includes a tag
# (e.g. "[hesitates] hmm") — it renders on v3 and strips cleanly elsewhere via the
# per-segment strip (ADR-0027). The English set is intentionally varied (random,
# no-immediate-repeat selection wears better with more choices). To add a language,
# add an entry here — nothing else changes.
_COMFORT_FILLER_PHRASES_BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "en": (
        "Just a moment.",
        "One moment please.",
        "Bear with me.",
        "Let me check that for you.",
        "Just a second.",
        "Almost there.",
        "Hold on a moment.",
        "Let me look into that.",
        "Give me just a second.",
        "One moment.",
    ),
}
# The English set is the back-compatible default for a directly-constructed
# MediaConfig / CallLoop (no env, no language argument).
_DEFAULT_COMFORT_FILLER_PHRASES: tuple[str, ...] = _COMFORT_FILLER_PHRASES_BY_LANGUAGE[
    _DEFAULT_LANGUAGE
]
# BCP-47 primary-subtag format: 2-8 ASCII alpha chars, optionally followed by
# hyphen-separated subtags of 1-8 alphanumeric chars each (ADR-0084).  Acceptance is
# structural (well-formed), not a registry lookup — see ADR-0084 for rationale.
_LANGUAGE_RE: re.Pattern[str] = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$")

# Caller-silence reprompt / no-input handling (ADR-0057). When the caller is live
# (RTP flowing) but never speaks, the engine's RTP watchdog never fires (it only
# fires on DEAD media). The no-input watchdog speaks a short reprompt after a silence
# window, and ends the call gracefully (goodbye → clean run() return) after N
# unanswered reprompts so an abandoned/dropped line is noticed promptly. ON by default
# (the operator wants a live-but-silent caller prompted, not left in dead air forever).
_NO_INPUT_REPROMPT_KEY = "HERMES_VOIP_NO_INPUT_REPROMPT"
_DEFAULT_NO_INPUT_REPROMPT = True
# Silence window (ms) of no caller end-of-turn before a reprompt fires. Must be
# strictly positive. 10 s is long enough not to nag a thinking caller, short enough
# that a dropped/abandoned line is noticed promptly.
_NO_INPUT_TIMEOUT_MS_KEY = "HERMES_VOIP_NO_INPUT_TIMEOUT_MS"
_DEFAULT_NO_INPUT_TIMEOUT_MS = 10_000
# Number of unanswered reprompts before the loop ends the call gracefully. 0 = end on
# first silent window with no reprompt; must be non-negative.
_NO_INPUT_MAX_REPROMPTS_KEY = "HERMES_VOIP_NO_INPUT_MAX_REPROMPTS"
_DEFAULT_NO_INPUT_MAX_REPROMPTS = 2
# Pipe-separated reprompt phrase set; blank/empty → the built-in English default.
# Same parse convention as HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES.
_NO_INPUT_REPROMPT_PHRASES_KEY = "HERMES_VOIP_NO_INPUT_REPROMPT_PHRASES"
# The built-in English reprompt set — MUST exactly match
# _DEFAULT_NO_INPUT_REPROMPT_PHRASES in media/call_loop.py so behaviour is
# unchanged when env vars are unset.
_DEFAULT_NO_INPUT_REPROMPT_PHRASES: tuple[str, ...] = (
    "Are you still there?",
    "Hello, are you still there?",
    "Sorry, I can't hear anything. Are you still there?",
)

# Spoken goodbye on a loop-initiated graceful end (ADR-0057). When ON (the default),
# the call loop speaks ``goodbye_phrase`` and flushes it BEFORE run() returns, so the
# caller hears a clean closing line rather than a silent BYE. NOT spoken on a
# caller-hangup / inbound-EOS / error end (no media path there). ON by default;
# HERMES_VOIP_GOODBYE_PHRASE selects the phrase.
_GOODBYE_KEY = "HERMES_VOIP_GOODBYE"
_DEFAULT_GOODBYE = True
# The goodbye phrase — MUST exactly match _DEFAULT_GOODBYE_PHRASE in call_loop.py.
_GOODBYE_PHRASE_KEY = "HERMES_VOIP_GOODBYE_PHRASE"
_DEFAULT_GOODBYE_PHRASE = "Goodbye."

# Polite-decline line spoken on a ``deny_mode=decline`` declined caller (ADR-0020 §5).
# When the gateway's deny_mode is ``decline``, a declined-group caller is ANSWERED and
# hears this one short line before the call is BYE'd (instead of a hard 603). Operator-
# overridable; blank/whitespace → the built-in default. NOT spoken on the default
# ``reject`` mode (no media path there). ``HERMES_VOIP_DECLINE_PHRASE``.
_DECLINE_PHRASE_KEY = "HERMES_VOIP_DECLINE_PHRASE"
_DEFAULT_DECLINE_PHRASE = "Sorry, I cannot take this call."

# Safe-decline line spoken on a guard REFUSE (ADR-0076). When the injection guard
# refuses a turn it is never forwarded to the agent; without a spoken line a caller the
# guard false-positived hears pure dead air, repeats into the same wall, and is hung up
# on. The loop speaks ONE short language-keyed decline line instead. Pipe-separated
# override; blank/empty → the selected language's built-in set (never all-silence — that
# would re-introduce the bug). Each set MUST mirror _DEFAULT_REFUSE_DECLINE_PHRASES in
# media/call_loop.py for the English default so behaviour matches when env is unset. To
# add a language, add an entry here — nothing else changes.
_REFUSE_DECLINE_PHRASES_KEY = "HERMES_VOIP_REFUSE_DECLINE_PHRASES"
_REFUSE_DECLINE_PHRASES_BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "en": (
        "Sorry, I can't help with that. Is there anything else?",
        "I'm not able to do that. Is there something else I can help with?",
        "Sorry, that's something I can't do. How else can I help?",
    ),
}
_DEFAULT_REFUSE_DECLINE_PHRASES: tuple[str, ...] = _REFUSE_DECLINE_PHRASES_BY_LANGUAGE[
    _DEFAULT_LANGUAGE
]

# Operator-overridable apology for provider/runtime errors (ADR-0063).
# Empty string → use per-language built-in from provider_error.py.
_ERROR_APOLOGY_KEY = "HERMES_VOIP_ERROR_APOLOGY"

_DEFAULT_BARGE_IN_MODE = "gated"
_BARGE_IN_MODES = frozenset({"off", "gated", "full"})
# 600 ms ≈ 19 VAD windows at 8 kHz — above the longest observed gateway-echo
# burst (~15 windows ≈ 480 ms in the live log), with margin, so echo never
# reaches the sustained-barge-in threshold while a real interruption (which
# sustains well beyond 600 ms) still does.
_DEFAULT_BARGE_IN_MIN_SPEECH_MS = 600
_DEFAULT_BARGE_IN_TAIL_MS = 250

# Symmetric-RTP (comedia) latching for NAT traversal (ADR-0005 §NAT). When on
# (the default) the media engine latches its outbound destination onto the peer's
# real RTP source — the source tuple of the first valid inbound RTP packet —
# instead of trusting the SDP c=/m= address (which under NAT may be a private or
# SBC-rewritten address the peer's media never comes from). Set false to always
# honour the SDP address (for gateways that route RTP by the negotiated address).
_RTP_SYMMETRIC_KEY = "HERMES_VOIP_RTP_SYMMETRIC"
_DEFAULT_RTP_SYMMETRIC = True

# RTCP (RFC 3550 §6 / RFC 5761) — the control channel that reports loss/jitter/RTT
# and names our source (SDES CNAME). Activated by the adapter on the CLEARTEXT
# plain-RTP path (ADR-0061); a secured SDES/SAVP session is not activated (the engine
# has no SRTCP transform — RFC 3711 §3.4). On by default (a gateway may gate on RTCP
# presence and an absent RTCP can be read as a dead media path); set false as an
# operator kill-switch to suppress all RTCP send/receive.
_RTCP_ENABLED_KEY = "HERMES_VOIP_RTCP_ENABLED"
_DEFAULT_RTCP_ENABLED = True

# Secured-path RTCP over SRTCP (RFC 3711 §3.4, ADR-0066) — gated OFF by default.
# When a secured (SDES RTP/SAVP) inbound call has no a=rtcp-mux, activating RTCP
# opens a sibling SRTCP socket on RTP-port+1 and emits SRTCP on the wire. A live
# Grandstream UCM that did NOT negotiate rtcp-mux MUTED the media session on that
# unexpected SRTCP (no two-way audio). So secured-path RTCP is OPT-IN: by default a
# secured call stays RTCP-dormant (the pre-#160 behaviour, audio works), and the
# SRTCP capability is retained behind this flag for a gateway-validated rollout. The
# master _RTCP_ENABLED_KEY kill-switch still applies on top of this.
_SECURED_RTCP_ENABLED_KEY = "HERMES_VOIP_SECURED_RTCP_ENABLED"
_DEFAULT_SECURED_RTCP_ENABLED = False

# Call-progress detection (fax CNG/CED + answering-machine detection), ADR-0064.
# The whole feature is OFF by default — the conversational pipeline assumes a human
# caller, and turning the detector on adds per-frame Goertzel work and surfaces
# extra system turns; an operator opts in. When on, fax detection runs both
# directions and AMD runs only on outbound calls (and only when `enable_amd`).
_CALL_PROGRESS_KEY = "HERMES_VOIP_CALL_PROGRESS"
_DEFAULT_CALL_PROGRESS = False
# Answering-machine detection — a sub-switch of call-progress, OFF by default. Only
# meaningful on OUTBOUND calls (on inbound the agent is the answerer); off keeps fax
# detection live while AMD/record-cue stay silent.
_AMD_KEY = "HERMES_VOIP_AMD"
_DEFAULT_AMD = False
# When a fax tone is detected, auto hang up rather than leave the line open for a
# conversation that cannot happen. ON by default (a detected fax never converses);
# an operator who wants the agent to decide can turn it off.
_AMD_HANGUP_ON_FAX_KEY = "HERMES_VOIP_AMD_HANGUP_ON_FAX"
_DEFAULT_AMD_HANGUP_ON_FAX = True

# RTP-inactivity watchdog (ADR-0026). A silent media/network drop otherwise hangs
# the call forever (the inbound generator blocks on the recv queue with nothing
# ever setting the stop event). When no inbound RTP arrives within this window the
# media engine ends the call as a MEDIA_TIMEOUT (→ a /stop hard stop to Hermes).
# Operator-configurable in [1, 300] s; the 300 s cap bounds how long a wedged call
# can persist. Default 20 s — long enough to ride out a brief network hiccup or a
# held call's silence, short enough that a real drop is cleaned up promptly.
_RTP_TIMEOUT_SECS_KEY = "HERMES_VOIP_RTP_TIMEOUT_SECS"
_DEFAULT_RTP_TIMEOUT_SECS = 20
_MIN_RTP_TIMEOUT_SECS = 1
_MAX_RTP_TIMEOUT_SECS = 300

# RFC 4028 session timers (ADR-0071). ``session_expires`` is the interval (seconds)
# we OFFER outbound and INSERT into an inbound 2xx (the refresher then refreshes at
# SE/2; the non-refresher BYEs near expiry if no refresh arrives), so a dead dialog
# is detected and torn down instead of lingering. ``min_se`` is the smallest interval
# we ACCEPT inbound — an INVITE offering a Session-Expires below it is rejected
# ``422 Session Interval Too Small`` carrying our Min-SE (the UAC then retries
# larger). RFC 4028 §4/§5 floor BOTH at 90 s; the parser/post-init enforce
# ``min_se >= 90`` and ``session_expires >= min_se`` (a sub-floor or below-minimum
# session interval is a misconfiguration surfaced at load, never silently accepted).
# Default 600 s session interval is a conservative liveness window for a voice call.
_SESSION_EXPIRES_KEY = "HERMES_VOIP_SESSION_EXPIRES"
_MIN_SE_KEY = "HERMES_VOIP_MIN_SE"
_DEFAULT_SESSION_EXPIRES = 600
_DEFAULT_MIN_SE = 90
# RFC 4028 §4/§5 absolute minimum for Session-Expires / Min-SE.
_RFC4028_MIN_SE_FLOOR = 90

# Adaptive jitter-buffer ceiling (ADR-0056 activated by ADR-0063). The media
# engine's RX JitterBuffer runs in ADAPTIVE mode: its reorder tolerance grows
# under loss/wide-reorder up to this many packets and shrinks back after a clean
# run (floor = the engine's small fixed target_depth). 10 packets ≈ 200 ms at the
# standard 20 ms ptime — the upper bound on added latency the buffer may trade for
# loss resilience. Must be positive (validated). Defaults to a sane value so a
# bare install gets adaptive jitter without tuning.
_JITTER_MAX_DEPTH_KEY = "HERMES_VOIP_JITTER_MAX_DEPTH"
_DEFAULT_JITTER_MAX_DEPTH = 10
# The adaptive ceiling is also the buffer's FLOOR's upper companion: the engine
# builds the adaptive JitterBuffer with its fixed jitter_depth (2) as the floor, and
# rtp.JitterBuffer requires max_depth >= target_depth (floor). A ceiling below the
# floor would raise at engine construction (crashing the call), so the minimum valid
# ceiling is the floor itself — validated here, not deferred to a runtime crash.
_MIN_JITTER_MAX_DEPTH = 2

# RFC 5626 double-CRLF keepalive (SIP-over-TLS path). The interval (seconds) between
# the double-CRLF pings that keep the TLS connection alive through NAT/firewall
# bindings. Must be strictly positive and finite — a zero/negative value disables the
# keepalive entirely (NAT bindings expire; mid-call silence = dropped connection), and
# NaN/inf slip past a naive ``> 0`` test. Validated at connect() via
# parse_keepalive_interval so a misconfiguration surfaces at startup, not mid-call
# (rule 37). Has no effect on the WSS/WebRTC path.
_KEEPALIVE_INTERVAL_KEY = "HERMES_VOIP_KEEPALIVE_INTERVAL"
_DEFAULT_KEEPALIVE_INTERVAL: float = 30.0

# WebRTC ICE STUN servers (ADR-0032/0016, default revised ADR-0043). A comma-separated
# list of ``stun:`` URLs used to gather server-reflexive (srflx) ICE candidates for the
# WebRTC media path. UNSET ⇒ the default public list below; an explicit EMPTY value ⇒
# host-only ICE. Has no effect on the SIP-over-TLS path. Each member is trimmed; blank
# members are dropped.
_ICE_STUN_URLS_KEY = "HERMES_VOIP_ICE_STUN_URLS"
# Default public STUN servers (operator-directed 2026-06-18, ADR-0043). Both are free,
# no-auth, widely used, and dual-stack (they publish AAAA records), so a NAT'd
# deployment gathers a server-reflexive candidate — including an IPv6 srflx on an
# IPv6-capable host — out of the box. Overridable via HERMES_VOIP_ICE_STUN_URLS; an
# explicit empty value disables STUN. Not a paid/SaaS dependency (rule 36): public
# STUN is a stateless reflexive-address echo, used only until the operator sets theirs.
DEFAULT_ICE_STUN_URLS: tuple[str, ...] = (
    "stun:stun.l.google.com:19302",
    "stun:stun.cloudflare.com:3478",
)
# IPv6-first ICE (ADR-0043): gather IPv6 candidates (and prefer them in the answer),
# with IPv4 kept as the fallback family. Both default ON and are independently
# overridable so an operator can run IPv6-only or IPv4-only.
_ICE_USE_IPV4_KEY = "HERMES_VOIP_ICE_USE_IPV4"
_ICE_USE_IPV6_KEY = "HERMES_VOIP_ICE_USE_IPV6"

# WebRTC outbound video (ADR-0044). A pre-encoded H.264 Annex-B file the operator
# supplies; the plugin packetises it (RFC 6184) and loops it over the BUNDLE'd
# DTLS-SRTP video stream. UNSET ⇒ the WebRTC video answer is a=inactive (no
# outbound video). There is NO in-process encoder (the named bindings do not exist
# and the system-library route corrupts the heap — ADR-0044). The path is a local
# file path, not a secret; it never appears in a tracked file. Has no effect on the
# SIP-over-TLS path (WebRTC video only in this lane).
_VIDEO_SOURCE_PATH_KEY = "HERMES_VOIP_VIDEO_SOURCE_PATH"
# The source's frame rate; the 90 kHz RTP timestamp advances 90000//fps per frame.
_VIDEO_FPS_KEY = "HERMES_VOIP_VIDEO_FPS"
_DEFAULT_VIDEO_FPS = 10
_MIN_VIDEO_FPS = 1
_MAX_VIDEO_FPS = 60

# WebRTC DTLS answerer role (ADR-0050, RFC 8842 §5.3). For an ``a=setup:actpass``
# offer the answerer is free to pick its DTLS role; ``auto`` (the default) makes us
# ``active`` (the DTLS client, sending the ClientHello) per RFC 8842 — many gateways
# offer ``actpass`` yet act as the DTLS server, so a ``passive`` answer deadlocks.
# ``passive`` forces the server role for a gateway that insists on being the client;
# ``active`` is the explicit form of the default. A pinned ``active``/``passive``
# offer always overrides this knob (it cannot create two clients/servers). No effect
# on the SIP-over-TLS path.
_WEBRTC_DTLS_SETUP_KEY = "HERMES_VOIP_WEBRTC_DTLS_SETUP"
_DEFAULT_WEBRTC_DTLS_SETUP = "auto"
_WEBRTC_DTLS_SETUPS = frozenset({"auto", "active", "passive"})

# SIP DTLS-SRTP activation (ADR-0053 Stage 2). When True (the default), an inbound
# SIP-over-TLS call whose audio offers ``UDP/TLS/RTP/SAVP`` with an ``a=fingerprint``
# is answered with cert-keyed DTLS-SRTP media (the operator's "real certs" preferred
# tier). Setting it false is the ROLLBACK SWITCH (not a downgrade path): a SIP-DTLS
# offer then falls through to the SDES/plain handler unchanged. No effect on the
# WebRTC (SAVPF/ICE) or plain/SDES paths.
_SIP_DTLS_SRTP_KEY = "HERMES_VOIP_SIP_DTLS_SRTP"
_DEFAULT_SIP_DTLS_SRTP = True

# Outbound SDES-SRTP offering (ADR-0067). When True, an agent-originated outbound
# SIP-over-TLS INVITE offers ``RTP/SAVP`` with a fresh per-call ``a=crypto``
# (SDES, RFC 4568) instead of plain ``RTP/AVP``; a 2xx that answers plain RTP/AVP
# then FAILS the call (fail-closed — never a silent plaintext downgrade of a call we
# asked to protect). Default OFF: it is opt-in because the fail-closed policy turns a
# non-SRTP terminating leg into a failed call, so flipping it on is the operator's
# explicit choice once the terminating side is known SRTP-capable. No effect on the
# inbound answer path or the WebRTC outbound path (which already offers DTLS-SRTP).
_SIP_SDES_OFFER_KEY = "HERMES_VOIP_SIP_SDES_OFFER"
_DEFAULT_SIP_SDES_OFFER = False

# Secure-media mandate on the inbound answer path (ADR-0070). When True (the
# default), an inbound INVITE whose audio m-line offers plain ``RTP/AVP`` (no
# SRTP) is REJECTED with ``488 Not Acceptable Here`` instead of being answered as
# a cleartext RTP call. The plugin only ever REGISTERS over TLS/WSS (the transport
# is restricted to ``_VIA_TRANSPORT`` = {tls, wss}), so every SIP *signalling* leg
# is already encrypted; this closes the remaining MEDIA-plane cleartext gap. Any
# SECURED profile is still accepted — SDES (``RTP/SAVP``), DTLS-SRTP
# (``UDP/TLS/RTP/SAVP``) and WebRTC (``UDP/TLS/RTP/SAVPF``) — i.e. every profile
# for which ``AudioMedia.is_srtp`` is true; only plain ``RTP/AVP`` is refused.
# Setting it false is the rollback switch (opportunistic plaintext, the pre-ADR
# behaviour) for a gateway that can only offer cleartext media. No effect on the
# outbound offer path (its own SRTP knobs are independent).
_REQUIRE_SECURE_MEDIA_KEY = "HERMES_VOIP_REQUIRE_SECURE_MEDIA"
_DEFAULT_REQUIRE_SECURE_MEDIA = True

# SIP DTLS-SRTP answerer role (ADR-0053 §2, RFC 8842 §5.3 / RFC 5763 §5). For an
# ``a=setup:actpass`` offer the answerer picks its DTLS role; ``auto`` (the default)
# makes us ``active`` (the DTLS client, sending the ClientHello) — many gateways offer
# ``actpass`` yet act as the DTLS server, so a ``passive`` answer deadlocks. ``passive``
# forces the server role; ``active`` is the explicit form of the default. A pinned
# ``active``/``passive`` offer always overrides this knob. This MIRRORS
# ``HERMES_VOIP_WEBRTC_DTLS_SETUP`` but is INDEPENDENT — the two transports may need
# different defaults against different gateways. No effect on the WebRTC path.
_SIP_DTLS_SETUP_KEY = "HERMES_VOIP_SIP_DTLS_SETUP"
_DEFAULT_SIP_DTLS_SETUP = "auto"
_SIP_DTLS_SETUPS = frozenset({"auto", "active", "passive"})

# WebRTC ICE TURN relay (ADR-0034). A comma-separated list of ``turn:``/``turns:``
# URLs plus long-term credentials (RFC 8656). When set, a *relay* ICE candidate is
# gathered so WebRTC works without a host / STUN-reflexive path (symmetric NAT,
# restrictive firewalls). The plugin only CONSUMES an operator-provided TURN server;
# it does not run one. Empty (the default) ⇒ no relay candidate. When URLs are set,
# both username and password are REQUIRED (a credential-less TURN URL would silently
# gather nothing — rejected loudly at load, rule 27). The password is a secret and is
# repr-suppressed (never logged). Has no effect on the SIP-over-TLS path.
_ICE_TURN_URLS_KEY = "HERMES_VOIP_ICE_TURN_URLS"
_ICE_TURN_USERNAME_KEY = "HERMES_VOIP_ICE_TURN_USERNAME"
# S105 noqa: this is the env-var NAME, not a password value — the secret is read from
# this key at runtime, never hardcoded.
_ICE_TURN_PASSWORD_KEY = "HERMES_VOIP_ICE_TURN_PASSWORD"  # noqa: S105

# Prompt-injection guard (ADR-0009). Default is the in-process ONNX classifier;
# the optional loopback sidecar is opt-in (and out of this parser's scope).
_INJECTION_GUARD_KEY = "HERMES_VOIP_INJECTION_GUARD"
_INJECTION_GUARD_MODEL_DIR_KEY = "HERMES_VOIP_INJECTION_GUARD_MODEL_DIR"
_DEFAULT_INJECTION_GUARD = "onnx"
_INJECTION_GUARDS = frozenset({"onnx", "sidecar"})

# DTMF (ADR-0010/0034). Default `auto` negotiates RFC 4733 from the offer, else falls
# to the in-band last resort on a G.711 call. All four ADR-0010 mechanisms are now
# implemented (ADR-0036): RFC 4733 (telephone-event), SIP INFO (in-dialog), and in-band
# Goertzel — send AND receive. `dtmf_mode` selects per call; the per-call backend is
# resolved (config + negotiation) in `hermes_voip.dtmf_config`, so every mode value
# drives a real backend (no inert key — rule-27).
_DTMF_MODE_KEY = "HERMES_SIP_DTMF_MODE"
_DTMF_INTERDIGIT_MS_KEY = "HERMES_SIP_DTMF_INTERDIGIT_MS"
_DTMF_INBAND_ENABLED_KEY = "HERMES_SIP_DTMF_INBAND_ENABLED"
_DEFAULT_DTMF_MODE = "auto"
_DTMF_MODES = frozenset({"auto", "rfc4733", "sip_info", "inband"})
_DEFAULT_DTMF_INBAND_ENABLED = True

# Tone diagnostic (operator-use only).  When set to a positive number of
# seconds the call opening plays a generated 440 Hz sine tone directly at
# 8 kHz (bypassing TTS + resample) instead of the TTS greeting.  This lets
# the operator confirm the RTP transport and G.711 codec are working before
# implicating the TTS/resample layers.  Unset / 0 = normal operation.
_TEST_TONE_KEY = "HERMES_VOIP_TEST_TONE"
_DEFAULT_TEST_TONE_SECS: float = 0.0

# Boolean spellings accepted for the env booleans (case-insensitive, trimmed).
_TRUE_TOKENS = frozenset({"true", "1", "yes", "on"})
_FALSE_TOKENS = frozenset({"false", "0", "no", "off"})


class ConfigError(ValueError):
    """The ``HERMES_SIP_*`` environment is missing, ambiguous, or malformed."""


@dataclass(frozen=True, slots=True)
class ExtensionConfig:
    """One registrable extension, sourced from ``HERMES_SIP_*``.

    Attributes:
        index: The scheme index (``0`` for the back-compatible single form).
        extension: The extension number / SIP user-part (e.g. ``1000``).
        username: The digest auth username (defaults to ``extension``).
        password: The digest auth password. A **secret** — repr-suppressed so it
            never reaches a log line or traceback (rule 34), the same discipline as
            :attr:`GatewayConfig.ws_password` and the cloud API keys. The value stays
            accessible by attribute for the digest computation.
    """

    index: int
    extension: str
    username: str
    # The SIP digest password is a secret: repr-suppressed so a traceback or
    # config-dump rendering this extension (e.g. via GatewayConfig's extensions
    # tuple) never prints the plaintext credential (rule 34).
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """The shared SIP gateway plus its registrable extensions.

    Attributes:
        host: The gateway FQDN (the SIP domain / registrar).
        port: The signalling port (transport default unless overridden).
        transport: The scheme token (``tls`` | ``wss``).
        expires: The requested registration lifetime in seconds.
        user_agent: The ``User-Agent`` header value for every registration.
        extensions: All configured extensions, ordered by ``index`` ascending.
        default_index: The ``index`` of the inbound-fallback registration.
        ws_path: The WebSocket upgrade path for the SIP-over-WSS transport
            (RFC 7118; default ``/ws``). Only read on the ``wss`` transport.
        ws_password: An optional digest-password override for the ``wss``
            endpoint (ADR-0038). When set on a ``wss`` gateway it replaces the
            per-extension SIP password in the digest; ``None`` (the default)
            falls back to the per-extension SIP password. A **secret** —
            repr-suppressed so it never reaches a log line.
        max_calls: The maximum number of concurrent active calls (ADR-0059). A new
            inbound INVITE arriving while at this cap is rejected ``486 Busy Here``
            BEFORE any per-call media engine / STT-TTS pipeline is built, so a
            burst/flood cannot exhaust CPU/memory on a 24/7 line. Strictly positive
            (default 8). ``HERMES_SIP_MAX_CALLS``.
        shutdown_drain_secs: The bounded graceful-shutdown drain timeout in seconds
            (ADR-0059). On ``disconnect`` (aclose / SIGTERM path) the adapter sends a
            BYE to every live call and waits up to this long for the drain before
            forcing teardown, so a restart no longer hard-drops live callers. Strictly
            positive and finite (default 5.0). ``HERMES_SIP_SHUTDOWN_DRAIN_SECS``.
        deny_mode: How a declined-group caller is handled at the inbound INVITE
            (ADR-0020 §5/§6). ``reject`` (the default, Phase 1) sends a hard
            ``603 Decline`` in the pre-200-OK window — no dialog, no media, no agent
            surface. ``decline`` (Phase 2) ANSWERS the call (200 OK), speaks one short
            TTS line (:attr:`MediaConfig.decline_phrase`), then BYEs — a polite decline
            that trains a spammer less than a hard 603. ``HERMES_VOIP_DENY_MODE``.
    """

    host: str
    port: int
    transport: str
    expires: int
    user_agent: str
    extensions: tuple[ExtensionConfig, ...]
    default_index: int
    ws_path: str = _DEFAULT_WS_PATH
    ws_password: str | None = field(default=None, repr=False)
    max_calls: int = _DEFAULT_MAX_CALLS
    shutdown_drain_secs: float = _DEFAULT_SHUTDOWN_DRAIN_SECS
    deny_mode: DenyMode = _DEFAULT_DENY_MODE

    def __post_init__(self) -> None:
        """Enforce the invariants the type promises, not just the parser.

        ``GatewayConfig`` is public, so a caller can construct one directly;
        the dataclass validates itself rather than trusting
        :func:`load_gateway_config` to have done so (the ``default_extension``
        lookup and the demux logic depend on these holding).
        """
        if not self.extensions:
            msg = "GatewayConfig requires at least one extension"
            raise ConfigError(msg)
        indices = [ext.index for ext in self.extensions]
        if len(set(indices)) != len(indices):
            msg = "GatewayConfig extension indices must be unique"
            raise ConfigError(msg)
        numbers = [ext.extension for ext in self.extensions]
        if len(set(numbers)) != len(numbers):
            msg = "GatewayConfig extension numbers must be unique"
            raise ConfigError(msg)
        if self.default_index not in indices:
            msg = f"default_index {self.default_index} is not a configured index"
            raise ConfigError(msg)
        # ADR-0059 lifecycle knobs validate here too (the dataclass is public, so a
        # direct construction is self-validating, not only the env parser).
        if self.max_calls <= 0:
            msg = f"max_calls must be positive, got {self.max_calls}"
            raise ConfigError(msg)
        if not math.isfinite(self.shutdown_drain_secs) or self.shutdown_drain_secs <= 0:
            msg = (
                "shutdown_drain_secs must be a positive finite number, "
                f"got {self.shutdown_drain_secs!r}"
            )
            raise ConfigError(msg)
        # deny_mode is a Literal, but a direct (non-parser) construction could pass an
        # off-Literal string at runtime; validate against the runtime counterpart so a
        # bad value fails LOUD here (naming the env var) rather than silently slipping
        # past the type into the inbound handler.
        if self.deny_mode not in _DENY_MODES:
            choices = ", ".join(sorted(_DENY_MODES))
            msg = (
                f"{_DENY_MODE_KEY} must be one of {{{choices}}}, got {self.deny_mode!r}"
            )
            raise ConfigError(msg)

    @property
    def via_transport(self) -> ViaTransport:
        """The Via transport token (``TLS`` | ``WSS``) for this gateway."""
        return _VIA_TRANSPORT[self.transport]

    @property
    def default_extension(self) -> ExtensionConfig:
        """The registration that owns inbound calls with no better match."""
        # __post_init__ guarantees exactly one match for default_index.
        return next(ext for ext in self.extensions if ext.index == self.default_index)

    def registration_config(
        self,
        ext: ExtensionConfig,
        *,
        contact: str,
        local_sent_by: str,
    ) -> RegistrationConfig:
        """Complete a :class:`RegistrationConfig` from transport-derived inputs.

        ``contact`` and ``local_sent_by`` are knowable only once the transport
        socket is up (the local host:port, or an ``.invalid`` host for WebSocket
        per RFC 7118), so they are supplied by the caller; everything else comes
        from this env-sourced config. ``ext`` must be one of this gateway's
        configured extensions.
        """
        if ext not in self.extensions:
            msg = f"extension {ext.extension!r} is not configured on this gateway"
            raise ConfigError(msg)
        # ADR-0038: a WebRTC/WSS gateway edge commonly authenticates against a
        # SEPARATE credential than the SIP-TLS edge. On the wss transport, the
        # optional ws_password overrides the per-extension SIP password; unset
        # (or any tls transport) falls back to the per-extension SIP password.
        password = ext.password
        if self.transport == "wss" and self.ws_password is not None:
            password = self.ws_password
        # ADR-0005/ADR-0080: the AOR scheme is ``sips:`` on a secure transport.
        # ``self.transport`` is validated to ``_VIA_TRANSPORT`` = {tls, wss} (see
        # ``_parse_transport``), both TLS-protected, so the scheme is always
        # ``sips:`` here — a ``sip:`` AOR on TLS/WSS is rejected by
        # RegistrationConfig. If a cleartext transport is ever added to that set,
        # this must become transport-derived rather than the ``sips:`` constant.
        return RegistrationConfig(
            aor=f"sips:{ext.extension}@{self.host}",
            username=ext.username,
            password=password,
            contact=contact,
            local_sent_by=local_sent_by,
            transport=self.via_transport,
            expires=self.expires,
            user_agent=self.user_agent,
        )


@dataclass(frozen=True, slots=True)
class MediaConfig:
    """The conversational-media + DTMF feature config (ADR-0006..0010).

    Sourced from the ``HERMES_VOIP_*`` / ``HERMES_SIP_DTMF_*`` env scheme, parsed
    independently of :class:`GatewayConfig`. Every field has a default so a bare
    install runs the fully-offline self-host path; cloud API keys are read by
    reference only and are **repr-suppressed** so a secret never reaches a log
    line (invariant: secrets never logged).

    Attributes:
        stt_provider: Streaming-STT provider token (``sherpa-onnx`` default).
        stt_model_dir: Filesystem path to the pinned STT model dir, or ``None``.
        tts_provider: Streaming-TTS provider token (``sherpa-kokoro`` default).
        tts_model: Provider-specific model id / voice-pack, or ``None``. For
            sherpa-kokoro this is the model *directory*; for ElevenLabs it is the
            synthesis model id (``None`` → the provider's Flash v2.5 default).
        tts_voice: Provider-specific voice id, or ``None``.
        elevenlabs_api_key: ElevenLabs credential (by reference; never logged).
        deepgram_api_key: Deepgram credential (by reference; never logged).
        vad_threshold: Voice-activity probability cut-off in ``[0.0, 1.0]``.
        endpoint_silence_ms: Trailing silence (ms) that ends a caller turn.
        duplex_mode: ``half`` (shipped) or ``full`` (deferred Phase-2 barge-in).
        greeting: Opening line spoken the instant an inbound call is answered
            (``DEFAULT_GREETING`` when unset; ``""`` disables it). Speaking on
            answer sends RTP first — the caller hears it immediately and a
            symmetric-RTP gateway behind NAT latches onto our source tuple.
        rtp_symmetric: Whether the media engine latches its outbound RTP onto the
            peer's real source tuple (the first valid inbound RTP packet) for NAT
            traversal — ``True`` by default. ``False`` always honours the SDP
            ``c=``/``m=`` address.
        barge_in_mode: Echo-robust barge-in mode (ADR-0023): ``gated`` (default —
            require a sustained voiced run while TTS plays), ``full`` (legacy
            immediate barge-in on any onset), or ``off`` (never barge in).
        barge_in_min_speech_ms: In ``gated`` mode, the minimum sustained voiced
            run (ms) required to barge in while the agent's TTS is playing (or in
            the tail after). Must be positive. Short echo blips never reach it.
        barge_in_tail_ms: How long (ms) after the agent's TTS ends the gate keeps
            requiring a sustained run (echo lags the TTS via jitter/network).
            ``0`` disarms the instant TTS ends; must be non-negative.
        barge_in_fade_ms: Length (ms) of the linear fade-out applied to the final
            outbound frames when a barge-in flushes the agent's queued audio
            (ADR-0028), so the clean stop is click-free. Default 30; ``0`` is an
            instant hard cut; must be non-negative.
        injection_guard: Prompt-injection guard token (``onnx`` in-process default).
        injection_guard_model_dir: Path to the guard's ONNX model dir, or ``None``.
        dtmf_mode: The DTMF backend selector (ADR-0010/0034): ``auto`` (negotiate RFC
            4733 from the offer, else the in-band last resort on a G.711 call),
            ``rfc4733`` (force telephone-event), ``sip_info`` (force in-dialog INFO),
            or ``inband`` (force Goertzel/tone-gen, G.711 only). The per-call send +
            receive backend is resolved from this + the negotiation in
            ``hermes_voip.dtmf_config``.
        dtmf_interdigit_ms: Inter-digit gap (ms) for digit aggregation, or ``None``.
        dtmf_inband_enabled: Whether the in-band Goertzel last resort is permitted under
            ``auto`` when the peer offered no telephone-event (default ``True``).
            ``False`` forbids it: an ``auto`` call with no telephone-event then resolves
            to no DTMF rather than the (less spoof-resistant) in-band backend. No effect
            when ``dtmf_mode`` is forced to a specific backend.
        tone_secs: When positive, the call opening plays a generated 440 Hz sine
            tone for this many seconds at 8 kHz (bypassing TTS + resample) so the
            operator can isolate the RTP transport layer from TTS issues.
            ``0.0`` (the default) means normal operation (TTS greeting).
        tts_stability: ElevenLabs ``voice_settings.stability`` in ``[0.0, 1.0]``, or
            ``None`` to use the provider's dynamic default. *Lower* = more
            expressive/varied (the main dynamism dial); too low = inconsistent.
        tts_style: ElevenLabs ``voice_settings.style`` in ``[0.0, 1.0]``, or
            ``None`` for the provider default (``0.0``). Above 0 adds expression but
            costs stability and may add latency — raise deliberately.
        tts_similarity: ElevenLabs ``voice_settings.similarity_boost`` in
            ``[0.0, 1.0]``, or ``None`` for the provider default.
        tts_speaker_boost: ElevenLabs ``voice_settings.use_speaker_boost``, or
            ``None`` for the provider default (``True``).
        tts_streaming_latency: ElevenLabs ``optimize_streaming_latency`` query value
            (int in ``[0, 4]``), or ``None`` to send nothing (the default —
            deprecated param; ``4`` disables number/date normalisation).
        tts_fallback: Automatic TTS failover provider token (ADR-0025), or ``None``
            when failover is off. When the primary TTS raises during synthesis (the
            live HTTP 400, a timeout, any exception) the system synthesises via this
            fallback so the call still gets audio. Defaults to ``sherpa-kokoro`` for a
            cloud primary and ``None`` for a self-host primary;
            ``HERMES_VOIP_TTS_FALLBACK=none`` disables it. Must be a known TTS
            provider that differs from the primary.
        tts_fallback_model: The failover provider's own model directory (e.g. the
            Kokoro dir for a ``sherpa-kokoro`` fallback), or ``None``. The shared
            ``tts_model`` is the cloud primary's model id, so a model-backed self-host
            fallback needs this dedicated dir; required when ``tts_fallback`` is a
            model-backed provider so the fallback can load on a primary failure.
        media_timeout_secs: RTP-inactivity watchdog window in seconds (ADR-0026),
            in ``[1, 300]`` (default 20). The media engine ends the call as a
            MEDIA_TIMEOUT (→ ``/stop``) when no inbound RTP arrives within this
            window, so a silent media/network drop is cleaned up rather than hanging
            the call forever.
        comfort_filler: Dead-air comfort filler master switch (ADR-0030), ``False``
            by default (off = today's behaviour exactly). When on, the call loop
            emits ONE short natural filler on a turn gap that exceeds
            ``comfort_filler_delay_ms`` before the agent's reply audio starts.
        comfort_filler_delay_ms: Dead-air threshold (ms) before one filler fires;
            must be positive (default 900). Inert while ``comfort_filler`` is off.
        comfort_filler_phrases: The filler phrase set (one chosen per gap, round-robin
            per call). Each phrase reads naturally on every TTS model; non-empty.
        jitter_max_depth: The adaptive jitter-buffer ceiling (ADR-0056/0063) — the
            maximum reorder tolerance in packets the RX :class:`JitterBuffer` grows
            to under loss before shrinking back toward its fixed floor. Must be
            ``>= 2`` (the buffer's floor; a lower ceiling would fail engine
            construction). ``HERMES_VOIP_JITTER_MAX_DEPTH`` (default 10 ≈ 200 ms at
            20 ms ptime).
        no_input_reprompt: Caller-silence reprompt master switch (ADR-0057), ``True``
            by default. When ``True``, a live-but-silent caller (RTP flowing, no
            end-of-turn) is reprompted after ``no_input_timeout_ms`` of silence, and
            after ``no_input_max_reprompts`` unanswered reprompts the loop ends the
            call gracefully. ``HERMES_VOIP_NO_INPUT_REPROMPT``.
        no_input_timeout_ms: Silence window (ms) of no caller end-of-turn before a
            reprompt fires (ADR-0057). Must be strictly positive; default 10 000.
            ``HERMES_VOIP_NO_INPUT_TIMEOUT_MS``.
        no_input_max_reprompts: Unanswered reprompts before the loop ends the call
            gracefully (ADR-0057). ``0`` = end on the first silent window with no
            reprompt; must be non-negative; default 2.
            ``HERMES_VOIP_NO_INPUT_MAX_REPROMPTS``.
        no_input_reprompt_phrases: The reprompt phrase set; one is chosen at random
            per fire, never repeating the immediately-previous phrase. Must be
            non-empty. ``HERMES_VOIP_NO_INPUT_REPROMPT_PHRASES`` (pipe-separated).
        goodbye: Spoken-goodbye master switch (ADR-0057), ``True`` by default. When
            ``True``, the loop-initiated graceful end (no-input limit exhausted) speaks
            ``goodbye_phrase`` before :meth:`run` returns so the caller hears a clean
            closing line. ``HERMES_VOIP_GOODBYE``.
        goodbye_phrase: The closing line spoken on a loop-initiated graceful end
            (ADR-0057). Reads naturally on every TTS model. Default ``"Goodbye."``.
            ``HERMES_VOIP_GOODBYE_PHRASE``.
    """

    stt_provider: str
    stt_model_dir: str | None
    tts_provider: str
    tts_model: str | None
    tts_voice: str | None
    elevenlabs_api_key: str | None = field(repr=False)
    deepgram_api_key: str | None = field(repr=False)
    cartesia_api_key: str | None = field(repr=False)
    vad_threshold: float
    endpoint_silence_ms: int
    duplex_mode: str
    greeting: str
    rtp_symmetric: bool
    barge_in_mode: str
    barge_in_min_speech_ms: int
    barge_in_tail_ms: int
    barge_in_fade_ms: int
    injection_guard: str
    injection_guard_model_dir: str | None
    dtmf_mode: str
    dtmf_interdigit_ms: int | None
    dtmf_inband_enabled: bool
    tone_secs: float
    # ElevenLabs dynamic-voice tuning. Defaulted to None so existing direct
    # constructions stay valid and an unset knob means "provider default" (a
    # dynamic-but-stable voice), not a flat override.
    tts_stability: float | None = None
    tts_style: float | None = None
    tts_similarity: float | None = None
    tts_speaker_boost: bool | None = None
    tts_streaming_latency: int | None = None
    # Automatic TTS failover provider token (ADR-0025), or ``None`` when failover
    # is off. Defaulted to None so existing direct constructions stay valid; the
    # parser resolves the cloud-primary default (sherpa-kokoro) when unset.
    tts_fallback: str | None = None
    # The failover provider's OWN model directory (sherpa-kokoro), or ``None``. The
    # shared tts_model is the cloud primary's model id, so a model-backed self-host
    # fallback needs its own dir; required when tts_fallback needs one.
    tts_fallback_model: str | None = None
    # RTP-inactivity watchdog window in seconds (ADR-0026), in [1, 300]. The media
    # engine ends a call as MEDIA_TIMEOUT (→ /stop) when no inbound RTP arrives
    # within this window, so a silent media/network drop is cleaned up instead of
    # hanging the call forever. Defaulted (20) so existing direct constructions stay
    # valid; the parser validates the operator override's bounds.
    media_timeout_secs: int = _DEFAULT_RTP_TIMEOUT_SECS
    # Active conversation language (ADR-0054, ADR-0084), selecting the built-in
    # comfort-filler phrase set.  Any well-formed BCP-47 primary subtag is accepted;
    # languages without a built-in phrase set fall back to the English default.
    # Defaulted so existing direct constructions stay valid.
    language: str = _DEFAULT_LANGUAGE
    # Dead-air comfort filler (ADR-0030, extended ADR-0054). ON by default (the
    # operator wants a slow turn to never leave the caller in silence). When on, the
    # call loop emits a short natural filler ("One moment please.") on the gap after
    # ``comfort_filler_delay_ms`` if no reply audio has started, then RE-EMITS a fresh
    # random phrase every ``comfort_filler_repeat_ms`` until the reply starts —
    # cancelled the instant the reply or a barge-in arrives. Defaulted so existing
    # direct constructions stay valid; off = the pre-filler behaviour exactly.
    comfort_filler: bool = _DEFAULT_COMFORT_FILLER
    # Dead-air threshold (ms): how long the gap must last before the first filler
    # fires. Must be strictly positive (validated). Inert while ``comfort_filler`` off.
    comfort_filler_delay_ms: int = _DEFAULT_COMFORT_FILLER_DELAY_MS
    # Periodic repeat interval (ms): on a sustained gap a fresh filler fires this often
    # after the first, until the reply audio starts. Strictly positive (validated);
    # defaults to the dead-air delay. Inert while ``comfort_filler`` is off.
    comfort_filler_repeat_ms: int = _DEFAULT_COMFORT_FILLER_REPEAT_MS
    # The filler phrase set; one is chosen at RANDOM per fire, never repeating the
    # immediately-previous phrase (ADR-0054). Each phrase reads naturally on every TTS
    # model (no bracket tag). Defaults to the selected language's built-in set; a blank
    # override falls back to it; empty members are dropped (parser).
    comfort_filler_phrases: tuple[str, ...] = _DEFAULT_COMFORT_FILLER_PHRASES
    # Call-progress detection (fax CNG/CED + AMD), ADR-0064. The whole feature is OFF
    # by default (the pipeline assumes a human; the detector adds per-frame Goertzel
    # work + extra system turns). When on, the CallLoop feeds the sans-IO detector and
    # surfaces its events to the agent. Defaulted so existing direct constructions stay
    # valid.
    enable_call_progress: bool = _DEFAULT_CALL_PROGRESS
    # Answering-machine detection — a sub-switch, OFF by default; only active on
    # OUTBOUND calls (on inbound the agent is the answerer). Off keeps fax detection
    # live while AMD and the record cue stay silent.
    enable_amd: bool = _DEFAULT_AMD
    # When a fax tone is detected, auto hang up (a fax cannot converse). ON by default;
    # off lets the agent decide via its call-control tools.
    amd_hang_up_on_fax: bool = _DEFAULT_AMD_HANGUP_ON_FAX
    # WebRTC ICE STUN servers (ADR-0032; default revised ADR-0043), as ``stun:`` URLs
    # for srflx candidate gathering. Defaults to the public dual-stack list; an explicit
    # empty env value ⇒ host-only ICE. No effect on the SIP-over-TLS path. The dataclass
    # default stays empty so existing direct constructions are unchanged;
    # load_media_config supplies DEFAULT_ICE_STUN_URLS when the env key is unset.
    ice_stun_urls: tuple[str, ...] = ()
    # IPv6-first ICE address families (ADR-0043). Both default ON: gather IPv6
    # (preferred in the answer) and IPv4 (fallback). Independently overridable for
    # IPv6-only / IPv4-only deployments. No effect on the SIP-over-TLS path.
    ice_use_ipv4: bool = True
    ice_use_ipv6: bool = True
    # WebRTC DTLS answerer role for an ``a=setup:actpass`` offer (ADR-0050, RFC 8842
    # §5.3): ``auto`` (the default — answer ``active``, the DTLS client) / ``active`` /
    # ``passive`` (force the server role). A pinned ``active``/``passive`` offer always
    # overrides this. Defaulted so existing direct constructions stay valid; validated
    # against the allowed set. No effect on the SIP-over-TLS path.
    webrtc_dtls_setup: str = _DEFAULT_WEBRTC_DTLS_SETUP
    # SIP DTLS-SRTP activation (ADR-0053 Stage 2): when True (the default) an inbound
    # ``UDP/TLS/RTP/SAVP``+fingerprint offer is answered with cert-keyed DTLS-SRTP
    # media; false is the rollback switch (the offer falls through to SDES/plain). No
    # effect on the WebRTC/plain/SDES paths. Defaulted so existing direct constructions
    # stay valid.
    sip_dtls_srtp: bool = _DEFAULT_SIP_DTLS_SRTP
    # SIP DTLS-SRTP answerer role for an ``a=setup:actpass`` offer (ADR-0053 §2, RFC
    # 8842 §5.3): ``auto`` (the default — answer ``active``, the DTLS client) /
    # ``active`` / ``passive`` (force the server role). A pinned ``active``/``passive``
    # offer always overrides this. Mirrors ``webrtc_dtls_setup`` but is independent.
    # Defaulted so existing direct constructions stay valid; validated against the
    # allowed set. No effect on the WebRTC path.
    sip_dtls_setup: str = _DEFAULT_SIP_DTLS_SETUP
    # Outbound SDES-SRTP offering (ADR-0067): when True an agent-originated outbound
    # SIP-over-TLS INVITE offers ``RTP/SAVP`` + a fresh per-call ``a=crypto`` instead
    # of plain ``RTP/AVP``, and a 2xx that answers plain RTP/AVP fails the call
    # (fail-closed). Default OFF (opt-in) so existing outbound deployments keep
    # offering cleartext until the operator enables it. No effect on the inbound
    # answer path or the WebRTC outbound path. Defaulted so existing direct
    # constructions stay valid.
    sip_sdes_offer: bool = _DEFAULT_SIP_SDES_OFFER
    # Secure-media mandate on the inbound answer path (ADR-0070): when True (the
    # default) an inbound INVITE that offers plain ``RTP/AVP`` audio is rejected
    # with 488 instead of being answered as a cleartext RTP call (signalling is
    # already TLS/WSS, so this closes the media-plane cleartext gap). Any secured
    # profile — SDES ``RTP/SAVP``, DTLS-SRTP ``UDP/TLS/RTP/SAVP`` and WebRTC
    # ``UDP/TLS/RTP/SAVPF`` (any ``is_srtp`` offer) — is still accepted. False is
    # the rollback switch (opportunistic plaintext). Defaulted so existing direct
    # constructions stay valid.
    require_secure_media: bool = _DEFAULT_REQUIRE_SECURE_MEDIA
    # WebRTC ICE TURN relay (ADR-0034), as ``turn:``/``turns:`` URLs for relay
    # candidate gathering. Empty (the default) ⇒ no relay candidate. When set, the
    # username + password are required (validated at load). No effect on the
    # SIP-over-TLS path. Defaulted so existing direct constructions stay valid.
    ice_turn_urls: tuple[str, ...] = ()
    ice_turn_username: str | None = None
    # The TURN password is a secret — repr-suppressed so it never reaches a log line
    # or traceback (same discipline as the cloud API keys above).
    ice_turn_password: str | None = field(default=None, repr=False)
    # WebRTC outbound video (ADR-0044): a pre-encoded H.264 Annex-B file path, or
    # None ⇒ the WebRTC video answer is a=inactive (no outbound video). No effect on
    # the SIP-over-TLS path. Defaulted so existing direct constructions stay valid.
    video_source_path: str | None = None
    video_fps: int = _DEFAULT_VIDEO_FPS
    # In-process acoustic echo cancellation (ADR-0033). On by default: the gateway
    # reflects the agent's TTS back, and the canceller subtracts the known outbound
    # reference from each inbound frame before the VAD/ASR see it, so the echo cannot
    # false-trigger barge-in — which is what lets the barge-in threshold drop to a
    # responsive 200 ms (see ``barge_in_min_speech_ms``). ``aec_enabled=False``
    # reverts to ADR-0023's sustained-gate-only behaviour. Defaulted so existing
    # direct constructions stay valid.
    aec_enabled: bool = _DEFAULT_AEC_ENABLED
    # The adaptive-filter length (ms) — taps = ms x analysis_rate / 1000. Spans the
    # echo path's impulse response; must be positive (validated).
    aec_filter_ms: int = _DEFAULT_AEC_FILTER_MS
    # A fixed reference delay (ms) skipped before the adaptive window, for a gateway
    # with a large constant echo-return delay; 0 lets the taps cover it. Non-negative.
    aec_bulk_delay_ms: int = _DEFAULT_AEC_BULK_DELAY_MS
    # NLMS step size in the OPEN interval (0, 2); higher converges faster with a
    # higher steady-state residual (validated).
    aec_mu: float = _DEFAULT_AEC_MU
    # Adaptive jitter-buffer ceiling (ADR-0056 activated by ADR-0063): the maximum
    # reorder tolerance (packets) the RX JitterBuffer's adaptive depth may grow to
    # under loss/reorder before shrinking back toward the engine's fixed floor. Must
    # be positive (validated). Defaulted (10 ≈ 200 ms at 20 ms ptime) so existing
    # direct constructions stay valid and a bare install gets adaptive jitter.
    jitter_max_depth: int = _DEFAULT_JITTER_MAX_DEPTH
    # RTCP (RFC 3550 §6, ADR-0061): when True (the default) the adapter activates the
    # RTCP SR/RR/SDES/BYE control channel on the cleartext plain-RTP path. The master
    # operator kill-switch (suppresses ALL RTCP, cleartext and secured).
    rtcp_enabled: bool = _DEFAULT_RTCP_ENABLED
    # Secured-path RTCP over SRTCP (RFC 3711 §3.4, ADR-0066): when True the adapter
    # ALSO activates RTCP (wrapped in SRTCP) on the secured SDES (RTP/SAVP) path.
    # Default FALSE — a live non-mux Grandstream muted the media on the unexpected
    # SRTCP, so secured RTCP is opt-in pending real-gateway validation; by default the
    # secured path stays RTCP-dormant (the pre-#160 behaviour). Gated additionally by
    # ``rtcp_enabled`` (the master kill-switch).
    secured_rtcp_enabled: bool = _DEFAULT_SECURED_RTCP_ENABLED
    # RFC 4028 session timers (ADR-0071). ``session_expires`` is the interval (seconds)
    # we offer outbound / insert into an inbound 2xx; the refresher refreshes at SE/2
    # and the non-refresher BYEs near expiry, so a dead dialog is reclaimed. ``min_se``
    # is the smallest interval we accept inbound — a smaller offered Session-Expires is
    # rejected 422 with our Min-SE. RFC 4028 §4/§5 floors both at 90 s; __post_init__
    # enforces ``min_se >= 90`` and ``session_expires >= min_se``. Defaulted so existing
    # direct constructions stay valid.
    session_expires: int = _DEFAULT_SESSION_EXPIRES
    min_se: int = _DEFAULT_MIN_SE
    # Caller-silence reprompt / no-input handling (ADR-0057). When
    # ``no_input_reprompt`` is True (the default), the call loop reprompts the caller
    # after ``no_input_timeout_ms`` of silence, and ends the call gracefully after
    # ``no_input_max_reprompts`` unanswered reprompts. ``no_input_reprompt_phrases``
    # is the phrase set (one chosen at random per fire, no immediate repeat). All
    # five defaults MUST exactly match call_loop.py module-level constants so
    # behaviour is UNCHANGED when env vars are unset. ``HERMES_VOIP_NO_INPUT_*``.
    no_input_reprompt: bool = _DEFAULT_NO_INPUT_REPROMPT
    no_input_timeout_ms: int = _DEFAULT_NO_INPUT_TIMEOUT_MS
    no_input_max_reprompts: int = _DEFAULT_NO_INPUT_MAX_REPROMPTS
    no_input_reprompt_phrases: tuple[str, ...] = _DEFAULT_NO_INPUT_REPROMPT_PHRASES
    # Spoken goodbye on a loop-initiated graceful end (ADR-0057). When True (the
    # default), the loop speaks ``goodbye_phrase`` before run() returns so the caller
    # hears a clean closing line. The phrase MUST exactly match call_loop.py's
    # _DEFAULT_GOODBYE_PHRASE so behaviour is unchanged when env vars are unset.
    # ``HERMES_VOIP_GOODBYE`` / ``HERMES_VOIP_GOODBYE_PHRASE``.
    goodbye: bool = _DEFAULT_GOODBYE
    goodbye_phrase: str = _DEFAULT_GOODBYE_PHRASE
    # Polite-decline line spoken on a ``deny_mode=decline`` declined caller (ADR-0020
    # §5/§6 Phase 2). When the gateway's deny_mode is ``decline``, a declined-group
    # caller is ANSWERED (200 OK) and hears this one short line over the real media path
    # before the call is BYE'd — a polite decline that trains a spammer less than a hard
    # 603. Must be non-blank (a blank line would answer-then-immediately-BYE with dead
    # air). ``HERMES_VOIP_DECLINE_PHRASE``; blank/unset → the built-in default.
    decline_phrase: str = _DEFAULT_DECLINE_PHRASE
    # Safe-decline line spoken on a guard REFUSE (ADR-0076): one phrase chosen at
    # random per refusal (no immediate repeat) so a false-positived caller hears a
    # short line instead of dead air. The refused turn is STILL never delivered to the
    # agent. The default MUST match call_loop.py's _DEFAULT_REFUSE_DECLINE_PHRASES so
    # behaviour matches when env is unset. ``HERMES_VOIP_REFUSE_DECLINE_PHRASES``.
    refuse_decline_phrases: tuple[str, ...] = _DEFAULT_REFUSE_DECLINE_PHRASES
    # Operator-overridable spoken apology for provider/runtime errors (ADR-0063).
    # When set to a non-empty string (``HERMES_VOIP_ERROR_APOLOGY`` env var), this
    # line is spoken instead of the built-in per-language apology, allowing operators
    # to customise the message for their deployment.  Empty string (the default) means
    # use the per-language built-in line (or English fallback for an unknown language).
    # NOT repr-suppressed: the apology text is safe to log (no secret content).
    error_apology: str = ""

    def __post_init__(self) -> None:
        """Enforce the value invariants the type promises.

        :class:`MediaConfig` is public, so a caller can construct one directly;
        it validates the bounded/enumerated fields itself rather than trusting
        :func:`load_media_config` to have done so.
        """
        # Normalise the language tag to lowercase so both the direct-construction
        # path and the env path (which lowercases via _value_lower) behave
        # identically.  _validate_comfort_filler then matches against _LANGUAGE_RE
        # on the already-lowercased value.  object.__setattr__ is required because
        # MediaConfig is a frozen dataclass; this is the standard pattern for
        # normalising frozen-dataclass fields in __post_init__.
        object.__setattr__(self, "language", self.language.lower())
        if not _finite_in_range(
            self.vad_threshold, _MIN_VAD_THRESHOLD, _MAX_VAD_THRESHOLD
        ):
            msg = (
                f"vad_threshold must be a finite value in "
                f"[{_MIN_VAD_THRESHOLD}, {_MAX_VAD_THRESHOLD}], "
                f"got {self.vad_threshold!r}"
            )
            raise ConfigError(msg)
        if self.endpoint_silence_ms <= 0:
            msg = (
                f"endpoint_silence_ms must be positive, got {self.endpoint_silence_ms}"
            )
            raise ConfigError(msg)
        if self.dtmf_interdigit_ms is not None and self.dtmf_interdigit_ms <= 0:
            msg = (
                f"dtmf_interdigit_ms must be positive when set, "
                f"got {self.dtmf_interdigit_ms}"
            )
            raise ConfigError(msg)
        if self.duplex_mode not in _DUPLEX_MODES:
            allowed = ", ".join(sorted(_DUPLEX_MODES))
            msg = f"duplex_mode must be one of {{{allowed}}}, got {self.duplex_mode!r}"
            raise ConfigError(msg)
        if self.barge_in_mode not in _BARGE_IN_MODES:
            allowed = ", ".join(sorted(_BARGE_IN_MODES))
            msg = (
                f"barge_in_mode must be one of {{{allowed}}}, "
                f"got {self.barge_in_mode!r}"
            )
            raise ConfigError(msg)
        if self.barge_in_min_speech_ms <= 0:
            msg = (
                f"barge_in_min_speech_ms must be positive, "
                f"got {self.barge_in_min_speech_ms}"
            )
            raise ConfigError(msg)
        if self.barge_in_tail_ms < 0:
            msg = f"barge_in_tail_ms must be non-negative, got {self.barge_in_tail_ms}"
            raise ConfigError(msg)
        if self.barge_in_fade_ms < 0:
            msg = f"barge_in_fade_ms must be non-negative, got {self.barge_in_fade_ms}"
            raise ConfigError(msg)
        self._validate_aec()
        self._validate_comfort_filler()
        self._validate_no_input()
        self._validate_media_timers()
        if self.dtmf_mode not in _DTMF_MODES:
            allowed = ", ".join(sorted(_DTMF_MODES))
            msg = f"dtmf_mode must be one of {{{allowed}}}, got {self.dtmf_mode!r}"
            raise ConfigError(msg)
        # All four ADR-0010 modes are implemented (ADR-0036); the per-call backend is
        # resolved from the mode + negotiation in hermes_voip.dtmf_config, so no mode
        # value is inert (rule-27) and none is rejected beyond the vocabulary check.
        _require_enum("stt_provider", self.stt_provider, _STT_PROVIDERS)
        _require_enum("tts_provider", self.tts_provider, _TTS_PROVIDERS)
        _require_enum("injection_guard", self.injection_guard, _INJECTION_GUARDS)
        _require_enum("webrtc_dtls_setup", self.webrtc_dtls_setup, _WEBRTC_DTLS_SETUPS)
        _require_enum("sip_dtls_setup", self.sip_dtls_setup, _SIP_DTLS_SETUPS)
        if not math.isfinite(self.tone_secs) or self.tone_secs < 0:
            msg = (
                "tone_secs must be a non-negative finite number, "
                f"got {self.tone_secs!r}"
            )
            raise ConfigError(msg)
        self._validate_tts_tuning()
        # Cloud keys first: a missing PRIMARY credential is the more fundamental error
        # (reported before the fallback's own requirements).
        self._require_cloud_keys()
        self._validate_tts_fallback()

    def _validate_tts_fallback(self) -> None:
        """Validate the TTS failover provider token (ADR-0025), when set.

        ``None`` (failover off) is always valid. A set token must be a known TTS
        provider and must differ from the primary ``tts_provider`` — a fallback that
        is the same provider cannot recover the same failure. A model-backed self-host
        fallback (e.g. ``sherpa-kokoro``) additionally **requires its own model dir**
        (:data:`_TTS_FALLBACK_MODEL_KEY`): the shared ``tts_model`` is the cloud
        primary's model id, not a Kokoro directory, so without a dedicated dir the
        fallback could not be built and the call would still die silent on the first
        primary failure — that is rejected here, at startup, not discovered live.
        """
        if self.tts_fallback is None:
            return
        if self.tts_fallback not in _TTS_PROVIDERS:
            opts = ", ".join(sorted(_TTS_PROVIDERS))
            msg = (
                f"tts_fallback must be one of {{{opts}}} or 'none', "
                f"got {self.tts_fallback!r}"
            )
            raise ConfigError(msg)
        if self.tts_fallback == self.tts_provider:
            msg = (
                f"tts_fallback {self.tts_fallback!r} must differ from the primary "
                f"tts_provider {self.tts_provider!r} (a same-provider fallback "
                "cannot recover the primary's failure)"
            )
            raise ConfigError(msg)
        if (
            self.tts_fallback in _MODEL_DIR_TTS_PROVIDERS
            and not self.tts_fallback_model
        ):
            msg = (
                f"tts_fallback {self.tts_fallback!r} requires "
                f"{_TTS_FALLBACK_MODEL_KEY} to be set (the fallback's own model "
                "directory — the shared HERMES_VOIP_TTS_MODEL is the cloud primary's "
                "model id, not a Kokoro directory). Set it so the fallback can load on "
                "a primary failure, or set "
                f"{_TTS_FALLBACK_KEY}=none to disable failover"
            )
            raise ConfigError(msg)

    def _validate_comfort_filler(self) -> None:
        """Validate the dead-air comfort-filler invariants (ADR-0030/0054/0084).

        The delay and the periodic repeat interval must be strictly positive (a
        non-positive dead-air / repeat interval is meaningless); the language must be
        a well-formed BCP-47 primary subtag (ADR-0084 — acceptance is structural, not
        membership in the phrase dict); the phrase set must be non-empty and contain no
        blank phrase (a filler with nothing to say is a silent no-op). These hold
        regardless of the master switch so a direct :class:`MediaConfig` construction
        is also self-validating.
        """
        if self.comfort_filler_delay_ms <= 0:
            msg = (
                f"comfort_filler_delay_ms must be positive, "
                f"got {self.comfort_filler_delay_ms}"
            )
            raise ConfigError(msg)
        if self.comfort_filler_repeat_ms <= 0:
            msg = (
                f"comfort_filler_repeat_ms must be positive, "
                f"got {self.comfort_filler_repeat_ms}"
            )
            raise ConfigError(msg)
        if not _LANGUAGE_RE.match(self.language):
            msg = (
                f"language must be a well-formed BCP-47 language tag "
                f"(e.g. 'en', 'fr', 'pt-BR'), got {self.language!r}"
            )
            raise ConfigError(msg)
        if not self.comfort_filler_phrases:
            msg = "comfort_filler_phrases must not be empty"
            raise ConfigError(msg)
        if any(not phrase.strip() for phrase in self.comfort_filler_phrases):
            msg = "comfort_filler_phrases must not contain a blank phrase"
            raise ConfigError(msg)

    def _validate_no_input(self) -> None:
        """Validate the caller-silence reprompt / no-input invariants (ADR-0057).

        The timeout must be strictly positive (a non-positive silence window fires
        immediately and is meaningless); the max-reprompts count must be non-negative
        (``0`` is valid — end on the first silent window, no reprompt); the phrase
        set must be non-empty and contain no blank phrase. These hold regardless of
        the master switch so a direct :class:`MediaConfig` construction is
        self-validating.
        """
        if self.no_input_timeout_ms <= 0:
            msg = (
                f"no_input_timeout_ms must be positive, got {self.no_input_timeout_ms}"
            )
            raise ConfigError(msg)
        if self.no_input_max_reprompts < 0:
            msg = (
                "no_input_max_reprompts must be non-negative, "
                f"got {self.no_input_max_reprompts}"
            )
            raise ConfigError(msg)
        if not self.no_input_reprompt_phrases:
            msg = "no_input_reprompt_phrases must not be empty"
            raise ConfigError(msg)
        if any(not phrase.strip() for phrase in self.no_input_reprompt_phrases):
            msg = "no_input_reprompt_phrases must not contain a blank phrase"
            raise ConfigError(msg)
        if not self.goodbye_phrase.strip():
            msg = "goodbye_phrase must not be blank"
            raise ConfigError(msg)
        if not self.decline_phrase.strip():
            msg = "decline_phrase must not be blank"
            raise ConfigError(msg)
        if self.error_apology and not self.error_apology.strip():
            msg = "error_apology must not be blank when set"
            raise ConfigError(msg)
        # An EMPTY refuse_decline_phrases tuple is allowed (the operator opting OUT of
        # the spoken decline, back to the prior pure-silence behaviour); a tuple WITH a
        # blank member is a misconfiguration — the loop would synthesise empty speech.
        if any(not phrase.strip() for phrase in self.refuse_decline_phrases):
            msg = "refuse_decline_phrases must not contain a blank phrase"
            raise ConfigError(msg)

    def _validate_media_timers(self) -> None:
        """Validate media-plane timer bounds: RTP-inactivity, jitter, session timers.

        Groups the bounded media-timing invariants so :meth:`__post_init__` stays under
        the statement budget: the RTP-inactivity watchdog window (ADR-0026, [1, 300] s),
        the adaptive jitter-buffer ceiling floor (ADR-0056/0063), and the RFC 4028
        session timers (ADR-0071). Holds regardless of how the config is built so a
        direct :class:`MediaConfig` construction is self-validating.

        RFC 4028 session timers: both intervals are floored at 90 s (RFC 4028 §4/§5: the
        absolute minimum for ``Session-Expires``/``Min-SE``), and the offered/inserted
        ``session_expires`` must not be below the ``min_se`` we accept — otherwise our
        own ``Session-Expires`` would be below our advertised minimum and a strict peer
        could 422 it.
        """
        if not (
            _MIN_RTP_TIMEOUT_SECS <= self.media_timeout_secs <= _MAX_RTP_TIMEOUT_SECS
        ):
            msg = (
                f"media_timeout_secs must be in "
                f"[{_MIN_RTP_TIMEOUT_SECS}, {_MAX_RTP_TIMEOUT_SECS}], "
                f"got {self.media_timeout_secs}"
            )
            raise ConfigError(msg)
        if self.jitter_max_depth < _MIN_JITTER_MAX_DEPTH:
            msg = (
                f"jitter_max_depth must be >= {_MIN_JITTER_MAX_DEPTH} (the adaptive "
                f"jitter buffer's floor — a lower ceiling would fail engine "
                f"construction), got {self.jitter_max_depth}"
            )
            raise ConfigError(msg)
        if self.min_se < _RFC4028_MIN_SE_FLOOR:
            msg = (
                f"min_se must be >= {_RFC4028_MIN_SE_FLOOR} (the RFC 4028 §4/§5 "
                f"floor), got {self.min_se}"
            )
            raise ConfigError(msg)
        if self.session_expires < self.min_se:
            msg = (
                f"session_expires ({self.session_expires}) must be >= min_se "
                f"({self.min_se}) — RFC 4028: the session interval must not be below "
                f"the minimum we accept"
            )
            raise ConfigError(msg)

    def _validate_aec(self) -> None:
        """Validate the acoustic-echo-cancellation invariants (ADR-0033).

        The filter length must be strictly positive (a zero-tap filter cancels
        nothing); the bulk delay must be non-negative; the NLMS step ``mu`` must be
        in the OPEN interval ``(0, 2)`` — ``0`` never adapts and ``>= 2`` diverges.
        These hold regardless of ``aec_enabled`` so a direct :class:`MediaConfig`
        construction is self-validating.
        """
        if self.aec_filter_ms <= 0:
            msg = f"aec_filter_ms must be positive, got {self.aec_filter_ms}"
            raise ConfigError(msg)
        if self.aec_bulk_delay_ms < 0:
            msg = (
                f"aec_bulk_delay_ms must be non-negative, got {self.aec_bulk_delay_ms}"
            )
            raise ConfigError(msg)
        if not _MIN_AEC_MU < self.aec_mu < _MAX_AEC_MU:
            msg = (
                f"aec_mu must be in the open interval "
                f"({_MIN_AEC_MU}, {_MAX_AEC_MU}), got {self.aec_mu}"
            )
            raise ConfigError(msg)

    def _validate_tts_tuning(self) -> None:
        """Validate the optional ElevenLabs voice-tuning knobs (when set).

        Each float must be finite and within ``[0.0, 1.0]``; the streaming-latency
        int must be within ``[0, 4]``. ``None`` (unset) is always valid — the
        provider then applies its dynamic default for that field.
        """
        for name, value in (
            ("tts_stability", self.tts_stability),
            ("tts_style", self.tts_style),
            ("tts_similarity", self.tts_similarity),
        ):
            if value is not None and not _finite_in_range(
                value, _MIN_TTS_SETTING, _MAX_TTS_SETTING
            ):
                msg = (
                    f"{name} must be a finite value in "
                    f"[{_MIN_TTS_SETTING}, {_MAX_TTS_SETTING}], got {value!r}"
                )
                raise ConfigError(msg)
        if self.tts_streaming_latency is not None and not (
            _MIN_TTS_STREAMING_LATENCY
            <= self.tts_streaming_latency
            <= _MAX_TTS_STREAMING_LATENCY
        ):
            msg = (
                f"tts_streaming_latency must be in "
                f"[{_MIN_TTS_STREAMING_LATENCY}, {_MAX_TTS_STREAMING_LATENCY}], "
                f"got {self.tts_streaming_latency}"
            )
            raise ConfigError(msg)

    def _require_cloud_keys(self) -> None:
        """A selected cloud provider must have its credential set (fail-fast)."""
        if (
            key := _STT_REQUIRED_KEY.get(self.stt_provider)
        ) and not self.deepgram_api_key:
            msg = f"stt_provider {self.stt_provider!r} requires {key} to be set"
            raise ConfigError(msg)
        tts_key_env = _TTS_REQUIRED_KEY.get(self.tts_provider)
        if tts_key_env is not None:
            held = {
                _ELEVENLABS_API_KEY: self.elevenlabs_api_key,
                _CARTESIA_API_KEY: self.cartesia_api_key,
                _DEEPGRAM_API_KEY: self.deepgram_api_key,
            }[tts_key_env]
            if not held:
                msg = (
                    f"tts_provider {self.tts_provider!r} requires "
                    f"{tts_key_env} to be set"
                )
                raise ConfigError(msg)


def _require_enum(name: str, value: str, allowed: frozenset[str]) -> None:
    """Raise ConfigError unless ``value`` is one of ``allowed`` (fail-fast)."""
    if value not in allowed:
        opts = ", ".join(sorted(allowed))
        msg = f"{name} must be one of {{{opts}}}, got {value!r}"
        raise ConfigError(msg)


def load_media_config(env: Mapping[str, str]) -> MediaConfig:
    """Parse the media/feature env scheme into a validated :class:`MediaConfig`.

    Additive to :func:`load_gateway_config` and a pure function of ``env`` (no
    process environment is read). Every key is optional and defaults to the
    fully-offline self-host path. Free-form provider/model/voice strings are
    taken as-is (trimmed); enumerated tokens are lower-cased then checked.

    Raises:
        ConfigError: if an enum value is unknown, a numeric value is malformed or
            out of range, or a boolean value is unrecognised.
    """
    tts_provider = _value_lower(env, _TTS_PROVIDER_KEY) or _DEFAULT_TTS_PROVIDER
    # AEC-aware barge-in threshold default (ADR-0033): with AEC ON the reflected
    # echo is cancelled before the VAD, so the 600 ms echo-safety margin (ADR-0023)
    # is unnecessary and the threshold drops to a responsive 200 ms. AEC OFF keeps
    # the 600 ms default. An explicit HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS always wins
    # (the parse below uses this only as the fallback when the key is unset).
    aec_enabled = _parse_bool(env, _AEC_ENABLED_KEY, _DEFAULT_AEC_ENABLED)
    barge_in_min_speech_default = (
        _DEFAULT_BARGE_IN_MIN_SPEECH_MS_AEC
        if aec_enabled
        else _DEFAULT_BARGE_IN_MIN_SPEECH_MS
    )
    # TURN relay (ADR-0034): parsed + validated together (URLs require credentials).
    _ice_turn = _parse_ice_turn(env)
    # Active language (ADR-0054), parsed before the comfort-filler phrases so a blank
    # phrase override can fall back to THIS language's built-in set.
    language = _parse_language(env)
    return MediaConfig(
        stt_provider=_value_lower(env, _STT_PROVIDER_KEY) or _DEFAULT_STT_PROVIDER,
        stt_model_dir=_optional(env, _STT_MODEL_DIR_KEY),
        tts_provider=tts_provider,
        tts_model=_optional(env, _TTS_MODEL_KEY),
        tts_voice=_optional(env, _TTS_VOICE_KEY),
        elevenlabs_api_key=_optional(env, _ELEVENLABS_API_KEY),
        deepgram_api_key=_optional(env, _DEEPGRAM_API_KEY),
        cartesia_api_key=_optional(env, _CARTESIA_API_KEY),
        vad_threshold=_parse_vad_threshold(env),
        endpoint_silence_ms=_parse_positive_int(
            env, _ENDPOINT_SILENCE_MS_KEY, _DEFAULT_ENDPOINT_SILENCE_MS
        ),
        duplex_mode=_parse_enum(
            env, _DUPLEX_MODE_KEY, _DUPLEX_MODES, _DEFAULT_DUPLEX_MODE
        ),
        greeting=_parse_greeting(env),
        rtp_symmetric=_parse_bool(env, _RTP_SYMMETRIC_KEY, _DEFAULT_RTP_SYMMETRIC),
        rtcp_enabled=_parse_bool(env, _RTCP_ENABLED_KEY, _DEFAULT_RTCP_ENABLED),
        secured_rtcp_enabled=_parse_bool(
            env, _SECURED_RTCP_ENABLED_KEY, _DEFAULT_SECURED_RTCP_ENABLED
        ),
        barge_in_mode=_parse_enum(
            env, _BARGE_IN_MODE_KEY, _BARGE_IN_MODES, _DEFAULT_BARGE_IN_MODE
        ),
        barge_in_min_speech_ms=_parse_positive_int(
            env, _BARGE_IN_MIN_SPEECH_MS_KEY, barge_in_min_speech_default
        ),
        barge_in_tail_ms=_parse_non_negative_int(
            env, _BARGE_IN_TAIL_MS_KEY, _DEFAULT_BARGE_IN_TAIL_MS
        ),
        barge_in_fade_ms=_parse_non_negative_int(
            env, _BARGE_IN_FADE_MS_KEY, _DEFAULT_BARGE_IN_FADE_MS
        ),
        language=language,
        comfort_filler=_parse_bool(env, _COMFORT_FILLER_KEY, _DEFAULT_COMFORT_FILLER),
        comfort_filler_delay_ms=_parse_positive_int(
            env, _COMFORT_FILLER_DELAY_MS_KEY, _DEFAULT_COMFORT_FILLER_DELAY_MS
        ),
        comfort_filler_repeat_ms=_parse_positive_int(
            env, _COMFORT_FILLER_REPEAT_MS_KEY, _DEFAULT_COMFORT_FILLER_REPEAT_MS
        ),
        comfort_filler_phrases=_parse_comfort_filler_phrases(env, language),
        enable_call_progress=_parse_bool(
            env, _CALL_PROGRESS_KEY, _DEFAULT_CALL_PROGRESS
        ),
        enable_amd=_parse_bool(env, _AMD_KEY, _DEFAULT_AMD),
        amd_hang_up_on_fax=_parse_bool(
            env, _AMD_HANGUP_ON_FAX_KEY, _DEFAULT_AMD_HANGUP_ON_FAX
        ),
        injection_guard=_value_lower(env, _INJECTION_GUARD_KEY)
        or _DEFAULT_INJECTION_GUARD,
        injection_guard_model_dir=_optional(env, _INJECTION_GUARD_MODEL_DIR_KEY),
        dtmf_mode=_parse_enum(env, _DTMF_MODE_KEY, _DTMF_MODES, _DEFAULT_DTMF_MODE),
        dtmf_interdigit_ms=_parse_optional_positive_int(env, _DTMF_INTERDIGIT_MS_KEY),
        dtmf_inband_enabled=_parse_bool(
            env, _DTMF_INBAND_ENABLED_KEY, _DEFAULT_DTMF_INBAND_ENABLED
        ),
        tone_secs=_parse_tone_secs(env),
        tts_stability=_parse_optional_unit_float(env, _TTS_STABILITY_KEY),
        tts_style=_parse_optional_unit_float(env, _TTS_STYLE_KEY),
        tts_similarity=_parse_optional_unit_float(env, _TTS_SIMILARITY_KEY),
        tts_speaker_boost=_parse_optional_bool(env, _TTS_SPEAKER_BOOST_KEY),
        tts_streaming_latency=_parse_optional_bounded_int(
            env,
            _TTS_STREAMING_LATENCY_KEY,
            _MIN_TTS_STREAMING_LATENCY,
            _MAX_TTS_STREAMING_LATENCY,
        ),
        tts_fallback=_parse_tts_fallback(env, tts_provider),
        tts_fallback_model=_optional(env, _TTS_FALLBACK_MODEL_KEY),
        media_timeout_secs=_parse_bounded_int(
            env,
            _RTP_TIMEOUT_SECS_KEY,
            _MIN_RTP_TIMEOUT_SECS,
            _MAX_RTP_TIMEOUT_SECS,
            _DEFAULT_RTP_TIMEOUT_SECS,
        ),
        ice_stun_urls=_parse_ice_stun_urls(env),
        ice_use_ipv4=_parse_bool(env, _ICE_USE_IPV4_KEY, default=True),
        ice_use_ipv6=_parse_bool(env, _ICE_USE_IPV6_KEY, default=True),
        webrtc_dtls_setup=_value_lower(env, _WEBRTC_DTLS_SETUP_KEY)
        or _DEFAULT_WEBRTC_DTLS_SETUP,
        sip_dtls_srtp=_parse_bool(env, _SIP_DTLS_SRTP_KEY, _DEFAULT_SIP_DTLS_SRTP),
        sip_dtls_setup=_value_lower(env, _SIP_DTLS_SETUP_KEY)
        or _DEFAULT_SIP_DTLS_SETUP,
        sip_sdes_offer=_parse_bool(env, _SIP_SDES_OFFER_KEY, _DEFAULT_SIP_SDES_OFFER),
        require_secure_media=_parse_bool(
            env, _REQUIRE_SECURE_MEDIA_KEY, _DEFAULT_REQUIRE_SECURE_MEDIA
        ),
        ice_turn_urls=_ice_turn[0],
        ice_turn_username=_ice_turn[1],
        ice_turn_password=_ice_turn[2],
        video_source_path=_optional(env, _VIDEO_SOURCE_PATH_KEY),
        video_fps=_parse_bounded_int(
            env, _VIDEO_FPS_KEY, _MIN_VIDEO_FPS, _MAX_VIDEO_FPS, _DEFAULT_VIDEO_FPS
        ),
        aec_enabled=aec_enabled,
        aec_filter_ms=_parse_positive_int(
            env, _AEC_FILTER_MS_KEY, _DEFAULT_AEC_FILTER_MS
        ),
        aec_bulk_delay_ms=_parse_non_negative_int(
            env, _AEC_BULK_DELAY_MS_KEY, _DEFAULT_AEC_BULK_DELAY_MS
        ),
        aec_mu=_parse_aec_mu(env),
        jitter_max_depth=_parse_positive_int(
            env, _JITTER_MAX_DEPTH_KEY, _DEFAULT_JITTER_MAX_DEPTH
        ),
        # RFC 4028 session timers (ADR-0071). Parsed as strictly-positive ints; the
        # RFC 4028 §4/§5 floor (min_se >= 90) and the session_expires >= min_se
        # ordering are enforced in MediaConfig.__post_init__ (so a direct construction
        # is self-validating too).
        session_expires=_parse_positive_int(
            env, _SESSION_EXPIRES_KEY, _DEFAULT_SESSION_EXPIRES
        ),
        min_se=_parse_positive_int(env, _MIN_SE_KEY, _DEFAULT_MIN_SE),
        # Caller-silence reprompt / no-input handling (ADR-0057). Default values
        # MUST match call_loop.py's module-level constants so behaviour is UNCHANGED
        # when the env vars are unset (no regression).
        no_input_reprompt=_parse_bool(
            env, _NO_INPUT_REPROMPT_KEY, _DEFAULT_NO_INPUT_REPROMPT
        ),
        no_input_timeout_ms=_parse_positive_int(
            env, _NO_INPUT_TIMEOUT_MS_KEY, _DEFAULT_NO_INPUT_TIMEOUT_MS
        ),
        no_input_max_reprompts=_parse_non_negative_int(
            env, _NO_INPUT_MAX_REPROMPTS_KEY, _DEFAULT_NO_INPUT_MAX_REPROMPTS
        ),
        no_input_reprompt_phrases=_parse_no_input_reprompt_phrases(env),
        goodbye=_parse_bool(env, _GOODBYE_KEY, _DEFAULT_GOODBYE),
        goodbye_phrase=_parse_goodbye_phrase(env),
        # ADR-0020 §5/§6: the polite-decline line for deny_mode=decline.
        decline_phrase=_parse_decline_phrase(env),
        refuse_decline_phrases=_parse_refuse_decline_phrases(env, language),
        error_apology=_value(env, _ERROR_APOLOGY_KEY) or "",
    )


def load_gateway_config(env: Mapping[str, str]) -> GatewayConfig:
    """Parse the ``HERMES_SIP_*`` mapping into a validated :class:`GatewayConfig`.

    Raises:
        ConfigError: if a required value is missing, a value is malformed, the
            single and indexed schemes are mixed, or extension numbers collide.
    """
    host = _require_host(env)
    transport = _parse_transport(env)
    port = _parse_port(env, transport)
    expires = _parse_expires(env)
    user_agent = _value(env, _USER_AGENT_KEY) or _DEFAULT_USER_AGENT
    ws_path = _value(env, _WS_PATH_KEY) or _DEFAULT_WS_PATH
    # The separate WSS digest credential (ADR-0038): read by name; the value lives
    # only in .env / 1Password. None ⇒ fall back to the per-extension SIP password.
    ws_password = _value(env, _WS_PASSWORD_KEY) or None

    extensions = _parse_extensions(env)
    default_index = _resolve_default_index(env, extensions)

    return GatewayConfig(
        host=host,
        port=port,
        transport=transport,
        expires=expires,
        user_agent=user_agent,
        extensions=extensions,
        default_index=default_index,
        ws_path=ws_path,
        ws_password=ws_password,
        # ADR-0059 lifecycle knobs (admission cap + shutdown drain).
        max_calls=_parse_positive_int(env, _MAX_CALLS_KEY, _DEFAULT_MAX_CALLS),
        shutdown_drain_secs=_parse_positive_float(
            env, _SHUTDOWN_DRAIN_SECS_KEY, _DEFAULT_SHUTDOWN_DRAIN_SECS
        ),
        # ADR-0020 §5/§6: declined-caller disposition (reject 603 | decline+TTS+BYE).
        deny_mode=_parse_deny_mode(env),
    )


# --- shared field parsing ---------------------------------------------------


def _value(env: Mapping[str, str], key: str) -> str:
    """Return the trimmed value for ``key``, or ``""`` if unset/blank."""
    raw = env.get(key)
    return raw.strip() if raw is not None else ""


def _require(env: Mapping[str, str], key: str) -> str:
    value = _value(env, key)
    if not value:
        msg = f"{key} is required"
        raise ConfigError(msg)
    return value


def _value_aliased(env: Mapping[str, str], key: str, alias: str) -> str:
    """Return the trimmed value for ``key``, falling back to ``alias``.

    The canonical ``key`` takes precedence: its non-blank value wins even when
    ``alias`` is also set. A present-but-blank canonical value is treated as unset
    and falls through to ``alias`` (so a stray empty canonical key in the .env does
    not mask a populated provisioner alias). Returns ``""`` when neither is set.
    """
    return _value(env, key) or _value(env, alias)


def _require_host(env: Mapping[str, str]) -> str:
    """Resolve the gateway host from the canonical key or its provisioning alias.

    ``HERMES_SIP_HOST`` is canonical; ``HERMES_SIP_SERVER_HOST`` (the name the
    1Password provisioner emits) is the fallback so a first live launch from the
    sanctioned secret registers (runbook 0001). The error names BOTH keys so the
    operator knows either is accepted.
    """
    host = _value_aliased(env, _HOST_KEY, _SERVER_HOST_KEY)
    if not host:
        msg = f"{_HOST_KEY} (or {_SERVER_HOST_KEY}) is required"
        raise ConfigError(msg)
    return host


def _parse_transport(env: Mapping[str, str]) -> str:
    token = _value(env, _TRANSPORT_KEY).lower() or _DEFAULT_TRANSPORT
    if token not in _VIA_TRANSPORT:
        allowed = ", ".join(sorted(_VIA_TRANSPORT))
        msg = f"{_TRANSPORT_KEY} must be one of {{{allowed}}}, got {token!r}"
        raise ConfigError(msg)
    return token


def _parse_port(env: Mapping[str, str], transport: str) -> int:
    # Port resolution is transport-aware. HERMES_SIP_TLS_PORT is the provisioner's
    # SIP-TLS port and applies ONLY on the tls transport (there is no symmetric wss
    # alias). A real GDMS/Grandstream provisioner exports BOTH HERMES_SIP_PORT=5060
    # (the plain/UDP SIP port) and HERMES_SIP_TLS_PORT=5061 (the SIP-TLS port); on tls
    # the TLS handshake must target the TLS port, so for tls the precedence is
    # HERMES_SIP_TLS_PORT > HERMES_SIP_PORT > default(5061). (Preferring the canonical
    # cleartext 5060 made the TLS handshake hit the plain port -> ConnectionReset and
    # registration never started; confirmed live by forcing 5061 -> 401 challenge.)
    # On wss the TLS alias is irrelevant and is not consulted: HERMES_SIP_PORT > 443.
    # The error names the key the value actually came from so a malformed alias points
    # the operator at the right variable; a blank candidate falls through to the next.
    candidates: list[tuple[str, str]] = []
    if transport == "tls":
        candidates.append((_TLS_PORT_KEY, _value(env, _TLS_PORT_KEY)))
    candidates.append((_PORT_KEY, _value(env, _PORT_KEY)))

    source_key, raw = _PORT_KEY, _DEFAULT_PORT[transport]
    for key, value in candidates:
        if value:
            source_key, raw = key, value
            break

    port = _parse_int(raw, source_key)
    if not _MIN_PORT <= port <= _MAX_PORT:
        msg = f"{source_key} must be in [{_MIN_PORT}, {_MAX_PORT}], got {port}"
        raise ConfigError(msg)
    return port


def _parse_expires(env: Mapping[str, str]) -> int:
    raw = _value(env, _EXPIRES_KEY)
    if not raw:
        return _DEFAULT_EXPIRES
    expires = _parse_int(raw, _EXPIRES_KEY)
    if expires <= 0:
        msg = f"{_EXPIRES_KEY} must be positive, got {expires}"
        raise ConfigError(msg)
    return expires


def _parse_int(raw: str, key: str) -> int:
    if not _INDEX_RE.fullmatch(raw):
        msg = f"{key} must be a non-negative integer, got {raw!r}"
        raise ConfigError(msg)
    return int(raw)


def _parse_positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    """Parse ``key`` as a strictly-positive finite float, defaulting when unset.

    Rejects non-numeric, NaN/inf, and non-positive values fail-fast (rule 37) — a
    zero/negative drain timeout would defeat the graceful drain it configures, and
    NaN slips past a naive ``> 0`` test, so finiteness is checked explicitly.

    Raises:
        ConfigError: If the value is non-numeric, NaN/inf, or ``<= 0``.
    """
    raw = _value(env, key)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{key} must be a positive number, got {raw!r}"
        raise ConfigError(msg) from exc
    if not math.isfinite(value) or value <= 0:
        msg = f"{key} must be a positive finite number, got {raw!r}"
        raise ConfigError(msg)
    return value


def parse_keepalive_interval(env: Mapping[str, str]) -> float:
    """Parse ``HERMES_VOIP_KEEPALIVE_INTERVAL`` as a strictly-positive finite float.

    Returns the default (30.0 s) when the key is unset or blank. Rejects zero,
    negative, NaN, and infinite values fail-fast (rule 37) so a misconfigured
    keepalive surfaces at connect()/startup rather than mid-call when the TLS
    session goes silent unexpectedly.

    Args:
        env: The process/platform env mapping (e.g. ``PlatformConfig.extra``).

    Returns:
        The validated keepalive interval in seconds.

    Raises:
        ConfigError: If the value is non-numeric, NaN/inf, or ``<= 0``.
    """
    return _parse_positive_float(
        env, _KEEPALIVE_INTERVAL_KEY, _DEFAULT_KEEPALIVE_INTERVAL
    )


# --- media / feature field parsing ------------------------------------------


def _optional(env: Mapping[str, str], key: str) -> str | None:
    """Return the trimmed value for ``key``, or ``None`` if unset/blank.

    A present-but-blank value (``""`` or whitespace) collapses to ``None`` so an
    accidentally-empty override reads as "unset" rather than an empty string.
    """
    value = _value(env, key)
    return value or None


def _value_lower(env: Mapping[str, str], key: str) -> str:
    """Return the trimmed, lower-cased value for ``key``, or ``""`` if unset."""
    return _value(env, key).lower()


def _parse_enum(
    env: Mapping[str, str],
    key: str,
    allowed: frozenset[str],
    default: str,
) -> str:
    """Parse ``key`` as a lower-cased token constrained to ``allowed``."""
    token = _value_lower(env, key) or default
    if token not in allowed:
        choices = ", ".join(sorted(allowed))
        msg = f"{key} must be one of {{{choices}}}, got {token!r}"
        raise ConfigError(msg)
    return token


def _parse_deny_mode(env: Mapping[str, str]) -> DenyMode:
    """Parse ``HERMES_VOIP_DENY_MODE`` into the validated :data:`DenyMode` Literal.

    Unset → the Phase-1 default ``reject`` (hard 603). An invalid value raises
    :class:`ConfigError` naming the env var (rule 37 — a misconfigured deny policy
    fails loud, never silently defaults). The value is narrowed to the Literal here
    so the typed config field carries no off-Literal value.
    """
    mode = _value_lower(env, _DENY_MODE_KEY) or _DEFAULT_DENY_MODE
    if mode == "reject":
        return "reject"
    if mode == "decline":
        return "decline"
    choices = ", ".join(sorted(_DENY_MODES))
    msg = f"{_DENY_MODE_KEY} must be one of {{{choices}}}, got {mode!r}"
    raise ConfigError(msg)


def _parse_positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    """Parse ``key`` as a strictly-positive integer, defaulting when unset."""
    raw = _value(env, key)
    if not raw:
        return default
    value = _parse_int(raw, key)
    if value <= 0:
        msg = f"{key} must be positive, got {value}"
        raise ConfigError(msg)
    return value


def _parse_non_negative_int(env: Mapping[str, str], key: str, default: int) -> int:
    """Parse ``key`` as a ``>= 0`` integer, defaulting when unset.

    Unlike :func:`_parse_positive_int`, ``0`` is accepted (e.g. a barge-in tail of
    0 ms means "disarm the instant TTS ends"). The shared ``_parse_int`` already
    rejects negatives (its regex matches only non-negative integers), so a
    malformed or negative value raises :class:`ConfigError`.
    """
    raw = _value(env, key)
    if not raw:
        return default
    return _parse_int(raw, key)


def _parse_optional_positive_int(env: Mapping[str, str], key: str) -> int | None:
    """Parse ``key`` as a strictly-positive integer, or ``None`` if unset."""
    raw = _value(env, key)
    if not raw:
        return None
    value = _parse_int(raw, key)
    if value <= 0:
        msg = f"{key} must be positive, got {value}"
        raise ConfigError(msg)
    return value


def _parse_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    """Parse ``key`` as a boolean from common spellings, defaulting when unset."""
    raw = _value_lower(env, key)
    if not raw:
        return default
    if raw in _TRUE_TOKENS:
        return True
    if raw in _FALSE_TOKENS:
        return False
    truthy = ", ".join(sorted(_TRUE_TOKENS))
    falsy = ", ".join(sorted(_FALSE_TOKENS))
    msg = f"{key} must be a boolean ({truthy} / {falsy}), got {raw!r}"
    raise ConfigError(msg)


def _parse_optional_bool(env: Mapping[str, str], key: str) -> bool | None:
    """Parse ``key`` as a boolean, or ``None`` when unset (no default applied).

    Unlike :func:`_parse_bool` there is no fallback value: an unset knob stays
    ``None`` so a downstream provider can supply its own default. A present-but-
    unrecognised spelling still raises.
    """
    if not _value(env, key):
        return None
    return _parse_bool(env, key, default=False)


def _parse_optional_unit_float(env: Mapping[str, str], key: str) -> float | None:
    """Parse ``key`` as a float in ``[0.0, 1.0]``, or ``None`` when unset.

    NaN/inf and out-of-range values raise (NaN slips past a naive ``lo <= x <= hi``
    test, so :func:`_finite_in_range` rejects it explicitly). Used by the ElevenLabs
    voice-tuning knobs, whose ``voice_settings`` floats are 0.0-1.0.

    Raises:
        ConfigError: If the value is non-numeric, NaN/inf, or outside ``[0, 1]``.
    """
    raw = _value(env, key)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{key} must be a number in [0.0, 1.0], got {raw!r}"
        raise ConfigError(msg) from exc
    if not _finite_in_range(value, _MIN_TTS_SETTING, _MAX_TTS_SETTING):
        msg = (
            f"{key} must be a finite value in "
            f"[{_MIN_TTS_SETTING}, {_MAX_TTS_SETTING}], got {raw!r}"
        )
        raise ConfigError(msg)
    return value


def _parse_optional_bounded_int(
    env: Mapping[str, str], key: str, lo: int, hi: int
) -> int | None:
    """Parse ``key`` as an int within ``[lo, hi]``, or ``None`` when unset.

    Raises:
        ConfigError: If the value is not an integer or is outside ``[lo, hi]``.
    """
    raw = _value(env, key)
    if not raw:
        return None
    value = _parse_int(raw, key)
    if not lo <= value <= hi:
        msg = f"{key} must be in [{lo}, {hi}], got {value}"
        raise ConfigError(msg)
    return value


def _parse_bounded_int(
    env: Mapping[str, str], key: str, lo: int, hi: int, default: int
) -> int:
    """Parse ``key`` as an int within ``[lo, hi]``, defaulting when unset.

    Unlike :func:`_parse_optional_bounded_int` (which returns ``None`` when unset),
    a missing value falls back to ``default``. A present value outside ``[lo, hi]``
    raises (fail-fast, not silently clamped) — the operator's intent to disable a
    safety watchdog with ``0`` is rejected here, not quietly accepted.

    Raises:
        ConfigError: If the value is not an integer or is outside ``[lo, hi]``.
    """
    raw = _value(env, key)
    if not raw:
        return default
    value = _parse_int(raw, key)
    if not lo <= value <= hi:
        msg = f"{key} must be in [{lo}, {hi}], got {value}"
        raise ConfigError(msg)
    return value


def _parse_tts_fallback(env: Mapping[str, str], tts_provider: str) -> str | None:
    """Resolve the TTS failover provider token (ADR-0025), or ``None`` when off.

    Resolution:

    * ``HERMES_VOIP_TTS_FALLBACK`` set to ``none`` (any case) → ``None`` (off).
    * set to a provider token → that token (lower-cased).
    * **unset/blank** → the primary-dependent default: a CLOUD primary
      (``elevenlabs``/``cartesia``/``aura2``) gets ``sherpa-kokoro`` (so a remote
      failure recovers locally); a self-host primary gets ``None`` (already safe).

    The token's membership/uniqueness-vs-primary is validated by
    :meth:`MediaConfig._validate_tts_fallback`; this resolves the value only.
    """
    raw = _value_lower(env, _TTS_FALLBACK_KEY)
    if not raw:
        # Unset → cloud primary defaults to the self-host fallback, else off.
        if tts_provider in _CLOUD_TTS_PROVIDERS:
            return _DEFAULT_CLOUD_TTS_FALLBACK
        return None
    if raw == _TTS_FALLBACK_NONE:
        return None
    return raw


def _finite_in_range(value: float, lo: float, hi: float) -> bool:
    """True iff ``value`` is finite (not NaN/inf) and within ``[lo, hi]``."""
    return math.isfinite(value) and lo <= value <= hi


def _parse_greeting(env: Mapping[str, str]) -> str:
    """Parse the opening greeting, distinguishing 'unset' from 'explicitly empty'.

    Unlike :func:`_optional` (which collapses a blank value to ``None``), the
    greeting must tell apart two intents: *unset* → use the friendly
    :data:`DEFAULT_GREETING`; *present-but-empty* (``""`` or whitespace) → opt
    out of any greeting (returns ``""``). A set value is trimmed.
    """
    raw = env.get(_GREETING_KEY)
    if raw is None:  # key absent → friendly default
        return DEFAULT_GREETING
    return raw.strip()  # present (incl. empty/whitespace) → verbatim, trimmed


def _parse_language(env: Mapping[str, str]) -> str:
    """Parse and validate the active conversation language (ADR-0054, ADR-0084).

    Lower-cased; defaults to :data:`_DEFAULT_LANGUAGE` when unset.  A structurally
    malformed code (not a well-formed BCP-47 primary subtag) is rejected fail-fast so
    a typo is caught at startup.  A valid code that has no built-in comfort-filler
    phrase set is accepted; ``_parse_comfort_filler_phrases`` falls back to the
    English default phrase set for such languages (ADR-0084).
    """
    token = _value_lower(env, _LANGUAGE_KEY) or _DEFAULT_LANGUAGE
    if not _LANGUAGE_RE.match(token):
        msg = (
            f"{_LANGUAGE_KEY} must be a well-formed BCP-47 language tag "
            f"(e.g. 'en', 'fr', 'pt-BR'), got {token!r}"
        )
        raise ConfigError(msg)
    return token


def _parse_comfort_filler_phrases(
    env: Mapping[str, str], language: str
) -> tuple[str, ...]:
    """Parse the `|`-separated comfort-filler phrase set (ADR-0030, ADR-0054, ADR-0084).

    Each member is trimmed; empty members (e.g. from a trailing or doubled ``|``)
    are dropped. An unset or all-blank value falls back to the selected *language*'s
    built-in set (:data:`_COMFORT_FILLER_PHRASES_BY_LANGUAGE`), or to the English
    default when the language has no built-in set (ADR-0084), so a missing-phrase-set
    language never yields a filler with no phrase to speak. An explicit set always
    wins (it overrides both the language default and the English fallback). The result
    is always non-empty. ``language`` is pre-validated by :func:`_parse_language`.
    """
    default = _COMFORT_FILLER_PHRASES_BY_LANGUAGE.get(
        language, _DEFAULT_COMFORT_FILLER_PHRASES
    )
    raw = _value(env, _COMFORT_FILLER_PHRASES_KEY)
    if not raw:
        return default
    phrases = tuple(
        part.strip() for part in raw.split(_COMFORT_FILLER_PHRASE_SEP) if part.strip()
    )
    return phrases or default


def _parse_no_input_reprompt_phrases(env: Mapping[str, str]) -> tuple[str, ...]:
    """Parse the ``|``-separated no-input reprompt phrase set (ADR-0057).

    Each member is trimmed; empty members (from a trailing or doubled ``|``) are
    dropped. An unset or all-blank value falls back to
    :data:`_DEFAULT_NO_INPUT_REPROMPT_PHRASES` — the exact phrases hardcoded in
    ``call_loop.py`` — so behaviour is UNCHANGED when the env var is unset. An
    explicit set always wins. The result is always non-empty.
    """
    raw = _value(env, _NO_INPUT_REPROMPT_PHRASES_KEY)
    if not raw:
        return _DEFAULT_NO_INPUT_REPROMPT_PHRASES
    phrases = tuple(
        part.strip() for part in raw.split(_COMFORT_FILLER_PHRASE_SEP) if part.strip()
    )
    return phrases or _DEFAULT_NO_INPUT_REPROMPT_PHRASES


def _parse_refuse_decline_phrases(
    env: Mapping[str, str], language: str
) -> tuple[str, ...]:
    """Parse the ``|``-separated guard-REFUSE safe-decline phrase set (ADR-0076/0084).

    Each member is trimmed; empty members (from a trailing or doubled ``|``) are
    dropped. An unset or all-blank value falls back to the selected *language*'s
    built-in set (:data:`_REFUSE_DECLINE_PHRASES_BY_LANGUAGE`), or to the English
    default when the language has no built-in set (ADR-0084), so a missing-phrase-set
    language never re-strands a false-positived caller in silence. An explicit set
    always wins. The result is always non-empty. ``language`` is pre-validated by
    :func:`_parse_language`.
    """
    default = _REFUSE_DECLINE_PHRASES_BY_LANGUAGE.get(
        language, _DEFAULT_REFUSE_DECLINE_PHRASES
    )
    raw = _value(env, _REFUSE_DECLINE_PHRASES_KEY)
    if not raw:
        return default
    phrases = tuple(
        part.strip() for part in raw.split(_COMFORT_FILLER_PHRASE_SEP) if part.strip()
    )
    return phrases or default


def _parse_goodbye_phrase(env: Mapping[str, str]) -> str:
    """Parse ``HERMES_VOIP_GOODBYE_PHRASE``, defaulting when unset or blank.

    Unlike the greeting (which distinguishes 'unset' from 'explicitly empty'),
    a blank/whitespace goodbye phrase falls back to the default — an empty goodbye
    is a misconfiguration (the goodbye speech path is always non-trivially short).
    The operator disables the goodbye entirely via ``HERMES_VOIP_GOODBYE=false``,
    not by blanking the phrase.
    """
    raw = _value(env, _GOODBYE_PHRASE_KEY)
    return raw or _DEFAULT_GOODBYE_PHRASE


def _parse_decline_phrase(env: Mapping[str, str]) -> str:
    """Parse ``HERMES_VOIP_DECLINE_PHRASE``, defaulting when unset or blank.

    The polite-decline line spoken on a ``deny_mode=decline`` declined caller
    (ADR-0020 §5/§6). A blank/whitespace value falls back to the built-in default —
    answering a declined caller only to play dead air is a misconfiguration, so the
    spoken line is always non-trivially short. The operator keeps the hard-603 posture
    via ``HERMES_VOIP_DENY_MODE=reject`` (the default), not by blanking this phrase.
    """
    raw = _value(env, _DECLINE_PHRASE_KEY)
    return raw or _DEFAULT_DECLINE_PHRASE


def _parse_ice_stun_urls(env: Mapping[str, str]) -> tuple[str, ...]:
    """Parse ``HERMES_VOIP_ICE_STUN_URLS`` (ADR-0032; default revised ADR-0043).

    Precedence: the key being **unset** yields :data:`DEFAULT_ICE_STUN_URLS` (the
    public dual-stack list, so a NAT'd deployment gathers a srflx out of the box);
    an **explicit** value (including an empty / all-blank string) is honoured
    verbatim, so setting it empty disables STUN (host-only ICE). Members are
    trimmed; blank members (trailing/doubled comma) are dropped. No ``stun:`` scheme
    validation here — the ICE layer (:func:`hermes_voip.media.ice._parse_stun_url`)
    validates each URL when it builds the agent, so a bad URL fails loudly at use.
    """
    raw = env.get(_ICE_STUN_URLS_KEY)
    if raw is None:
        return DEFAULT_ICE_STUN_URLS  # unset -> public default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_ice_turn(
    env: Mapping[str, str],
) -> tuple[tuple[str, ...], str | None, str | None]:
    """Parse the TURN relay config (ADR-0034): URLs + long-term credentials.

    Returns ``(turn_urls, username, password)``. Each URL member is trimmed; blank
    members are dropped (same shape as STUN). When ``turn_urls`` is non-empty, both
    a username and a password are REQUIRED (RFC 8656 §9.2): a credential-less TURN
    URL would gather no relay candidate, which rule 27 forbids — so a missing
    credential is a loud :class:`ConfigError`, not a silent no-op.

    Raises:
        ConfigError: If TURN URLs are set but the username or password is missing.
    """
    raw = _value(env, _ICE_TURN_URLS_KEY)
    urls = tuple(part.strip() for part in raw.split(",") if part.strip()) if raw else ()
    username = _value(env, _ICE_TURN_USERNAME_KEY) or None
    password = _value(env, _ICE_TURN_PASSWORD_KEY) or None
    if urls and (username is None or password is None):
        msg = (
            f"{_ICE_TURN_URLS_KEY} is set but a TURN credential is missing: both "
            f"{_ICE_TURN_USERNAME_KEY} and {_ICE_TURN_PASSWORD_KEY} are required "
            "(RFC 8656 §9.2 long-term credentials)."
        )
        raise ConfigError(msg)
    return urls, username, password


def _parse_tone_secs(env: Mapping[str, str]) -> float:
    """Parse ``HERMES_VOIP_TEST_TONE`` as a non-negative float (seconds).

    Absent or ``"0"`` → ``0.0`` (tone disabled, normal operation). A positive
    value enables the diagnostic tone path for that many seconds.

    Raises:
        ConfigError: If the value is set but not a valid non-negative number.
    """
    raw = _value(env, _TEST_TONE_KEY)
    if not raw:
        return _DEFAULT_TEST_TONE_SECS
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{_TEST_TONE_KEY} must be a number of seconds, got {raw!r}"
        raise ConfigError(msg) from exc
    if not math.isfinite(value) or value < 0:
        msg = f"{_TEST_TONE_KEY} must be a non-negative number, got {raw!r}"
        raise ConfigError(msg)
    return value


def _parse_aec_mu(env: Mapping[str, str]) -> float:
    """Parse the NLMS step ``HERMES_VOIP_AEC_MU`` as a float in the open ``(0, 2)``.

    ``0`` never adapts and ``>= 2`` diverges, so both bounds are exclusive (NaN/inf
    are rejected too). Absent → the default. ADR-0033.

    Raises:
        ConfigError: If the value is non-numeric, NaN/inf, or outside ``(0, 2)``.
    """
    raw = _value(env, _AEC_MU_KEY)
    if not raw:
        return _DEFAULT_AEC_MU
    try:
        value = float(raw)
    except ValueError as exc:
        msg = (
            f"{_AEC_MU_KEY} must be a number in "
            f"({_MIN_AEC_MU}, {_MAX_AEC_MU}), got {raw!r}"
        )
        raise ConfigError(msg) from exc
    if not math.isfinite(value) or not _MIN_AEC_MU < value < _MAX_AEC_MU:
        msg = (
            f"{_AEC_MU_KEY} must be a finite number in the open interval "
            f"({_MIN_AEC_MU}, {_MAX_AEC_MU}), got {raw!r}"
        )
        raise ConfigError(msg)
    return value


def _parse_vad_threshold(env: Mapping[str, str]) -> float:
    """Parse the VAD threshold as a finite float in ``[0.0, 1.0]``."""
    raw = _value(env, _VAD_THRESHOLD_KEY)
    if not raw:
        return _DEFAULT_VAD_THRESHOLD
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{_VAD_THRESHOLD_KEY} must be a number, got {raw!r}"
        raise ConfigError(msg) from exc
    if not _finite_in_range(value, _MIN_VAD_THRESHOLD, _MAX_VAD_THRESHOLD):
        msg = (
            f"{_VAD_THRESHOLD_KEY} must be a finite value in "
            f"[{_MIN_VAD_THRESHOLD}, {_MAX_VAD_THRESHOLD}], got {raw!r}"
        )
        raise ConfigError(msg)
    return value


# --- extension parsing ------------------------------------------------------


def _parse_extensions(env: Mapping[str, str]) -> tuple[ExtensionConfig, ...]:
    # Any bare credential key — not just the extension — signals the single
    # scheme, so a stray HERMES_SIP_PASSWORD/USERNAME beside the indexed scheme
    # is caught as a mix (a likely typo) rather than silently ignored.
    has_bare = any(
        key in env for key in (_BARE_EXTENSION, _BARE_PASSWORD, _BARE_USERNAME)
    )
    indexed_indices = _indexed_indices(env)

    if has_bare and indexed_indices:
        msg = (
            f"{_BARE_EXTENSION}/{_BARE_PASSWORD} (single) and "
            f"{_EXTENSION_PREFIX}<n> (indexed) schemes must not be combined; "
            "use one"
        )
        raise ConfigError(msg)

    extensions: tuple[ExtensionConfig, ...]
    if has_bare:
        extensions = (_parse_bare_extension(env),)
    else:
        extensions = _parse_indexed_extensions(env, indexed_indices)

    if not extensions:
        msg = (
            f"no extension configured: set {_BARE_EXTENSION} (+{_BARE_PASSWORD}) "
            f"or {_EXTENSION_PREFIX}<n> (+{_PASSWORD_PREFIX}<n>)"
        )
        raise ConfigError(msg)

    _reject_duplicate_numbers(extensions)
    return extensions


def _parse_bare_extension(env: Mapping[str, str]) -> ExtensionConfig:
    extension = _require(env, _BARE_EXTENSION)
    password = _require(env, _BARE_PASSWORD)
    username = _value(env, _BARE_USERNAME) or extension
    return ExtensionConfig(
        index=0, extension=extension, username=username, password=password
    )


def _indexed_indices(env: Mapping[str, str]) -> tuple[int, ...]:
    """Collect and validate the integer indices from ``HERMES_SIP_EXTENSION_<n>``.

    A non-integer suffix is malformed; an indexed ``PASSWORD``/``USERNAME``
    without a matching ``EXTENSION`` is an orphan. Both raise.
    """
    ext_indices = _suffix_indices(env, _EXTENSION_PREFIX)
    pwd_indices = _suffix_indices(env, _PASSWORD_PREFIX)
    user_indices = _suffix_indices(env, _USERNAME_PREFIX)

    orphans = (pwd_indices | user_indices) - ext_indices
    if orphans:
        joined = ", ".join(str(i) for i in sorted(orphans))
        msg = (
            f"indexed {_PASSWORD_PREFIX}/{_USERNAME_PREFIX} without a matching "
            f"{_EXTENSION_PREFIX} for index(es): {joined}"
        )
        raise ConfigError(msg)
    return tuple(sorted(ext_indices))


def _suffix_indices(env: Mapping[str, str], prefix: str) -> set[int]:
    indices: set[int] = set()
    for key in env:
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        if not _INDEX_RE.fullmatch(suffix):
            msg = f"{key}: index suffix must be a non-negative integer"
            raise ConfigError(msg)
        index = int(suffix)
        if index in indices:
            msg = f"duplicate index {index} from {prefix}<n> keys"
            raise ConfigError(msg)
        indices.add(index)
    return indices


def _parse_indexed_extensions(
    env: Mapping[str, str], indices: tuple[int, ...]
) -> tuple[ExtensionConfig, ...]:
    configs: list[ExtensionConfig] = []
    for index in indices:
        extension = _require(env, f"{_EXTENSION_PREFIX}{index}")
        password = _require(env, f"{_PASSWORD_PREFIX}{index}")
        username = _value(env, f"{_USERNAME_PREFIX}{index}") or extension
        configs.append(
            ExtensionConfig(
                index=index,
                extension=extension,
                username=username,
                password=password,
            )
        )
    return tuple(configs)


def _reject_duplicate_numbers(extensions: tuple[ExtensionConfig, ...]) -> None:
    seen: set[str] = set()
    for ext in extensions:
        if ext.extension in seen:
            msg = f"duplicate extension number {ext.extension!r}"
            raise ConfigError(msg)
        seen.add(ext.extension)


def _resolve_default_index(
    env: Mapping[str, str], extensions: tuple[ExtensionConfig, ...]
) -> int:
    chosen = _value(env, _DEFAULT_EXTENSION_KEY)
    if not chosen:
        # Lowest index wins; extensions are sorted ascending.
        return extensions[0].index
    for ext in extensions:
        if ext.extension == chosen:
            return ext.index
    available = ", ".join(ext.extension for ext in extensions)
    msg = (
        f"{_DEFAULT_EXTENSION_KEY}={chosen!r} is not a configured extension "
        f"(have: {available})"
    )
    raise ConfigError(msg)
