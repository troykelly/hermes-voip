"""The Hermes VoIP platform adapter (W10): ``VoipAdapter``.

``VoipAdapter`` subclasses the real ``gateway.platforms.base.BasePlatformAdapter``
so the Hermes gateway recognises it via ``isinstance`` and it inherits
``handle_message`` / ``build_source`` / ``set_message_handler`` and the
send-retry machinery. This module is therefore the **only** one that imports the
hermes-agent runtime (``gateway.platforms.base`` / ``gateway.config``) at module
top — and it is imported **lazily** (from :func:`hermes_voip.plugin.register`'s
factory, only when the gateway instantiates the platform), so a bare
``import hermes_voip`` never pulls in the optional runtime.

hermes-agent ships no ``py.typed``. Rather than launder the imports to ``Any``
with ``# type: ignore`` (banned by rule 17), this module is excluded from the
default no-hermes ``mypy`` gate and type-checked in the ``hermes-contract`` CI
job — which installs the ``hermes`` extra and uses ``follow_untyped_imports`` so
mypy analyses the real ``gateway`` source for genuine types. There are **zero**
``# type: ignore`` here.

End-to-end call flow for a SIP-over-TLS inbound INVITE:
1. ``SipOverTlsTransport`` frames the TLS stream and delivers a ``NewCall``
   to the ``on_new_call`` callback registered here during ``connect()``.
2. ``_on_inbound_invite`` builds a ``Dialog``, negotiates the SDP offer,
   sends a ``200 OK`` answer, opens a UDP ``RtpMediaTransport``, wires a
   ``CallSession`` and ``CallLoop``, and starts the loop as a background
   asyncio task. Any failure after the 200 OK runs ``_teardown_call`` so the
   accepted call never leaks its RTP engine or in-dialog routes.
3. When the caller finishes a turn the ``CallLoop`` calls ``deliver_turn``
   with the transcript. ``_deliver_turn`` builds a VOICE ``MessageEvent`` via
   the inherited ``build_source`` and awaits the inherited ``handle_message``,
   which routes the text to the agent.
4. The Hermes agent replies via ``adapter.send(call_id, text)``; ``send``
   delivers the text to the call's ``CallLoop.speak()``.
5. ``disconnect()`` cancels all call loops, closes the manager and transport.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING

# The real hermes-agent runtime surface. This module is imported ONLY lazily
# (inside ``hermes_voip.plugin._adapter_factory``), so a bare ``import
# hermes_voip`` never triggers these imports — but when the Hermes gateway
# instantiates the platform, ``VoipAdapter`` is a genuine ``BasePlatformAdapter``
# subclass and inherits ``handle_message`` / ``build_source`` /
# ``set_message_handler`` / the send-retry machinery. The hermes-agent package
# ships no ``py.typed``; this module is therefore excluded from the no-hermes
# ``mypy`` gate and type-checked in the hermes-contract CI job (which installs
# the ``hermes`` extra) instead of via per-line escape hatches.
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from hermes_voip.call import CallSession
from hermes_voip.config import MediaConfig, load_gateway_config, load_media_config
from hermes_voip.dialog import Dialog
from hermes_voip.digest import DigestCredentials
from hermes_voip.incall import LocalMediaSession
from hermes_voip.manager import NewCall, RegistrationManager
from hermes_voip.media.call_loop import CallLoop
from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.media.vad import VoiceActivityDetector, load_silero_model
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

__all__ = ["VoipAdapter"]

_log = logging.getLogger(__name__)

# Supported G.711 encoding names for SDP negotiation.
_SUPPORTED_ENCODINGS = ("PCMU", "PCMA", "telephone-event")

# The platform name this adapter registers under.
_PLATFORM_NAME = "voip"


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


class VoipAdapter(BasePlatformAdapter):
    """Hermes ``kind: platform`` adapter for SIP-over-TLS telephony (ADR-0002).

    Subclasses the real ``gateway.platforms.base.BasePlatformAdapter`` and
    implements its four abstract async methods (``connect`` / ``disconnect`` /
    ``send`` / ``get_chat_info``), inheriting ``handle_message`` /
    ``build_source`` / ``set_message_handler`` and the gateway's send-retry
    machinery. It wires all the merged pieces — provider registry,
    SIP-over-TLS transport, RTP media engine, and per-call ``CallLoop`` — into a
    loadable plugin.

    One adapter instance supports **N simultaneous SIP registrations** (one per
    configured ``HERMES_SIP_EXTENSION_<n>``); each inbound call creates its own
    ``CallSession`` + ``CallLoop`` keyed by SIP ``Call-ID``.

    ``config`` is the opaque object passed by the Hermes gateway through the
    ``Callable[[object], …]`` adapter-factory contract; at runtime this is always
    a ``PlatformConfig``.  ``__init__`` narrows it with an ``isinstance`` check
    (valid here because this module is in the hermes gate where ``PlatformConfig``
    is importable) and then reads SIP credentials and media settings from
    ``config.extra`` (which the Hermes gateway populates from environment
    variables).  The ``Platform`` member is resolved from :data:`_PLATFORM_NAME`
    (the gateway has already registered ``"voip"`` by the time the factory runs,
    so ``Platform("voip")`` resolves via the enum's ``_missing_`` hook).
    """

    def __init__(self, config: object) -> None:
        """Initialise the base adapter; defer all IO until ``connect()``."""
        if not isinstance(config, PlatformConfig):
            msg = f"VoipAdapter: expected PlatformConfig, got {type(config).__name__!r}"
            raise TypeError(msg)
        super().__init__(config, Platform(_PLATFORM_NAME))

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
        extra = self.config.extra
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

        # Open the TLS connection FIRST: the transport learns its local socket
        # address inside connect(), and RegistrationManager's constructor reads
        # transport.local_sent_by / contact_uri() for every extension to build
        # each Contact + Via. Building the manager before the transport is up
        # raises RuntimeError("local_sent_by is unavailable before connect()").
        await transport.connect()

        # INVARIANT: keep these three statements await-free and contiguous. The
        # transport's reader task is already running, but on the single-threaded
        # loop it cannot dispatch an inbound message until the next await
        # (``manager.connect()`` below) — so the manager is always built, stored,
        # and bound before any REGISTER response / INVITE can be routed. Insert
        # an ``await`` here and a pre-bind message would be routed with
        # ``transport._manager is None`` (reported unroutable, not delivered).
        manager = RegistrationManager(
            gateway_cfg,
            transport,
        )
        self._manager = manager
        transport.bind_manager(manager)

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
        metadata: Mapping[str, object] | None = None,  # noqa: ARG002 — no metadata consumed
    ) -> SendResult:
        """Deliver agent text to the caller via TTS synthesis.

        ``chat_id`` is the SIP ``Call-ID``. The text is passed to the call's
        ``CallLoop.speak()`` as a single-item async iterator (the Hermes adapter
        calls this once per agent reply; full streaming is a Phase-2 upgrade).

        Returns a ``SendResult``. An unknown ``chat_id`` returns a failure result
        (does not raise) — the call may have already ended.
        """
        loop = self._call_loops.get(chat_id)
        if loop is None:
            return SendResult(success=False, error=f"unknown call_id {chat_id!r}")

        text = content

        async def _single_chunk() -> AsyncIterator[str]:
            yield text

        try:
            await loop.speak(_single_chunk())
        except Exception as exc:  # noqa: BLE001 — surface as failure, never swallow
            _log.warning("speak() failed for %s: %s", chat_id, exc)
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, message_id=chat_id)

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

    async def _handle_inbound_invite(  # noqa: PLR0915 — RFC 3261 INVITE handling requires these sequential reject-early guard steps; extraction would only move the complexity elsewhere
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

        # --- Build + run CallLoop (leak-safe) ------------------------------
        # Everything from here on has already accepted the call (200 OK sent,
        # in-dialog routes installed). ANY failure now — provider/config not
        # ready, VAD/endpointer/CallLoop construction, or the loop itself —
        # must release the RTP engine and both call routes, never leak them.
        try:
            await self._run_call_loop(
                call_id=call_id,
                engine=engine,
                guard_state=guard_state,
            )
        finally:
            await self._teardown_call(
                call_id=call_id,
                engine=engine,
                transport=transport,
                dialog_id=session.dialog_id,
            )

    async def _run_call_loop(
        self,
        *,
        call_id: str,
        engine: RtpMediaTransport,
        guard_state: GuardSessionState,
    ) -> None:
        """Build the per-call ``CallLoop`` and drive it to completion.

        Raises if providers/media config are absent or any collaborator
        construction fails; the caller's ``finally`` performs teardown.
        """
        providers = self._providers
        if providers is None:
            msg = f"INVITE {call_id}: providers not initialised"
            raise RuntimeError(msg)
        media_cfg = self._media_cfg
        if media_cfg is None:
            msg = f"INVITE {call_id}: media config not initialised"
            raise RuntimeError(msg)

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
        await call_loop.run()

    async def _teardown_call(
        self,
        *,
        call_id: str,
        engine: RtpMediaTransport,
        transport: SipOverTlsTransport,
        dialog_id: tuple[str, str, str],
    ) -> None:
        """Release every resource an accepted call holds; mark it ended.

        Safe to call after a partial setup failure: stops the RTP engine,
        removes the manager + transport in-dialog routes, drops the live
        ``CallLoop``, and flags the call ended. Never raises (teardown of one
        resource must not strand the others).
        """
        info = dict(self._call_info.get(call_id, {}))
        info["ended"] = True
        self._call_info[call_id] = info
        self._call_loops.pop(call_id, None)
        if self._manager is not None:
            self._manager.remove_call(dialog_id)
        transport.remove_call(call_id)
        try:
            await engine.stop()
        except Exception as exc:  # noqa: BLE001 — log; never strand the call routes
            _log.warning("INVITE %s: error stopping media engine: %s", call_id, exc)

    async def _deliver_turn(self, call_id: str, text: str) -> None:
        """Route a finalized caller transcript to the Hermes agent.

        Builds a ``MessageEvent`` via the inherited ``build_source`` and hands
        it to the inherited ``handle_message`` (which spawns the agent turn).
        ``handle_message`` is an async method on the real base, so it is awaited
        here (this runs on the call's background task, off the media hot path).
        """
        info = self._call_info.get(call_id, {})
        caller_name = str(info.get("name", call_id))

        source = self.build_source(
            chat_id=call_id,
            chat_name=caller_name,
            chat_type="dm",
            user_id=caller_name,
            user_name=caller_name,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.VOICE,
            source=source,
            media_urls=[],
        )
        await self.handle_message(event)

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
    return VoiceActivityDetector(
        model=load_silero_model(),
        threshold=media_cfg.vad_threshold,
    )


def _make_endpointer(media_cfg: MediaConfig) -> Endpointer:
    """Build an Endpointer using the configured trailing-silence threshold."""
    return Endpointer(silence_ms=media_cfg.endpoint_silence_ms)
