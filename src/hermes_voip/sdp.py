"""SDP (RFC 4566) parsing, building, and offer/answer negotiation (RFC 3264).

Scoped to what a telephony endpoint needs: one audio media description with its
codecs (PCMU/PCMA/telephone-event), the media-security profile (RTP/AVP vs
RTP/SAVP + SDES ``a=crypto`` lines), ptime, and direction. Video and other media
are ignored. Addresses are passed in by the transport; none are hard-coded.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

# RFC 3551 static payload types we may see without an explicit a=rtpmap.
_STATIC_PAYLOADS: dict[int, tuple[str, int]] = {
    0: ("PCMU", 8000),
    3: ("GSM", 8000),
    8: ("PCMA", 8000),
    9: ("G722", 8000),
    18: ("G729", 8000),
}
_DIRECTIONS = frozenset({"sendrecv", "sendonly", "recvonly", "inactive"})
_TELEPHONE_EVENT = "telephone-event"
_DEFAULT_CLOCK_RATE = 8000
_CONN_ADDR_FIELD = 2  # c=<nettype> <addrtype> <address>
_MIN_LINE_LEN = 2  # an SDP line is at minimum "<type>="
_M_AUDIO_MIN_FIELDS = 3  # m=audio <port> <proto> [<fmt>...]
_MAX_PORT = 65535
_CRLF = "\r\n"


class SdpError(ValueError):
    """Raised when an SDP body is malformed (inbound network data)."""


@dataclass(frozen=True, slots=True)
class Codec:
    """One RTP codec: payload type plus its rtpmap and optional fmtp.

    Attributes:
        payload_type: The RTP payload type number.
        encoding: The encoding name (e.g. ``PCMU``, ``telephone-event``).
        clock_rate: The RTP clock rate in Hz (8000 for narrowband telephony).
        channels: Channel count (1 for telephony).
        fmtp: The format-specific parameters (e.g. ``0-16`` for DTMF), if any.
    """

    payload_type: int
    encoding: str
    clock_rate: int
    channels: int = 1
    fmtp: str | None = None


@dataclass(frozen=True, slots=True)
class AudioMedia:
    """A parsed ``m=audio`` description.

    Attributes:
        port: The RTP port the peer receives on.
        protocol: The transport profile (``RTP/AVP``, ``RTP/SAVP``, ...).
        codecs: The offered codecs in offer order.
        crypto: Raw ``a=crypto`` lines (SDES) for an SRTP profile.
        ptime: Packetisation time in ms, if declared.
        direction: ``sendrecv`` / ``sendonly`` / ``recvonly`` / ``inactive``.
        connection_address: The effective connection address for this media.
    """

    port: int
    protocol: str
    codecs: tuple[Codec, ...]
    crypto: tuple[str, ...]
    ptime: int | None
    direction: str
    connection_address: str | None

    @property
    def is_srtp(self) -> bool:
        """True when the transport profile is secured (SAVP)."""
        return "SAVP" in self.protocol


@dataclass(slots=True)
class _AudioAccumulator:
    """Mutable scratch state for one ``m=audio`` block while parsing."""

    port: int = 0
    protocol: str = ""
    fmt_order: list[int] = field(default_factory=list)
    rtpmaps: dict[int, tuple[str, int, int]] = field(default_factory=dict)
    fmtps: dict[int, str] = field(default_factory=dict)
    crypto: list[str] = field(default_factory=list)
    ptime: int | None = None
    direction: str = "sendrecv"
    connection: str | None = None

    def set_media_line(self, value: str) -> None:
        """Apply an ``m=audio <port> <proto> <fmt...>`` line.

        Raises:
            SdpError: If the line is truncated or carries a non-integer port or
                payload type.
        """
        fields = value.split()
        if len(fields) < _M_AUDIO_MIN_FIELDS:
            msg = f"malformed m=audio line: {value!r}"
            raise SdpError(msg)
        try:
            self.port = int(fields[1])
            self.fmt_order = [int(pt) for pt in fields[3:]]
        except ValueError as exc:
            msg = f"malformed m=audio line: {value!r}"
            raise SdpError(msg) from exc
        self.protocol = fields[2]

    def add_attribute(self, value: str) -> None:
        """Fold one media-level ``a=`` attribute into the accumulator.

        Raises:
            SdpError: If an ``rtpmap``/``fmtp``/``ptime`` value is malformed.
        """
        tag, _, rest = value.partition(":")
        try:
            self._add_attribute(tag, rest, value)
        except ValueError as exc:
            if isinstance(exc, SdpError):
                raise
            msg = f"malformed a={value!r}"
            raise SdpError(msg) from exc

    def _add_attribute(self, tag: str, rest: str, value: str) -> None:
        if tag == "rtpmap":
            pt_str, _, enc_str = rest.partition(" ")
            encoding, _, after = enc_str.partition("/")
            rate_str, _, ch_str = after.partition("/")
            rate = int(rate_str) if rate_str else _DEFAULT_CLOCK_RATE
            channels = int(ch_str) if ch_str else 1
            self.rtpmaps[int(pt_str)] = (encoding, rate, channels)
        elif tag == "fmtp":
            pt_str, _, params = rest.partition(" ")
            self.fmtps[int(pt_str)] = params.strip()
        elif tag == "crypto":
            self.crypto.append(rest.strip())
        elif tag == "ptime":
            self.ptime = int(rest.strip())
        elif value in _DIRECTIONS:
            self.direction = value

    def build(self, session_connection: str | None) -> AudioMedia:
        """Resolve the accumulated state into an immutable :class:`AudioMedia`."""
        codecs: list[Codec] = []
        for pt in self.fmt_order:
            if pt in self.rtpmaps:
                encoding, rate, channels = self.rtpmaps[pt]
            elif pt in _STATIC_PAYLOADS:
                encoding, rate = _STATIC_PAYLOADS[pt]
                channels = 1
            else:
                continue  # dynamic payload with no rtpmap is unusable
            codecs.append(
                Codec(pt, encoding, rate, channels=channels, fmtp=self.fmtps.get(pt))
            )
        return AudioMedia(
            port=self.port,
            protocol=self.protocol,
            codecs=tuple(codecs),
            crypto=tuple(self.crypto),
            ptime=self.ptime,
            direction=self.direction,
            connection_address=self.connection or session_connection,
        )


@dataclass(frozen=True, slots=True)
class SessionDescription:
    """A parsed SDP body, reduced to the audio media we care about."""

    connection_address: str | None
    audio: AudioMedia | None

    @classmethod
    def parse(cls, text: str) -> SessionDescription:
        """Parse an SDP body, extracting the first audio media if present."""
        session_conn: str | None = None
        acc: _AudioAccumulator | None = None
        in_audio = False  # currently inside the (single) selected audio section
        seen_media = False  # any m= line has started; session-level c= is over

        for raw in text.replace(_CRLF, "\n").split("\n"):
            line = raw.strip()
            if len(line) < _MIN_LINE_LEN or line[1] != "=":
                continue
            kind, value = line[0], line[2:]
            if kind == "m":
                seen_media = True
                if acc is not None:
                    in_audio = False  # first audio already captured; ignore the rest
                elif value.startswith("audio"):
                    acc = _AudioAccumulator()
                    acc.set_media_line(value)
                    in_audio = True
                else:
                    in_audio = False
            elif kind == "c":
                fields = value.split()
                addr = (
                    fields[_CONN_ADDR_FIELD] if len(fields) > _CONN_ADDR_FIELD else None
                )
                if not seen_media:
                    session_conn = addr  # session-level c= precedes any media
                elif in_audio and acc is not None:
                    acc.connection = addr  # media-level c= for the audio section
            elif kind == "a" and in_audio and acc is not None:
                acc.add_attribute(value)

        audio = acc.build(session_conn) if acc is not None else None
        return cls(connection_address=session_conn, audio=audio)


def negotiate_audio(offer: AudioMedia, supported: Sequence[str]) -> tuple[Codec, ...]:
    """Choose the codecs common to ``offer`` and ``supported``, in offer order.

    Args:
        offer: The peer's offered audio media.
        supported: Encoding names we can handle (case-insensitive).

    Returns:
        The agreed codecs (including ``telephone-event`` for DTMF when offered).

    Raises:
        ValueError: If no actual voice codec is shared (a DTMF-only match is
            not a usable call).
    """
    wanted = {name.upper() for name in supported}
    chosen = tuple(c for c in offer.codecs if c.encoding.upper() in wanted)
    has_voice = any(c.encoding.lower() != _TELEPHONE_EVENT for c in chosen)
    if not has_voice:
        msg = f"no common audio codec (offered {[c.encoding for c in offer.codecs]})"
        raise ValueError(msg)
    return chosen


def build_audio_offer(  # noqa: PLR0913 - SDP fields are independent; all keyword-only
    *,
    local_address: str,
    port: int,
    codecs: Sequence[Codec],
    direction: str = "sendrecv",
    ptime: int = 20,
    session_id: int = 0,
) -> str:
    """Build an SDP audio offer/answer body (RTP/AVP).

    Args:
        local_address: The address we receive RTP on (IPv4 literal).
        port: The RTP port we receive on.
        codecs: The codecs to offer, in preference order.
        direction: Media direction attribute.
        ptime: Packetisation time in ms.
        session_id: SDP ``o=`` session id/version (the transport supplies a real,
            monotonic value for re-INVITE; defaults to 0).

    Returns:
        The SDP body terminated by CRLF, ready to attach to an INVITE/200.

    Raises:
        ValueError: If ``direction`` is invalid, ``codecs`` is empty, ``port`` is
            outside ``1..65535``, or ``ptime`` is not positive.
    """
    if direction not in _DIRECTIONS:
        msg = f"invalid SDP direction: {direction!r}"
        raise ValueError(msg)
    if not codecs:
        msg = "an SDP audio offer needs at least one codec"
        raise ValueError(msg)
    if not 1 <= port <= _MAX_PORT:
        msg = f"RTP port out of range 1..{_MAX_PORT}: {port}"
        raise ValueError(msg)
    if ptime <= 0:
        msg = f"ptime must be positive, got {ptime}"
        raise ValueError(msg)
    payloads = " ".join(str(c.payload_type) for c in codecs)
    lines = [
        "v=0",
        f"o=- {session_id} {session_id} IN IP4 {local_address}",
        "s=-",
        f"c=IN IP4 {local_address}",
        "t=0 0",
        f"m=audio {port} RTP/AVP {payloads}",
    ]
    for codec in codecs:
        rate = f"{codec.encoding}/{codec.clock_rate}"
        if codec.channels != 1:
            rate = f"{rate}/{codec.channels}"
        lines.append(f"a=rtpmap:{codec.payload_type} {rate}")
        if codec.fmtp is not None:
            lines.append(f"a=fmtp:{codec.payload_type} {codec.fmtp}")
    lines.append(f"a=ptime:{ptime}")
    lines.append(f"a={direction}")
    return _CRLF.join(lines) + _CRLF
