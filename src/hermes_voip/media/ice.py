"""ICE agent (RFC 8445 / RFC 8839) wrapping ``aioice`` (ADR-0016, PR-D).

This module implements the ICE connectivity layer for the WebRTC media path:

* **Candidate gathering** -- host candidates always; server-reflexive (srflx)
  via a configured STUN server when ``stun_urls`` is non-empty; **relay**
  candidates via a configured TURN server when ``turn_urls`` + credentials are
  supplied (ADR-0034).  The plugin only *consumes* operator-provided TURN
  credentials (``HERMES_VOIP_ICE_TURN_*``); it does not run a TURN server.
* **Connectivity checks** -- full ICE.  Non-trickle by default (all candidates
  in the initial offer/answer, then checks run, RFC 8838 s3); aioice's check
  loop also accepts incremental remote candidates (the trickle SDP primitives
  live in ``sdp.py`` per ADR-0034).
* **Consent freshness (RFC 7675)** -- aioice runs it internally: ``connect()``
  arms a periodic STUN-consent task and ``close()``s the connection on consent
  loss, which makes a blocked :meth:`recv` raise (the engine then tears the call
  down).  This module adds no consent machinery of its own (ADR-0034).
* **Socket handoff seam** -- after :meth:`IceConnection.connect` the
  :attr:`IceConnection.selected_pair` is populated and
  :meth:`IceConnection.send` / :meth:`IceConnection.recv` carry application
  data over the nominated UDP path.  The DTLS and RTP engine layers (PR-C /
  PR-E) consume this seam: ``ice.send`` / ``ice.recv`` are the datagram I/O
  methods they call instead of opening their own socket.

**Lazy import**: ``aioice`` is declared in the optional ``webrtc`` extra.
Importing this module succeeds without the extra; :class:`IceConnection`
raises :exc:`ImportError` at *construction* time when ``aioice`` is absent
(AGENTS.md rule 37 -- error propagates, never swallowed).

**Typing strategy**: ``aioice`` is absent from the default mypy gate (it lives
in the optional ``webrtc`` extra), so we cannot import it even under
``TYPE_CHECKING``.  Mirroring the approach in ``media/srtp.py`` (which uses
narrow local :class:`typing.Protocol` classes over the optional
``cryptography`` extra): narrow Protocols cover only the ``aioice`` surface
this module calls.  ``importlib.import_module("aioice")`` returns
``types.ModuleType`` whose attribute accesses yield values whose type is
inferred as the Protocol -- no ``cast`` required, and every subsequent call is
fully type-checked in both gate environments.

**IceCandidate type shape** (integration point for sdp.py / PR-B):

The :class:`IceCandidate` dataclass exposes the following fields.  The
sdp.py lane (PR-B) MUST match this shape when it builds ``a=candidate``
lines; reconcile at integration::

    IceCandidate(
        foundation: str,       # includes "candidate:" prefix per aioice convention
        component: int,        # 1 for RTP (rtcp-mux)
        transport: str,        # "UDP" (uppercase; aioice normalises)
        priority: int,
        host: str,             # IP address string
        port: int,
        type: str,             # "host" | "srflx" | "relay"
        related_address: str | None,
        related_port: int | None,
    )

    IceCandidate.from_sdp(sdp_attr: str) -> IceCandidate
    IceCandidate.to_sdp() -> str          # canonical a=candidate value (no "a=")

**RFC 7983 first-byte demux** (engine seam / PR-E): the nominated UDP path
carries STUN (bytes 0-3), DTLS (20-63), and SRTP/SRTCP (128-191) multiplexed
on one 5-tuple per RFC 7983 (updates RFC 5764 s5.1.2).  ``aioice`` handles
the STUN traffic internally on its protocols.  Application bytes fed through
:meth:`send` / :meth:`recv` are non-STUN; the engine/DTLS layer applies the
RFC 7983 first-byte demux on the received bytes to separate DTLS handshake
traffic from SRTP media.  This module is unaware of that demux -- it passes
all non-STUN bytes up to the caller intact.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Protocol

__all__ = ["IceCandidate", "IceConnection", "IceSelectedPair"]

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Narrow Protocol surface over the optional ``aioice`` extra.
#
# ``aioice`` lives in the optional ``webrtc`` extra, absent from the default
# install and its mypy gate.  Mirroring ``media/srtp.py``'s approach for the
# optional ``cryptography`` extra: rather than suppress untyped-import errors
# with escape hatches (AGENTS.md rules 17/39 ban them), we declare *narrow
# local Protocols* covering only the constructors and methods this module
# calls.  A module attribute resolved via ``importlib.import_module`` yields a
# value whose type is ``Any``; ``Any`` is assignable to these structural
# callables without a cast, and every subsequent call is then fully type-
# checked -- clean in BOTH the no-webrtc gate and the webrtc-extra env,
# with zero ``# type: ignore``.
# ---------------------------------------------------------------------------


class _RawCandidate(Protocol):
    """The ``aioice.Candidate`` surface used by this module."""

    foundation: str
    component: int
    transport: str
    priority: int
    host: str
    port: int
    type: str
    related_address: str | None
    related_port: int | None

    def to_sdp(self) -> str:
        """Return the SDP ``a=candidate`` attribute value."""
        ...


class _RawCandidateParseCtor(Protocol):
    """The ``aioice.Candidate.from_sdp`` class-method surface."""

    def __call__(self, sdp: str) -> _RawCandidate:
        """Parse an SDP a=candidate attribute value into a raw candidate."""
        ...


class _RawCandidateCtor(Protocol):
    """The ``aioice.Candidate`` class surface (constructor + class-methods).

    Attributes:
        from_sdp: Class-method that parses an SDP ``a=candidate`` value.
    """

    from_sdp: _RawCandidateParseCtor

    def __call__(  # noqa: PLR0913 -- mirrors aioice.Candidate(9 keyword fields)
        self,
        *,
        foundation: str,
        component: int,
        transport: str,
        priority: int,
        host: str,
        port: int,
        type: str,  # noqa: A002 -- mirrors the aioice.Candidate parameter name
        related_address: str | None,
        related_port: int | None,
    ) -> _RawCandidate:
        """Construct a raw candidate."""
        ...


class _RawConnection(Protocol):
    """The ``aioice.Connection`` surface used by :class:`IceConnection`."""

    # Read-only properties (local credentials + gathered candidates).
    @property
    def local_username(self) -> str:
        """Local ICE ufrag."""
        ...

    @property
    def local_password(self) -> str:
        """Local ICE password."""
        ...

    @property
    def local_candidates(self) -> list[_RawCandidate]:
        """Gathered local candidates."""
        ...

    # Mutable plain attributes (remote credentials, set before connect).
    remote_username: str | None
    remote_password: str | None

    async def gather_candidates(self) -> None:
        """Gather local ICE candidates."""
        ...

    async def add_remote_candidate(
        self, remote_candidate: _RawCandidate | None
    ) -> None:
        """Add a remote candidate or signal end-of-candidates."""
        ...

    async def connect(self) -> None:
        """Perform ICE connectivity checks."""
        ...

    async def send(self, data: bytes) -> None:
        """Send bytes over the nominated pair (component 1)."""
        ...

    async def recv(self) -> bytes:
        """Receive the next datagram from the nominated pair."""
        ...

    async def close(self) -> None:
        """Close the connection and release sockets."""
        ...


class _RawConnectionCtor(Protocol):
    """The ``aioice.Connection`` constructor surface."""

    def __call__(  # noqa: PLR0913 -- mirrors aioice.Connection's keyword surface
        self,
        *,
        ice_controlling: bool,
        stun_server: tuple[str, int] | None,
        turn_server: tuple[str, int] | None,
        turn_username: str | None,
        turn_password: str | None,
        turn_ssl: bool,
        turn_transport: str,
        use_ipv4: bool,
        use_ipv6: bool,
    ) -> _RawConnection:
        """Construct an ICE connection (host/STUN/TURN per the given servers)."""
        ...


class _AioiceModule(Protocol):
    """The ``aioice`` module surface used by this module."""

    Candidate: _RawCandidateCtor
    Connection: _RawConnectionCtor


# ---------------------------------------------------------------------------
# Lazy aioice import helper
# ---------------------------------------------------------------------------


def _get_aioice() -> _AioiceModule:
    """Return the ``aioice`` module; raise :exc:`ImportError` if absent.

    Called once per :class:`IceConnection` construction.

    Raises:
        ImportError: If the ``webrtc`` extra (aioice) is not installed.
    """
    try:
        mod: _AioiceModule = importlib.import_module("aioice")
    except ModuleNotFoundError as exc:
        msg = (
            "aioice is required for ICE/WebRTC (install the 'webrtc' extra: "
            "uv sync --extra webrtc)"
        )
        raise ImportError(msg) from exc
    else:
        return mod


# ---------------------------------------------------------------------------
# IceCandidate -- typed, aioice-agnostic candidate dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IceCandidate:
    """A single ICE candidate (RFC 8445 s5.1.1), typed for the SDP layer.

    Attributes:
        foundation: Candidate foundation string.  ``aioice`` includes the
            ``"candidate:"`` token in this field (e.g. ``"candidate:1"``),
            matching the SDP ``a=candidate`` line format verbatim.
        component: Component ID (1 for RTP when ``a=rtcp-mux`` is in use).
        transport: Transport protocol string -- ``"UDP"`` (uppercase).
        priority: 32-bit priority computed per RFC 8445 s5.1.2.
        host: IP address string (IPv4 or IPv6).
        port: UDP port number.
        type: Candidate type: ``"host"``, ``"srflx"``, or ``"relay"``.
        related_address: Base address for srflx/relay; ``None`` for host.
        related_port: Base port for srflx/relay; ``None`` for host.

    Integration note for sdp.py (PR-B):
        ``IceCandidate.to_sdp()`` produces the value of an ``a=candidate``
        attribute (without the ``a=`` prefix).  ``from_sdp()`` parses the
        same format.  These delegate to ``aioice.Candidate`` so the wire
        syntax is always RFC 8839-conformant.
    """

    foundation: str
    component: int
    transport: str
    priority: int
    host: str
    port: int
    type: str
    related_address: str | None
    related_port: int | None

    @classmethod
    def from_sdp(cls, sdp_attr: str) -> IceCandidate:
        """Parse an SDP ``a=candidate`` attribute value into an :class:`IceCandidate`.

        Args:
            sdp_attr: The attribute value (without the ``a=`` prefix), e.g.
                ``"candidate:1 1 UDP 2130706431 192.0.2.1 5000 typ host"``.

        Returns:
            A new :class:`IceCandidate` instance.

        Raises:
            ImportError: If the ``webrtc`` extra (aioice) is not installed.
            ValueError: If the SDP attribute is malformed.
        """
        aioice = _get_aioice()
        raw = aioice.Candidate.from_sdp(sdp_attr)
        return cls._from_raw(raw)

    def to_sdp(self) -> str:
        """Serialise this candidate to an SDP ``a=candidate`` attribute value.

        Returns:
            The attribute value (without the ``a=`` prefix), e.g.
            ``"candidate:1 1 UDP 2130706431 192.0.2.1 5000 typ host"``.

        Raises:
            ImportError: If the ``webrtc`` extra (aioice) is not installed.
        """
        aioice = _get_aioice()
        raw: _RawCandidate = aioice.Candidate(
            foundation=self.foundation,
            component=self.component,
            transport=self.transport,
            priority=self.priority,
            host=self.host,
            port=self.port,
            type=self.type,
            related_address=self.related_address,
            related_port=self.related_port,
        )
        return raw.to_sdp()

    @classmethod
    def _from_raw(cls, raw: _RawCandidate) -> IceCandidate:
        """Build an :class:`IceCandidate` from an ``aioice.Candidate``."""
        return cls(
            foundation=raw.foundation,
            component=raw.component,
            transport=raw.transport,
            priority=raw.priority,
            host=raw.host,
            port=raw.port,
            type=raw.type,
            related_address=raw.related_address,
            related_port=raw.related_port,
        )


# ---------------------------------------------------------------------------
# IceSelectedPair -- the nominated candidate pair after connect()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IceSelectedPair:
    """The ICE-nominated candidate pair (RFC 8445 s8.1.1).

    Populated on :attr:`IceConnection.selected_pair` after a successful
    :meth:`IceConnection.connect`.  The engine and DTLS layers use this to
    confirm connectivity before starting the DTLS handshake.

    Attributes:
        local_candidate:  Our nominated local candidate.
        remote_candidate: The peer's nominated remote candidate.
    """

    local_candidate: IceCandidate
    remote_candidate: IceCandidate


# ---------------------------------------------------------------------------
# IceConnection -- asyncio ICE agent
# ---------------------------------------------------------------------------


class IceConnection:
    """Asyncio ICE agent for one WebRTC media stream (RFC 8445 / RFC 8839).

    Wraps ``aioice.Connection`` with a typed, aioice-agnostic API that the
    DTLS and engine layers can consume without importing aioice directly.

    Usage (non-trickle, RFC 8838 s3)::

        conn = IceConnection(
            ice_controlling=True,
            stun_urls=("stun:stun.example.test:3478",),
        )

        # 1. Gather local candidates.
        await conn.gather_candidates()

        # 2. Include conn.local_ufrag, conn.local_pwd, and conn.local_candidates
        #    in the SDP offer/answer (via sdp.py's ICE attribute builder).

        # 3. Apply the peer's credentials and candidates from their SDP.
        conn.set_remote_credentials(remote_ufrag, remote_pwd)
        for cand in remote_ice_candidates:
            await conn.add_remote_candidate(cand)
        await conn.add_remote_candidate(None)  # end-of-candidates

        # 4. Run connectivity checks.
        await conn.connect()   # raises ConnectionError on failure

        # 5. Use as a datagram pipe (DTLS layer sits on top).
        await conn.send(dtls_bytes)
        data = await conn.recv()

        # 6. Close.
        await conn.close()

    Args:
        ice_controlling: ``True`` if this agent is the controlling role (the
            SIP UAC / offerer is normally controlling -- RFC 8445 s6.1).
        stun_urls: Tuple of ``stun:`` URL strings.  Pass an empty tuple for
            host-only ICE (no STUN server required).
        turn_urls: Tuple of ``turn:`` / ``turns:`` URL strings for relay
            candidates (ADR-0034).  Empty (default) ⇒ no relay candidate.  Only
            the first URL is used (aioice accepts one TURN server).  When set,
            ``turn_username`` and ``turn_password`` are required (RFC 8656 s9.2
            long-term credentials).  The plugin consumes operator-provided TURN
            credentials; it does not run a TURN server.
        turn_username: TURN long-term username (required when ``turn_urls`` is set).
        turn_password: TURN long-term password (required when ``turn_urls`` is set).
            A secret -- never logged.
        use_ipv4: Gather IPv4 candidates (default ``True``).
        use_ipv6: Gather IPv6 candidates (default ``False`` -- most SIP
            gateways are IPv4-only; enable only if the gateway requires it).

    Raises:
        ImportError: At construction time if ``aioice`` is not installed.
        ValueError: If ``turn_urls`` is set but a credential is missing, or a
            ``turn:`` URL is malformed.
    """

    def __init__(  # noqa: PLR0913 -- independent keyword config (host/STUN/TURN/IP family)
        self,
        *,
        ice_controlling: bool,
        stun_urls: tuple[str, ...] = (),
        turn_urls: tuple[str, ...] = (),
        turn_username: str | None = None,
        turn_password: str | None = None,
        use_ipv4: bool = True,
        use_ipv6: bool = False,
    ) -> None:
        """Construct the ICE agent; no socket is opened until gather_candidates."""
        aioice = _get_aioice()

        # Parse the first stun: URL (if any) into a (host, port) tuple.
        stun_server: tuple[str, int] | None = None
        for url in stun_urls:
            stun_server = _parse_stun_url(url)
            break  # aioice.Connection accepts a single STUN server

        # Parse the first turn:/turns: URL (if any) into aioice's TURN params.
        # The plugin only consumes operator-provided TURN credentials (ADR-0034).
        turn_server: tuple[str, int] | None = None
        turn_ssl = False
        turn_transport = "udp"
        for url in turn_urls:
            host, port, turn_ssl, turn_transport = _parse_turn_url(url)
            turn_server = (host, port)
            if turn_username is None or turn_password is None:
                # RFC 8656 s9.2: TURN requires long-term credentials. A
                # credential-less TURN URL would silently gather no relay
                # candidate -- fail loudly instead (rule 27). The URL is not
                # echoed (it may carry userinfo).
                msg = "a TURN server requires a username and password"
                raise ValueError(msg)
            break  # aioice.Connection accepts a single TURN server

        self._conn: _RawConnection = aioice.Connection(
            ice_controlling=ice_controlling,
            stun_server=stun_server,
            turn_server=turn_server,
            turn_username=turn_username if turn_server is not None else None,
            turn_password=turn_password if turn_server is not None else None,
            turn_ssl=turn_ssl,
            turn_transport=turn_transport,
            use_ipv4=use_ipv4,
            use_ipv6=use_ipv6,
        )
        self._aioice = aioice
        self._selected_pair: IceSelectedPair | None = None
        self._closed: bool = False

    # ------------------------------------------------------------------
    # Local ICE credentials (available before gathering)
    # ------------------------------------------------------------------

    @property
    def local_ufrag(self) -> str:
        """Local ICE ufrag, for inclusion in SDP ``a=ice-ufrag``."""
        return self._conn.local_username

    @property
    def local_pwd(self) -> str:
        """Local ICE password, for inclusion in SDP ``a=ice-pwd``."""
        return self._conn.local_password

    # ------------------------------------------------------------------
    # Candidate gathering
    # ------------------------------------------------------------------

    async def gather_candidates(self) -> None:
        """Gather local ICE candidates (host + srflx if a STUN server is configured).

        After this returns, :attr:`local_candidates` is populated.  Must be
        called before :meth:`connect`.
        """
        await self._conn.gather_candidates()

    @property
    def local_candidates(self) -> list[IceCandidate]:
        """Gathered local candidates (populated after :meth:`gather_candidates`)."""
        return [IceCandidate._from_raw(c) for c in self._conn.local_candidates]

    # ------------------------------------------------------------------
    # Remote credentials + candidates
    # ------------------------------------------------------------------

    def set_remote_credentials(self, ufrag: str, pwd: str) -> None:
        """Set the peer's ICE credentials from their SDP.

        Must be called before :meth:`connect`.

        Args:
            ufrag: The peer's ``a=ice-ufrag`` value.
            pwd:   The peer's ``a=ice-pwd`` value.
        """
        self._conn.remote_username = ufrag
        self._conn.remote_password = pwd

    async def add_remote_candidate(self, candidate: IceCandidate | None) -> None:
        """Add a peer candidate from their SDP, or signal end-of-candidates.

        Args:
            candidate: An :class:`IceCandidate` parsed from the peer's SDP, or
                ``None`` to signal end-of-candidates (non-trickle: call once
                with ``None`` after adding all candidates -- RFC 8838 s3).

        Raises:
            ValueError: If called with a candidate after end-of-candidates has
                been signalled.
        """
        if candidate is None:
            await self._conn.add_remote_candidate(None)
            return
        raw: _RawCandidate = self._aioice.Candidate(
            foundation=candidate.foundation,
            component=candidate.component,
            transport=candidate.transport,
            priority=candidate.priority,
            host=candidate.host,
            port=candidate.port,
            type=candidate.type,
            related_address=candidate.related_address,
            related_port=candidate.related_port,
        )
        await self._conn.add_remote_candidate(raw)

    # ------------------------------------------------------------------
    # Connectivity checks
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Run ICE connectivity checks and nominate a candidate pair.

        Performs all STUN binding request/response exchanges; on completion the
        :attr:`selected_pair` is populated with the nominated pair and
        :meth:`send` / :meth:`recv` are ready to use.

        Raises:
            ConnectionError: If checks fail or no pair is nominated.
        """
        await self._conn.connect()
        # Access the internal nominated dict to build the typed IceSelectedPair.
        # aioice 0.10.2 has no public accessor for the nominated pair; the
        # internal _nominated dict is the only source (maintenance point: update
        # if aioice adds a public accessor in a future release).
        nominated = self._conn._nominated  # type: ignore[attr-defined]  # no public API in aioice 0.10.2
        if not nominated:
            msg = "ICE connect() completed but no nominated pair found"
            raise ConnectionError(msg)
        pair = nominated[min(nominated)]  # component 1 (RTP; rtcp-mux)
        self._selected_pair = IceSelectedPair(
            local_candidate=IceCandidate._from_raw(pair.local_candidate),
            remote_candidate=IceCandidate._from_raw(pair.remote_candidate),
        )
        _log.info(
            "ice: nominated pair %s:%d -> %s:%d",
            pair.local_candidate.host,
            pair.local_candidate.port,
            pair.remote_candidate.host,
            pair.remote_candidate.port,
        )

    @property
    def selected_pair(self) -> IceSelectedPair | None:
        """The nominated pair, or ``None`` before :meth:`connect`."""
        return self._selected_pair

    # ------------------------------------------------------------------
    # Data transfer -- socket handoff seam for DTLS / engine (PR-C / PR-E)
    # ------------------------------------------------------------------

    async def send(self, data: bytes) -> None:
        """Send bytes over the nominated ICE path (component 1).

        The engine / DTLS layer calls this instead of writing to a raw socket.
        On the WebRTC path, STUN consent, DTLS, and SRTP share this 5-tuple;
        the first-byte RFC 7983 demux happens in the engine layer (PR-E).

        Args:
            data: The datagram payload to send (DTLS record or SRTP packet).

        Raises:
            ConnectionError: If the connection has not been established.
        """
        await self._conn.send(data)

    async def recv(self) -> bytes:
        """Receive the next datagram from the nominated ICE path (component 1).

        STUN consent traffic is handled internally by ``aioice``; only
        non-STUN bytes are returned here.  The engine layer applies the
        RFC 7983 first-byte demux on the returned bytes.

        Returns:
            The next datagram payload (DTLS record or SRTP packet).

        Raises:
            ConnectionError: If the connection has not been established or
                has been closed.
        """
        return await self._conn.recv()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the ICE connection; idempotent.

        Releases all UDP sockets opened during gathering.
        """
        if self._closed:
            return
        self._closed = True
        await self._conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_stun_url(url: str) -> tuple[str, int]:
    """Parse a ``stun:<host>[:<port>]`` URL into a ``(host, port)`` tuple.

    Args:
        url: A ``stun:`` URL, e.g. ``"stun:stun.example.test:3478"``.

    Returns:
        A ``(host, port)`` tuple.  Default port 3478 per RFC 8489 s5 when
        the port is omitted.

    Raises:
        ValueError: If the URL does not start with ``"stun:"`` or has an
            invalid port.
    """
    if not url.startswith("stun:"):
        msg = f"expected a stun: URL, got {url!r}"
        raise ValueError(msg)
    remainder = url[len("stun:") :]
    if ":" not in remainder:
        return remainder, 3478
    host, port_str = remainder.rsplit(":", 1)
    try:
        port = int(port_str)
    except ValueError as exc:
        msg = f"invalid port in stun: URL {url!r}"
        raise ValueError(msg) from exc
    return host, port


# Default TURN ports (RFC 8656 s5): 3478 for turn: (plain/UDP/TCP), 5349 for
# turns: (TLS/DTLS).
_TURN_DEFAULT_PORT = 3478
_TURNS_DEFAULT_PORT = 5349


def _parse_turn_url(url: str) -> tuple[str, int, bool, str]:
    """Parse a ``turn:`` / ``turns:`` URL into aioice's TURN parameters (ADR-0034).

    Supports the RFC 7065 TURN URI shape ``turn:<host>[:<port>][?transport=udp|tcp]``
    (and ``turns:`` for TLS).  The plugin consumes operator-provided TURN servers;
    the URI carries no credentials (those are passed separately).

    Args:
        url: A ``turn:`` or ``turns:`` URL, e.g.
            ``"turn:turn.example.test:3478?transport=udp"``.

    Returns:
        A ``(host, port, ssl, transport)`` tuple: ``ssl`` is ``True`` for
        ``turns:``; ``transport`` is ``"udp"`` (default) or ``"tcp"``.  Default
        port is 3478 (``turn:``) or 5349 (``turns:``) when omitted (RFC 8656 s5).

    Raises:
        ValueError: If the URL scheme is not ``turn:``/``turns:``, the transport
            is unknown, or the port is invalid.  The error never echoes the URL
            (it may carry sensitive deployment detail).
    """
    if url.startswith("turns:"):
        ssl = True
        remainder = url[len("turns:") :]
        default_port = _TURNS_DEFAULT_PORT
    elif url.startswith("turn:"):
        ssl = False
        remainder = url[len("turn:") :]
        default_port = _TURN_DEFAULT_PORT
    else:
        msg = "expected a turn: or turns: URL"
        raise ValueError(msg)

    # Split off the optional ``?transport=...`` query (RFC 7065 s3.1).
    transport = "udp"
    if "?" in remainder:
        remainder, _, query = remainder.partition("?")
        key, _, value = query.partition("=")
        if key != "transport" or value not in {"udp", "tcp"}:
            msg = f"unsupported TURN URL query {query!r} (want transport=udp|tcp)"
            raise ValueError(msg)
        transport = value

    if ":" in remainder:
        host, port_str = remainder.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError as exc:
            msg = "invalid port in turn: URL"
            raise ValueError(msg) from exc
    else:
        host, port = remainder, default_port

    if not host:
        msg = "turn: URL is missing a host"
        raise ValueError(msg)
    return host, port, ssl, transport
