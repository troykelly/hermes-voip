"""WebRTC media session orchestration: ICE → DTLS-SRTP keying (ADR-0032).

Glues the already-tested WebRTC primitives into the two-phase flow the adapter
needs to answer an inbound ``UDP/TLS/RTP/SAVPF`` offer:

* :meth:`WebRtcMediaSession.prepare` — choose the DTLS role from the offer's
  ``a=setup`` (RFC 5763 §5: an ``actpass``/``active`` offer makes us ``passive``;
  a ``passive`` offer makes us ``active``), build the
  :class:`~hermes_voip.media.dtls.DtlsEndpoint` and the
  :class:`~hermes_voip.media.ice.IceConnection`, gather ICE candidates, and expose
  the fingerprint / setup / ICE ufrag+pwd+candidates the SDP answer carries.
* :meth:`WebRtcMediaSession.run_handshake` — apply the peer's ICE credentials +
  candidates (parsed from their offer), run ICE connectivity checks, pump the DTLS
  handshake records over the ICE datagram pipe (with the RFC 7983 first-byte demux
  so only DTLS records feed the state machine), verify the peer's certificate
  fingerprint against the offered ``a=fingerprint`` (RFC 5763 §5 — a mismatch
  aborts the call), and derive the ``(inbound, outbound)``
  :class:`~hermes_voip.media.srtp.SrtpSession` pair.

The derived ``IceConnection`` is then handed to the engine as its ``ice_transport``
seam (the engine carries SRTP over ``ice.send``/``ice.recv``), and the two SRTP
sessions become the engine's ``srtp_inbound``/``srtp_outbound``.

**Security invariants.** No key/cert material is logged or raised in exception text
(inherited from :mod:`hermes_voip.media.dtls`). The peer fingerprint is verified
before any SRTP key is derived (``derive_srtp_sessions`` itself enforces this).

**Dependency gating.** ``DtlsEndpoint`` / ``IceConnection`` lazy-import pyOpenSSL /
aioice (the ``webrtc`` extra); this module imports them at module scope but they in
turn defer the heavy imports, so ``import hermes_voip.media.webrtc_session`` stays
light and the ImportError surfaces only at construction (rule 37).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol

from hermes_voip.media.dtls import DtlsEndpoint, DtlsRole
from hermes_voip.media.ice import IceCandidate, IceConnection
from hermes_voip.media.srtp import SrtpSession
from hermes_voip.sdp import Fingerprint, SetupRole

__all__ = ["WebRtcMediaSession", "answer_setup_for_offer"]

_log = logging.getLogger(__name__)

# RFC 7983 first-byte demux: DTLS records are 20-63. During the handshake the ICE
# pipe carries DTLS; once keyed it carries SRTP (128-191). We feed only DTLS bytes
# to the handshake state machine and ignore the rest (a stray early SRTP/STUN
# datagram), so a misordered packet never corrupts the handshake.
_RFC7983_DTLS_MIN = 20
_RFC7983_DTLS_MAX = 63

# Safety bound on the DTLS handshake pump (datagram round-trips) so a stuck peer
# does not hang the call setup forever; each iteration awaits at most one recv with
# a per-recv timeout.
_MAX_HANDSHAKE_ROUNDS = 200
# Per-recv timeout (seconds) inside the handshake pump. A handshake that stalls
# this long on any single inbound datagram is treated as failed.
_HANDSHAKE_RECV_TIMEOUT_S = 10.0


class _IcePipe(Protocol):
    """The :class:`~hermes_voip.media.ice.IceConnection` surface this session drives.

    Declared as a Protocol so tests can inject an in-memory fake without a real
    aioice agent; :class:`~hermes_voip.media.ice.IceConnection` satisfies it
    structurally.
    """

    @property
    def local_ufrag(self) -> str:
        """Our ICE ufrag for the SDP answer."""
        ...

    @property
    def local_pwd(self) -> str:
        """Our ICE password for the SDP answer."""
        ...

    @property
    def local_candidates(self) -> list[IceCandidate]:
        """Our gathered ICE candidates for the SDP answer."""
        ...

    async def gather_candidates(self) -> None:
        """Gather local ICE candidates."""
        ...

    def set_remote_credentials(self, ufrag: str, pwd: str) -> None:
        """Apply the peer's ICE credentials from their offer."""
        ...

    async def add_remote_candidate(self, candidate: IceCandidate | None) -> None:
        """Add a peer candidate, or ``None`` for end-of-candidates."""
        ...

    async def connect(self) -> None:
        """Run ICE connectivity checks and nominate a pair."""
        ...

    async def send(self, data: bytes) -> None:
        """Send a datagram over the nominated pair."""
        ...

    async def recv(self) -> bytes:
        """Receive the next datagram from the nominated pair."""
        ...

    async def close(self) -> None:
        """Close the ICE connection."""
        ...


