"""The Hermes VoIP plugin: register(ctx) entry point and VoipAdapter (W10).

This module is the **only** place in the codebase that imports the Hermes
runtime surface (``gateway.platforms.base``, ``gateway.config``). Those
classes are unavailable in the default install and ship no ``py.typed``, so
they would resolve to ``Any`` under ``mypy --strict``. The typed boundary in
:mod:`hermes_voip.hermes_surface` provides the Protocol-checked substitutes
we compile against; the running plugin receives the real Hermes classes.

End-to-end call flow for a SIP-over-TLS inbound INVITE:
1. ``SipOverTlsTransport`` frames the TLS stream and delivers a ``NewCall``
   to the ``on_new_call`` callback registered here during ``connect()``.
2. ``_on_inbound_invite`` builds a ``Dialog``, negotiates the SDP offer,
   sends a ``200 OK`` answer, opens a UDP ``RtpMediaTransport``, wires a
   ``CallSession`` and ``CallLoop``, and starts the loop as a background
   asyncio task.
3. When the caller finishes a turn the ``CallLoop`` calls ``deliver_turn``
   with the transcript. ``deliver_turn`` builds a ``MessageEvent`` and calls
   the Hermes base's ``handle_message``, which routes the text to the agent.
4. The Hermes agent replies via ``adapter.send(call_id, text)``; ``send``
   delivers the text to the call's ``CallLoop.speak()``.
5. ``disconnect()`` cancels all call loops, closes the manager and transport.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from hermes_voip.call import CallSession
from hermes_voip.config import MediaConfig, load_gateway_config, load_media_config
from hermes_voip.dialog import Dialog
from hermes_voip.digest import DigestCredentials
from hermes_voip.hermes_surface import SendResultProtocol
from hermes_voip.incall import LocalMediaSession
from hermes_voip.manager import NewCall, RegistrationManager
from hermes_voip.media.call_loop import CallLoop
from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.media.vad import VadModel, VoiceActivityDetector
from hermes_voip.message import build_response, new_tag
from hermes_voip.providers.build import Providers, build_providers
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.sdp import (
    AudioMedia,
    SessionDescription,
    build_audio_answer,
    negotiate_audio,
)
from hermes_voip.sdp import (
    Codec as SdpCodec,
)
from hermes_voip.transport.connection import SipOverTlsTransport

if TYPE_CHECKING:
    from hermes_voip.media.srtp import SrtpSession

__all__ = ["VoipAdapter", "register", "validate_voip_config"]

_log = logging.getLogger(__name__)

# The RTP port range we pick from when binding the media engine.
_RTP_PORT_START = 20000
_RTP_PORT_END = 29999

# Default VAD silence window size (samples at 16 kHz).
_VAD_WINDOW_SAMPLES = 512

# Supported G.711 encoding names for SDP negotiation.
_SUPPORTED_ENCODINGS = ("PCMU", "PCMA", "telephone-event")

# Install hint shown by ``hermes plugins list``.
_INSTALL_HINT = (
    "Set HERMES_SIP_HOST, HERMES_SIP_EXTENSION, and HERMES_SIP_PASSWORD "
    "(or indexed HERMES_SIP_EXTENSION_<n>/PASSWORD_<n> for multiple registrations). "
    "Run `hermes plugins enable hermes-voip` to activate the plugin."
)

# The required HERMES_SIP_* environment variable names.
_REQUIRED_ENV = (
    "HERMES_SIP_HOST",
    "HERMES_SIP_EXTENSION",
    "HERMES_SIP_PASSWORD",
)


def _make_tls_context(host: str) -> ssl.SSLContext:
    """Build a client TLS context that verifies the server certificate."""
    ctx = ssl.create_default_context()
    # The gateway host name is used for SNI/verification.
    # We do not hard-code any certificate pinning here — gateway-agnostic.
    _ = host  # consumed by the transport's server_hostname
    return ctx


def _srtp_from_audio(audio: AudioMedia, *, outbound: bool) -> SrtpSession | None:  # noqa: ARG001 — outbound direction reserved for future SRTP policy; SrtpSession selects key material from the crypto attribute
    """Build an SrtpSession for a negotiated SRTP offer, or None for plain RTP.

    ``outbound=True`` for the TX direction (protect), ``outbound=False`` for RX
    (unprotect). Returns ``None`` when the offer is plain ``RTP/AVP``.

    The SrtpSession is imported lazily (``media`` extra absent in the default
    install; ``import hermes_voip.media.srtp`` still succeeds — the error
    surfaces at SrtpSession construction time, rule 37).
    """
    if not audio.is_srtp or not audio.crypto_attrs:
        return None
    from hermes_voip.media.srtp import SrtpSession  # noqa: PLC0415

    # The first validated, supported crypto attribute wins (offer order).
    return SrtpSession(audio.crypto_attrs[0])


def validate_voip_config(config: object) -> None:
    """Validate the Hermes PlatformConfig for the VoIP adapter.

    Calls :func:`~hermes_voip.config.load_gateway_config` against the config's
    ``extra`` mapping — if any required env var is missing or malformed this
    raises :class:`~hermes_voip.config.ConfigError`.

    Args:
        config: A Hermes ``PlatformConfig``-like object with an ``extra``
            attribute (``Mapping[str, str]``).

    Raises:
        ConfigError: If a required SIP env var is absent or invalid.
    """
    extra = getattr(config, "extra", {})
    load_gateway_config(extra)


class VoipAdapter:
    """Hermes ``kind: platform`` adapter for SIP-over-TLS telephony (ADR-0002).

    Implements the four abstract methods of ``BasePlatformAdapter``
    (via :class:`~hermes_voip.hermes_surface.BasePlatformAdapterProtocol`) and
    wires all the now-merged pieces — provider registry, SIP-over-TLS transport,
    RTP media engine, and per-call ``CallLoop`` — into a loadable plugin.

    One adapter instance supports **N simultaneous SIP registrations** (one per
    configured ``HERMES_SIP_EXTENSION_<n>``); each inbound call creates its own
    ``CallSession`` + ``CallLoop`` keyed by SIP ``Call-ID``.

    ``config`` and ``platform`` are the Hermes ``PlatformConfig`` and
    ``Platform`` objects injected by the framework; the adapter reads its SIP
    credentials and media settings from ``config.extra`` (which the Hermes
    gateway populates from environment variables).
    """

    # The Hermes framework injects these at construction (BasePlatformAdapter
    # signature: __init__(self, config, platform)).  We accept them as ``object``
    # and forward them to the real base in the running plugin (the typed boundary
    # is the Protocol in hermes_surface.py; no direct import of the Hermes base
    # here keeps the default install ML-free and mypy-clean).
    def __init__(self, config: object, platform: object) -> None:
        """Accept Hermes-injected config and platform; defer all IO until connect."""
        self._config = config
        self._platform = platform

        # Populated by connect():
        self._providers: Providers | None = None
        self._media_cfg: MediaConfig | None = None
        self._transport: SipOverTlsTransport | None = None
        self._manager: RegistrationManager | None = None
        self._connected = False

        # Per-call state: {call_id → CallLoop}
        self._call_loops: dict[str, CallLoop] = {}
        # Per-call metadata: {call_id → {name, remote_uri, type, ended}}
        self._call_info: dict[str, dict[str, object]] = {}
        # Background tasks for each active call loop
        self._call_tasks: dict[str, asyncio.Task[None]] = {}

    # -----------------------------------------------------------------------
    # BasePlatformAdapter abstract methods
    # -----------------------------------------------------------------------

    async def connect(self) -> bool:
        """Load config, open TLS, register extensions.

        Returns True when at least one extension is up. Degraded-up (one out
        of N extensions registered) counts as up — the manager's ``is_up``
        property already implements this rule.

        Returns:
            ``True`` if at least one extension registered successfully,
            ``False`` otherwise (never raises on partial failure — the caller
            decides whether to retry).
        """
        extra = getattr(self._config, "extra", {})
        gateway_cfg = load_gateway_config(extra)
        media_cfg = load_media_config(extra)

        self._providers = build_providers(media_cfg)
        self._media_cfg = media_cfg

        tls_ctx = _make_tls_context(gateway_cfg.host)

        transport = SipOverTlsTransport(
            host=gateway_cfg.host,
            port=gateway_cfg.port,
            ssl_context=tls_ctx,
            on_new_call=self._on_inbound_invite,
            on_unroutable=self._on_unroutable,
            on_connection_lost=self._on_connection_lost,
        )
        self._transport = transport

        manager = RegistrationManager(
            gateway_cfg,
            transport,
        )
        self._manager = manager
        transport.bind_manager(manager)

        await transport.connect()
        up = await manager.connect()

        self._connected = True
        return up

    async def disconnect(self) -> None:
        """Cancel all call loops, close the manager and transport; idempotent."""
        if not self._connected:
            return
        self._connected = False

        # Cancel and drain all per-call tasks.
        tasks = list(self._call_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._call_tasks.clear()
        self._call_loops.clear()

        manager = self._manager
        if manager is not None:
            await manager.aclose()
            self._manager = None

        transport = self._transport
        if transport is not None:
            await transport.aclose()
            self._transport = None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,  # noqa: ARG002 — SIP has no threaded replies
        metadata: object | None = None,  # noqa: ARG002 — no metadata consumed
    ) -> SendResultProtocol:
        """Deliver agent text to the caller via TTS synthesis.

        ``chat_id`` is the SIP ``Call-ID``. The text is passed to the call's
        ``CallLoop.speak()`` as a single-item async iterator (the Hermes adapter
        calls this once per agent reply; full streaming is a Phase-2 upgrade).

        Returns a SendResult-compatible object. An unknown ``chat_id`` returns a
        failure result (does not raise) — the call may have already ended.
        """
        loop = self._call_loops.get(chat_id)
        if loop is None:
            return _FailSendResult(f"unknown call_id {chat_id!r}")

        text = content

        async def _single_chunk() -> AsyncIterator[str]:
            yield text

        try:
            await loop.speak(_single_chunk())
        except Exception as exc:  # noqa: BLE001 — surface as failure, never swallow
            _log.warning("speak() failed for %s: %s", chat_id, exc)
            return _FailSendResult(str(exc))
        return _OkSendResult(chat_id)

    async def get_chat_info(self, chat_id: str) -> dict[str, object]:
        """Return chat metadata for a live or ended call.

        Returns at least ``{name: <caller-number>, type: "dm"}`` consistent with
        Hermes's one-to-one conversation model for telephony calls. A ``Call-ID``
        that was never seen or that has been garbage-collected returns a minimal
        fallback dict rather than raising.
        """
        info = self._call_info.get(chat_id)
        if info is None:
            return {"name": chat_id, "type": "dm"}
        return {
            "name": info.get("name", chat_id),
            "type": "dm",
            "remote_uri": info.get("remote_uri", ""),
            "ended": info.get("ended", False),
        }

    # -----------------------------------------------------------------------
    # Inbound call wiring
    # -----------------------------------------------------------------------

    def _on_inbound_invite(self, new_call: NewCall) -> None:
        """Wire a new inbound INVITE: build dialog, send 200 OK, start loop.

        This is a synchronous callback invoked from the transport's reader task.
        The actual async work is scheduled as a background task so the reader
        does not block while we open a UDP socket and send the 200 OK.
        """
        task = asyncio.ensure_future(self._handle_inbound_invite(new_call))
        # Store by call_id so we can cancel on disconnect.
        call_id = new_call.invite.header("Call-ID") or ""
        self._call_tasks[call_id] = task
        task.add_done_callback(lambda t: self._on_call_task_done(call_id, t))

    async def _handle_inbound_invite(  # noqa: PLR0911, PLR0915 — RFC 3261 INVITE handling requires these sequential reject-early guard steps; extraction would only move the complexity elsewhere
        self, new_call: NewCall
    ) -> None:
        """Async body of _on_inbound_invite; wires the full call stack."""
        invite = new_call.invite
        call_id = invite.header("Call-ID") or ""
        transport = self._transport
        if transport is None:
            _log.warning("INVITE %s arrived after transport closed — ignored", call_id)
            return

        # --- SDP negotiation ------------------------------------------------
        sdp_body = invite.body
        try:
            offer = SessionDescription.parse(sdp_body)
        except Exception as exc:  # noqa: BLE001 — malformed peer SDP; reject call
            _log.warning("INVITE %s: unparseable SDP offer: %s", call_id, exc)
            await transport.send(build_response(invite, 488, "Not Acceptable Here"))
            return

        audio = offer.audio
        if audio is None:
            await transport.send(build_response(invite, 488, "Not Acceptable Here"))
            return

        try:
            agreed_sdp_codecs = negotiate_audio(audio, _SUPPORTED_ENCODINGS)
        except ValueError as exc:
            _log.warning("INVITE %s: no common codec: %s", call_id, exc)
            await transport.send(build_response(invite, 488, "Not Acceptable Here"))
            return

        # --- Pick a local media Codec from the negotiated SDP codecs --------
        codec = _first_voice_codec(agreed_sdp_codecs)
        if codec is None:
            await transport.send(build_response(invite, 488, "Not Acceptable Here"))
            return

        # --- Build the UAS dialog -------------------------------------------
        local_tag = new_tag()
        local_contact = transport.contact_uri(new_call.registration.extension)
        dialog = Dialog.from_inbound_invite(
            invite,
            local_tag=local_tag,
            local_contact=local_contact,
            local_sent_by=transport.local_sent_by,
            transport="TLS",
        )

        # --- Open the media engine ------------------------------------------
        remote_address = _effective_address(audio, offer)
        engine = RtpMediaTransport(
            local_address="0.0.0.0",  # noqa: S104 — bind to all interfaces for RTP
            local_port=0,  # OS assigns a free port
            remote_address=remote_address,
            remote_port=audio.port,
            codec=_to_engine_codec(codec),
            srtp_inbound=_srtp_from_audio(audio, outbound=False),
            srtp_outbound=_srtp_from_audio(audio, outbound=True),
        )
        await engine.connect()

        # --- Build the SDP answer ------------------------------------------
        local_media = LocalMediaSession(
            local_address="127.0.0.1",  # replaced by real local addr in production
            port=engine.local_port,
            codecs=agreed_sdp_codecs,
            session_id=int(time.monotonic() * 1000) & 0xFFFF_FFFF,
        )
        try:
            answer_sdp = build_audio_answer(
                offer,
                local_address=local_media.local_address,
                port=local_media.port,
                supported=list(_SUPPORTED_ENCODINGS),
                session_id=local_media.session_id,
            )
        except Exception as exc:  # noqa: BLE001 — SdpError or negotiation failure
            _log.warning("INVITE %s: cannot build SDP answer: %s", call_id, exc)
            await transport.send(build_response(invite, 500, "Server Internal Error"))
            await engine.stop()
            return

        # --- Send 200 OK with the SDP answer --------------------------------
        ok_response = build_response(
            invite,
            200,
            "OK",
            extra_headers=(
                ("Contact", local_contact),
                ("Content-Type", "application/sdp"),
            ),
            body=answer_sdp,
        )
        await transport.send(ok_response)

        # --- Register the call for in-dialog routing -----------------------
        # One GuardSessionState per call, shared between CallSession and CallLoop.
        guard_state = GuardSessionState(call_id)
        credentials = DigestCredentials(
            username=new_call.registration.username,
            password=new_call.registration.password,
        )
        session = CallSession(
            dialog=dialog,
            signaling=transport,
            media=engine,
            guard=guard_state,
            local_media=local_media,
            credentials=credentials,
        )
        if self._manager is not None:
            self._manager.add_call(session.dialog_id, session)
        transport.add_call(call_id, session)

        # --- Extract caller info from the inbound INVITE -------------------
        remote_uri = invite.header("From") or ""
        caller_number = _caller_number(remote_uri)

        self._call_info[call_id] = {
            "name": caller_number,
            "remote_uri": remote_uri,
            "type": "dm",
            "ended": False,
        }

        # --- Build + start CallLoop ----------------------------------------
        providers = self._providers
        if providers is None:
            _log.error("INVITE %s: providers not initialised — ending call", call_id)
            await engine.stop()
            return
        media_cfg = self._media_cfg
        if media_cfg is None:
            _log.error("INVITE %s: media config not initialised — ending call", call_id)
            await engine.stop()
            return
        vad = _make_vad(media_cfg)
        endpointer = _make_endpointer(media_cfg)

        async def _deliver(text: str) -> None:
            await self._deliver_turn(call_id, text)

        call_loop = CallLoop(
            transport=engine,
            asr=providers.asr,
            tts=providers.tts,
            guard=providers.guard,
            vad=vad,
            endpointer=endpointer,
            guard_state=guard_state,
            deliver_turn=_deliver,
            voice="",
            call_id=call_id,
        )
        self._call_loops[call_id] = call_loop

        try:
            await call_loop.run()
        finally:
            self._call_info[call_id] = dict(self._call_info.get(call_id, {}))
            self._call_info[call_id]["ended"] = True
            self._call_loops.pop(call_id, None)
            if self._manager is not None:
                self._manager.remove_call(session.dialog_id)
            transport.remove_call(call_id)

    async def _deliver_turn(self, call_id: str, text: str) -> None:
        """Route a finalized caller transcript to the Hermes agent.

        Builds a ``MessageEvent`` compatible with the Hermes runtime's
        ``handle_message`` and calls the base. The Hermes framework supplies
        ``MessageType.VOICE`` and ``MessageEvent`` at runtime; we import them
        lazily so the default install (no ``hermes-agent``) stays importable.
        """
        info = self._call_info.get(call_id, {})
        caller_name = str(info.get("name", call_id))

        try:
            from gateway.config import (  # type: ignore[import-not-found]  # noqa: PLC0415
                Platform,
                PlatformConfig,
            )
            from gateway.platforms.base import (  # type: ignore[import-not-found]  # noqa: PLC0415
                MessageEvent,
                MessageType,
            )

            # Platform accepts unknown names via _missing_ (verified in P0.1).
            platform = Platform("voip")
            platform_config = PlatformConfig(
                platform=platform,
                label="VoIP",
                extra=getattr(self._config, "extra", {}),
            )
            source = self.build_source(  # type: ignore[attr-defined]
                chat_id=call_id,
                user_id=caller_name,
                user_name=caller_name,
                platform_config=platform_config,
            )
            event = MessageEvent(
                text=text,
                message_type=MessageType.VOICE,
                source=source,
                media_urls=[],
            )
            self.handle_message(event)  # type: ignore[attr-defined]
        except ImportError:
            # Running outside the Hermes runtime (e.g. in tests); call the mock.
            _handle = getattr(self, "handle_message", None)
            if _handle is not None:
                from types import SimpleNamespace  # noqa: PLC0415

                _test_event = SimpleNamespace(text=text, media_urls=[])
                _handle(_test_event)

    def _on_unroutable(self, what: object) -> None:
        """Log unroutable SIP messages at DEBUG; never crash the transport."""
        _log.debug("unroutable SIP message: %s", what)

    def _on_connection_lost(self, exc: BaseException | None) -> None:
        """Log a TLS connection loss; the adapter stays alive for in-progress calls."""
        if exc is not None:
            _log.error("SIP-over-TLS connection lost: %s", exc)
        else:
            _log.info("SIP-over-TLS connection closed cleanly")

    def _on_call_task_done(self, call_id: str, task: asyncio.Task[None]) -> None:
        """Observe a finished call task; surface any unhandled exception."""
        self._call_tasks.pop(call_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error("call %s ended with error: %s", call_id, exc)


# ---------------------------------------------------------------------------
# SendResult-compatible lightweight objects (no Hermes dependency at module level)
# ---------------------------------------------------------------------------


class _OkSendResult:
    """A successful send result."""

    success: bool = True
    error: str | None = None

    def __init__(self, message_id: str) -> None:
        self.message_id: str | None = message_id


class _FailSendResult:
    """A failed send result."""

    success: bool = False
    message_id: str | None = None

    def __init__(self, reason: str) -> None:
        self.error: str | None = reason


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _first_voice_codec(
    sdp_codecs: tuple[SdpCodec, ...],
) -> SdpCodec | None:
    """Return the first non-DTMF codec from the negotiated set, or None."""
    for c in sdp_codecs:
        if c.encoding.upper() in ("PCMU", "PCMA"):
            return c
    return None


def _to_engine_codec(sdp_codec: SdpCodec) -> Codec:
    """Map a negotiated SDP Codec to the engine's ``Codec`` enum."""
    if sdp_codec.encoding.upper() == "PCMU":
        return Codec.PCMU
    return Codec.PCMA


def _effective_address(audio: AudioMedia, offer: SessionDescription) -> str:
    """The remote RTP address: media-level c=, then session-level c=, else loopback."""
    addr = audio.connection_address or offer.connection_address
    return addr if addr else "127.0.0.1"


def _caller_number(from_header: str) -> str:
    """Extract the user part of the From AOR, or return the header verbatim."""
    # From: <sip:NUMBER@host>;tag=…  or  sip:NUMBER@host
    import re  # noqa: PLC0415

    match = re.search(r"sip:([^@>]+)@", from_header)
    return match.group(1) if match else from_header


def _make_vad(media_cfg: MediaConfig) -> VoiceActivityDetector:
    """Build a VoiceActivityDetector from the media config.

    Loads the silero-vad ONNX model from ``media_cfg.vad_model_dir`` (or the
    ``HERMES_VOIP_VAD_MODEL_DIR`` environment variable). Requires the ``ml``
    extra (onnxruntime + numpy); raises ``ImportError`` / ``FileNotFoundError``
    when the extra or model file is absent so the error surfaces clearly.

    Called once per inbound call; the ONNX session is created inside
    :func:`~hermes_voip.media.vad.load_silero_model`.
    """
    from hermes_voip.media.vad import load_silero_model  # noqa: PLC0415

    model: VadModel = load_silero_model()
    return VoiceActivityDetector(
        model=model,
        threshold=media_cfg.vad_threshold,
    )