class _IceFactory(Protocol):
    """Builds an ICE pipe (the real :class:`IceConnection`, or a test fake).

    A typed callable Protocol rather than ``Callable[..., _IcePipe]`` so the keyword
    signature is explicit and ``disallow_any_explicit`` stays satisfied (no ``...``).
    """

    def __call__(
        self, *, ice_controlling: bool, stun_urls: tuple[str, ...]
    ) -> _IcePipe:
        """Construct an ICE pipe for the given role and STUN servers."""
        ...


def _default_ice_factory(
    *, ice_controlling: bool, stun_urls: tuple[str, ...]
) -> _IcePipe:
    """Build the real :class:`~hermes_voip.media.ice.IceConnection`.

    Args:
        ice_controlling: ``True`` for the controlling role. The SIP UAS answering
            an inbound call is ICE-CONTROLLED (the offerer/UAC is controlling), so
            the adapter passes ``False`` here.
        stun_urls: ``stun:`` URLs for srflx candidates (empty ⇒ host-only).

    Returns:
        A new :class:`IceConnection`.
    """
    return IceConnection(ice_controlling=ice_controlling, stun_urls=stun_urls)


def answer_setup_for_offer(offer_setup: SetupRole | None) -> SetupRole:
    """Choose our ``a=setup`` role as the answerer (RFC 5763 §5).

    The answerer MUST NOT be ``actpass``. An ``actpass`` or ``active`` offer makes
    us ``passive`` (and thus the DTLS SERVER); a ``passive`` offer makes us
    ``active`` (the DTLS CLIENT, sending the ClientHello). A missing ``a=setup`` is
    treated as ``actpass`` (RFC 5763 §5 default), so we answer ``passive``.

    Args:
        offer_setup: The offered ``a=setup`` role, or ``None`` if absent.

    Returns:
        Our ``SetupRole`` for the answer (always ``active`` or ``passive``).
    """
    offered = offer_setup.value if offer_setup is not None else "actpass"
    # passive offer → we initiate (active); actpass/active offer → we wait (passive).
    return SetupRole("active") if offered == "passive" else SetupRole("passive")


class WebRtcMediaSession:
    """Orchestrates ICE + DTLS-SRTP keying for one inbound WebRTC call (ADR-0032).

    Construct with the offer's ``a=setup`` role; call :meth:`prepare` (gathers ICE +
    exposes the answer attributes), build + send the SDP answer from those
    attributes, then call :meth:`run_handshake` (ICE connect + DTLS + SRTP keying).
    The connected :attr:`ice` and the derived SRTP pair then drive the engine.

    Args:
        offer_setup: The offered ``a=setup`` role (``None`` ⇒ treated as actpass).
        stun_urls: ``stun:`` URLs for srflx ICE candidates (empty ⇒ host-only).
        ice_factory: Factory building the ICE pipe (defaults to the real
            :class:`IceConnection`; injected in tests).
        cipher_list: Optional DTLS cipher pin passed to :class:`DtlsEndpoint`.
    """

    def __init__(
        self,
        *,
        offer_setup: SetupRole | None,
        stun_urls: tuple[str, ...] = (),
        ice_factory: _IceFactory = _default_ice_factory,
        cipher_list: bytes | None = None,
    ) -> None:
        """Pick the DTLS role from the offer and build the DTLS + ICE objects."""
        self._setup = answer_setup_for_offer(offer_setup)
        role = DtlsRole.CLIENT if self._setup.value == "active" else DtlsRole.SERVER
        self._dtls = DtlsEndpoint(role=role, cipher_list=cipher_list)
        # The SIP UAS (answering an inbound INVITE) is ICE-CONTROLLED.
        self._ice: _IcePipe = ice_factory(ice_controlling=False, stun_urls=stun_urls)
        self._prepared = False

    # ------------------------------------------------------------------
    # Phase A: prepare (gather + expose answer attributes)
    # ------------------------------------------------------------------

    async def prepare(self) -> None:
        """Gather ICE candidates so the SDP answer attributes become available.

        Idempotent guard: must be called once before reading :attr:`ice_ufrag` /
        :attr:`ice_candidates` and before :meth:`run_handshake`.
        """
        await self._ice.gather_candidates()
        self._prepared = True

    @property
    def setup(self) -> SetupRole:
        """Our ``a=setup`` role for the SDP answer (``active`` or ``passive``)."""
        return self._setup

    @property
    def fingerprint(self) -> Fingerprint:
        """Our DTLS certificate fingerprint for the SDP ``a=fingerprint``."""
        algo, _, value = self._dtls.fingerprint().partition(" ")
        return Fingerprint(algorithm=algo.lower(), value=value)

    @property
    def ice_ufrag(self) -> str:
        """Our ICE ufrag for the SDP ``a=ice-ufrag``."""
        return self._ice.local_ufrag

    @property
    def ice_pwd(self) -> str:
        """Our ICE password for the SDP ``a=ice-pwd``."""
        return self._ice.local_pwd

    @property
    def ice_candidates(self) -> list[IceCandidate]:
        """Our gathered ICE candidates for the SDP ``a=candidate`` lines."""
        return self._ice.local_candidates

    @property
    def ice(self) -> _IcePipe:
        """The ICE connection (handed to the engine as ``ice_transport``)."""
        return self._ice

    # ------------------------------------------------------------------
    # Phase B: run the ICE + DTLS handshake, derive SRTP
    # ------------------------------------------------------------------

    async def run_handshake(
        self,
        *,
        peer_fingerprint: Fingerprint,
        peer_ice_ufrag: str,
        peer_ice_pwd: str,
        peer_candidates: Sequence[IceCandidate] = (),
    ) -> tuple[SrtpSession, SrtpSession]:
        """Run ICE connectivity + the DTLS handshake, returning the SRTP pair.

        Applies the peer's ICE credentials + candidates, runs ICE checks, pumps the
        DTLS handshake over the ICE pipe, verifies the peer's certificate against
        ``peer_fingerprint`` (RFC 5763 §5), and derives the SRTP sessions.

        Args:
            peer_fingerprint: The peer's ``a=fingerprint`` from their offer.
            peer_ice_ufrag: The peer's ``a=ice-ufrag``.
            peer_ice_pwd: The peer's ``a=ice-pwd``.
            peer_candidates: The peer's ``a=candidate`` list (non-trickle MVP).

        Returns:
            ``(inbound, outbound)`` SRTP sessions for the engine.

        Raises:
            RuntimeError: If called before :meth:`prepare`, or the DTLS handshake
                fails to complete.
            ValueError: If the peer's certificate fingerprint does not match
                ``peer_fingerprint`` (the call must be aborted).
            ConnectionError: If ICE connectivity checks fail.
        """
        if not self._prepared:
            msg = "WebRtcMediaSession.run_handshake() called before prepare()"
            raise RuntimeError(msg)

        # Apply the peer's ICE credentials + candidates (non-trickle: all up front).
        self._ice.set_remote_credentials(peer_ice_ufrag, peer_ice_pwd)
        for cand in peer_candidates:
            await self._ice.add_remote_candidate(cand)
        await self._ice.add_remote_candidate(None)  # end-of-candidates

        # Run ICE connectivity checks (nominates a pair; raises on failure).
        await self._ice.connect()

        # Pump the DTLS handshake over the nominated ICE pair.
        await self._pump_dtls_handshake()

        # RFC 5763 §5: verify the peer cert fingerprint BEFORE deriving keys. A
        # mismatch raises ValueError (the call must be rejected). The rendered
        # a=fingerprint value the peer offered keys this check.
        self._dtls.verify_peer_fingerprint(
            f"{peer_fingerprint.algorithm} {peer_fingerprint.value}"
        )

        # Derive the inbound/outbound SRTP sessions (role-mirrored per RFC 5764).
        inbound, outbound = self._dtls.derive_srtp_sessions()
        _log.info("webrtc: DTLS-SRTP keyed (setup=%s)", self._setup.value)
        return inbound, outbound

    async def _pump_dtls_handshake(self) -> None:
        """Exchange DTLS records over the ICE pipe until the handshake completes.

        Each round: drain the DTLS state machine's outbound datagrams and send them
        over the ICE pipe, then — if the handshake is not yet done — receive the
        next datagram, demux DTLS (RFC 7983 first byte 20-63; non-DTLS bytes are
        dropped), and feed it back. The CLIENT (``a=setup:active``) produces the
        ClientHello on the first drain; the SERVER waits for it. A fatal alert
        re-raises from feed/get_outbound_datagrams (rule 37).

        Raises:
            RuntimeError: If the handshake does not complete within the round/recv
                bound.
        """
        for _ in range(_MAX_HANDSHAKE_ROUNDS):
            for dg in self._dtls.get_outbound_datagrams():
                await self._ice.send(dg)
            if self._dtls.handshake_done():
                return
            try:
                data = await asyncio.wait_for(
                    self._ice.recv(), timeout=_HANDSHAKE_RECV_TIMEOUT_S
                )
            except TimeoutError as exc:
                msg = "DTLS handshake stalled waiting for a peer datagram"
                raise RuntimeError(msg) from exc
            if not data:
                continue
            first = data[0]
            if _RFC7983_DTLS_MIN <= first <= _RFC7983_DTLS_MAX:
                self._dtls.feed(data)
            else:
                # A non-DTLS datagram during the handshake (early SRTP/STUN). Ignore
                # it — only DTLS records advance the state machine.
                _log.debug(
                    "webrtc: ignoring non-DTLS datagram during handshake "
                    "(first byte %d)",
                    first,
                )
        msg = "DTLS handshake did not complete within the round limit"
        raise RuntimeError(msg)

    async def close(self) -> None:
        """Close the ICE connection (release aioice sockets); idempotent."""
        await self._ice.close()