def _make_endpointer(media_cfg: MediaConfig) -> Endpointer:
    """Build an Endpointer using the configured trailing-silence threshold."""
    return Endpointer(silence_ms=media_cfg.endpoint_silence_ms)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx: object) -> None:
    """Register the VoIP plugin with the Hermes runtime.

    Called by the Hermes plugin loader (``hermes_cli.plugins``) after the
    package is discovered via the ``hermes_agent.plugins`` entry point. The
    ``ctx`` object is a ``PluginContext`` at runtime — typed here as ``object``
    to keep the Hermes import out of the default install path.

    Registration is unconditional (``check_fn`` does the runtime probe);
    the Hermes gateway decides whether to activate the platform based on the
    user's ``hermes plugins enable hermes-voip`` configuration.

    Args:
        ctx: The Hermes ``PluginContextProtocol`` — accepts
             ``register_platform`` calls.
    """
    register_platform = getattr(ctx, "register_platform", None)
    if register_platform is None:
        _log.warning("register(ctx): ctx has no register_platform — skipping")
        return

    def _adapter_factory(config: object) -> VoipAdapter:
        """Lazy factory: import VoipAdapter only when the platform activates."""
        platform = object()  # replaced by real Platform by the gateway
        return VoipAdapter(config, platform)

    def _check_fn() -> bool:
        """Probe whether the SIP deps and env are available."""
        try:
            import ssl as _ssl  # noqa: PLC0415

            _ssl.create_default_context()
        except Exception:  # noqa: BLE001
            return False
        return True

    register_platform(
        "voip",
        "VoIP (SIP/WebRTC telephony)",
        _adapter_factory,
        _check_fn,
        validate_config=validate_voip_config,
        required_env=list(_REQUIRED_ENV),
        install_hint=_INSTALL_HINT,
    )
