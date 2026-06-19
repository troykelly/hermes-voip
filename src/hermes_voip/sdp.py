"""SDP (RFC 4566) parsing, building, and offer/answer negotiation (RFC 3264).

Scoped to what a telephony endpoint needs: one audio media description with its
codecs (PCMU/PCMA/telephone-event), the media-security profile (RTP/AVP vs
RTP/SAVP + SDES ``a=crypto`` lines, or UDP/TLS/RTP/SAVPF + DTLS-SRTP
``a=fingerprint``/``a=setup`` + ICE for WebRTC), ptime, and direction. Video and
other media are ignored. Addresses are passed in by the transport; none are
hard-coded.

Two exclusive media-keying paths are supported (ADR-0016):

* **SDES** (``RTP/SAVP``): ``a=crypto`` inline key; no fingerprint, no ICE.
  Used on the SIP-over-TLS transport.
* **DTLS-SRTP / WebRTC** (``UDP/TLS/RTP/SAVPF``): ``a=fingerprint`` +
  ``a=setup`` + ``a=ice-ufrag`` / ``a=ice-pwd`` + ``a=candidate`` +
  ``a=rtcp-mux``; **no** ``a=crypto``, **no** ``c=`` connection address
  (RFC 5763 §5).

A media description is EITHER SDES OR DTLS-SRTP, never both.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

# RFC 3551 static payload types we may see without an explicit a=rtpmap.
_STATIC_PAYLOADS: dict[int, tuple[str, int]] = {
    0: ("PCMU", 8000),
    3: ("GSM", 8000),
    8: ("PCMA", 8000),
    9: ("G722", 8000),
    18: ("G729", 8000),
}
_DIRECTIONS = frozenset({"sendrecv", "sendonly", "recvonly", "inactive"})
# RFC 3264 §6.1: the answer mirrors the offer's direction. An offerer that can
# only send wants us to only receive, and vice versa; inactive stays inactive.
_ANSWER_DIRECTION: dict[str, str] = {
    "sendrecv": "sendrecv",
    "sendonly": "recvonly",
    "recvonly": "sendonly",
    "inactive": "inactive",
}
_TELEPHONE_EVENT = "telephone-event"
_OPUS = "opus"
# G.711 payloads, emitted after Opus when Opus is offered (ADR-0005 codec order).
_G711_ENCODINGS = frozenset({"PCMU", "PCMA"})
_DEFAULT_CLOCK_RATE = 8000
# Video RTP runs on a 90 kHz clock (RFC 3551 §4); ADR-0044.
VIDEO_DEFAULT_CLOCK_RATE = 90000
_H264 = "h264"
# H.264 fmtp packetisation-mode (RFC 6184 §8.1): mode 1 (FU-A) is required so
# large IDR frames can be FU-A fragmented; mode 0 (single-NAL only) is declined.
# The value is matched by an exact fmtp-parameter parse, not a substring — a
# substring "packetization-mode=1" also (wrongly) matches "...=10"/"x-...=1".
_PACKETIZATION_MODE_KEY = "packetization-mode"
_PACKETIZATION_MODE_FU_A = "1"
_SECURE_PROFILE = "RTP/SAVP"
_PLAIN_PROFILE = "RTP/AVP"
# WebRTC DTLS-SRTP profile (ADR-0016, RFC 5764 / RFC 4585).
_WEBRTC_PROFILE = "UDP/TLS/RTP/SAVPF"
# RFC 4568 SDES. We negotiate the two AES_CM_128 SRTP crypto-suites: with an
# 80-bit and a 32-bit HMAC-SHA1 auth tag. Both use a 128-bit (16-octet) master
# key and 112-bit (14-octet) master salt — they differ only in the SRTP/SRTCP
# auth-tag length (RFC 4568 §6.2) — so for both the inline key||salt decodes to
# 30 octets.
_AES_CM_128_HMAC_SHA1_80 = "AES_CM_128_HMAC_SHA1_80"
_AES_CM_128_HMAC_SHA1_32 = "AES_CM_128_HMAC_SHA1_32"
_SUPPORTED_CRYPTO_SUITES = frozenset(
    {_AES_CM_128_HMAC_SHA1_80, _AES_CM_128_HMAC_SHA1_32}
)
_AES_CM_128_KEY_SALT_OCTETS = 16 + 14  # shared by both supported suites
_SRTP_KEY_SALT_OCTETS: dict[str, int] = {
    _AES_CM_128_HMAC_SHA1_80: _AES_CM_128_KEY_SALT_OCTETS,
    _AES_CM_128_HMAC_SHA1_32: _AES_CM_128_KEY_SALT_OCTETS,
}
_INLINE_PREFIX = "inline:"
_CRYPTO_MIN_FIELDS = 3  # <tag> <crypto-suite> <key-params>
_MAX_TAG_DIGITS = 9  # RFC 4568: tag = 1*9DIGIT
_DEFAULT_CRYPTO_TAG = 1  # tag for an initial offer keyed from a tagless string
_CONN_ADDR_FIELD = 2  # c=<nettype> <addrtype> <address>
_MIN_LINE_LEN = 2  # an SDP line is at minimum "<type>="
_M_AUDIO_MIN_FIELDS = 3  # m=audio <port> <proto> [<fmt>...]
_MAX_PORT = 65535
_CRLF = "\r\n"

# ICE candidate fields: foundation component transport priority address port typ [raddr rport]  # noqa: E501
_ICE_CAND_MIN_FIELDS = 8  # up to and including "typ <type>"
_ICE_CAND_TYP_IDX = 6  # index of the literal "typ" keyword
_ICE_CAND_TYPE_IDX = 7  # index of the candidate type token (host/srflx/relay)
_ICE_CAND_RADDR_IDX = 8  # index of "raddr" keyword in srflx/relay extension
_ICE_CAND_RADDR_VAL_IDX = 9  # index of the raddr value
_ICE_CAND_RPORT_KW_IDX = 10  # index of "rport" keyword in srflx/relay extension
_ICE_CAND_RPORT_VAL_IDX = 11  # index of the rport value
# Valid ICE candidate types (RFC 8839 §5.1).
_ICE_CANDIDATE_TYPES = frozenset({"host", "srflx", "prflx", "relay"})
# Valid DTLS setup roles (RFC 4145 §4, RFC 5763 §5).
_SETUP_ROLES = frozenset({"actpass", "active", "passive"})


class SdpError(ValueError):
    """Raised when an SDP body is malformed (inbound network data)."""


def _validate_crypto(tag: int, suite: str, key_params: str) -> None:
    """Validate a parsed SDES crypto attribute (RFC 4568) or raise ``SdpError``.

    Enforces: a positive decimal ``tag`` of at most nine digits (the ``parse``
    path also rejects a leading zero per RFC 4568 §4); a *supported* crypto-suite
    (``AES_CM_128_HMAC_SHA1_80`` or ``AES_CM_128_HMAC_SHA1_32``); and an
    ``inline:`` key whose base64-decoded master key||salt is exactly the suite's
    length (30 octets for both supported suites). Optional ``|lifetime|MKI:length``
    fields after the key are kept verbatim but not interpreted.
    """
    if not 1 <= tag <= 10**_MAX_TAG_DIGITS - 1:
        msg = f"crypto tag out of range 1..{10**_MAX_TAG_DIGITS - 1}: {tag}"
        raise SdpError(msg)
    if suite not in _SUPPORTED_CRYPTO_SUITES:
        # Never echo the suite token: a malformed body can put the inline key in
        # the suite position (e.g. "1 inline:<KEY> inline:<KEY>").
        msg = "unsupported crypto suite"
        raise SdpError(msg)
    if not key_params.startswith(_INLINE_PREFIX):
        # Never echo key_params: it is (or carries) the SRTP master key.
        msg = "crypto key-params must use an inline: key"
        raise SdpError(msg)
    # inline:<key||salt>[|lifetime][|MKI:length] — only the key||salt is checked.
    key_b64 = key_params[len(_INLINE_PREFIX) :].split("|", 1)[0]
    try:
        decoded = base64.b64decode(key_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        # Never echo the token: an invalid base64 string is still (corrupt) key
        # material; report only the structural fault.
        msg = "crypto inline key is not valid base64"
        raise SdpError(msg) from exc
    expected = _SRTP_KEY_SALT_OCTETS[suite]
    if len(decoded) != expected:
        msg = (
            f"crypto inline key||salt is {len(decoded)} octets, "
            f"expected {expected} for {suite}"
        )
        raise SdpError(msg)


@dataclass(frozen=True, slots=True)
class CryptoAttribute:
    """A validated SDES ``a=crypto`` attribute (RFC 4568).

    Attributes:
        tag: The negotiation identifier (``1*9DIGIT``); the answer reuses the
            accepted offer's tag.
        suite: The crypto-suite token (``AES_CM_128_HMAC_SHA1_80`` or
            ``AES_CM_128_HMAC_SHA1_32`` are supported here).
        key_params: The key parameters starting with ``inline:`` (the base64
            master key||salt plus any optional ``|lifetime|MKI:length`` fields).

    This is SDES keying only (SIP-over-TLS transport, ADR-0013). The DTLS-SRTP
    path (WebRTC, ADR-0016) uses :class:`Fingerprint` + :class:`SetupRole` +
    ICE — never ``a=crypto``.

    ``key_params`` is suppressed from ``repr`` (``field(repr=False)``) because it
    is (or carries) the SRTP master key||salt; a repr lands in logs and
    tracebacks and must never expose key material.
    """

    tag: int
    suite: str
    key_params: str = field(repr=False)

    def __post_init__(self) -> None:
        """Validate on construction so an instance is always RFC-4568-valid."""
        _validate_crypto(self.tag, self.suite, self.key_params)

    @classmethod
    def parse(cls, body: str) -> CryptoAttribute:
        """Parse a ``crypto`` attribute body (``<tag> <suite> <key-params>``).

        ``body`` is the text after ``a=crypto:``. Validation matches
        :func:`_validate_crypto`, and the tag must be a non-negative decimal with
        no leading zero (RFC 4568 §4).

        Raises:
            SdpError: If the body is truncated, the tag is non-decimal or
                leading-zero, the suite is unsupported, or the inline key is
                missing/invalid/wrong-length.
        """
        fields = body.split()
        if len(fields) < _CRYPTO_MIN_FIELDS:
            # Never echo the body: a truncated line still carries the inline key
            # in its trailing token; report only the expected structure.
            msg = "malformed a=crypto attribute: expected '<tag> <suite> <key-params>'"
            raise SdpError(msg)
        tag_str, suite = fields[0], fields[1]
        # The remaining tokens are key-params then optional session-params; we
        # keep only the first (the inline key) — session-params are not used.
        key_params = fields[2]
        if not tag_str.isdigit():
            # Never echo the tag token: a malformed body can put the inline key
            # in the tag position (e.g. "inline:<KEY> <suite> inline:<KEY>").
            msg = "crypto tag is not decimal"
            raise SdpError(msg)
        if len(tag_str) > 1 and tag_str[0] == "0":
            msg = f"crypto tag has a leading zero: {tag_str!r}"
            raise SdpError(msg)
        return cls(tag=int(tag_str), suite=suite, key_params=key_params)

    def render(self) -> str:
        """Render the attribute body for an ``a=crypto:`` line (no prefix)."""
        return f"{self.tag} {self.suite} {self.key_params}"


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """A validated DTLS certificate fingerprint (``a=fingerprint``, RFC 4572).

    Used on the WebRTC / DTLS-SRTP media path (ADR-0016). The SDES path
    (:class:`CryptoAttribute`) does not use fingerprints.

    Attributes:
        algorithm: The hash algorithm name, lower-cased (e.g. ``sha-256``).
        value: The colon-separated hex fingerprint string as it appears in SDP,
            preserving the original case (RFC 4572 allows upper or lower; the
            algorithm token is normalised to lower-case by :meth:`parse`).
    """

    algorithm: str
    value: str

    @classmethod
    def parse(cls, body: str) -> Fingerprint:
        """Parse a ``fingerprint`` attribute body (``<algorithm> <value>``).

        ``body`` is the text after ``a=fingerprint:``.

        Raises:
            SdpError: If the body is missing the value token.
        """
        parts = body.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():  # noqa: PLR2004 - literal count
            msg = "malformed a=fingerprint attribute: expected '<algorithm> <value>'"
            raise SdpError(msg)
        return cls(algorithm=parts[0].lower(), value=parts[1].strip())

    def render(self) -> str:
        """Render the attribute body for an ``a=fingerprint:`` line (no prefix)."""
        return f"{self.algorithm} {self.value}"


@dataclass(frozen=True, slots=True)
class SetupRole:
    """A validated DTLS setup role (``a=setup``, RFC 4145 / RFC 5763).

    Valid values are ``actpass``, ``active``, and ``passive``.  RFC 5763 §5
    restricts the answerer to ``active`` or ``passive`` (never ``actpass``);
    :func:`build_webrtc_answer` enforces that constraint.

    Attributes:
        value: One of ``actpass``, ``active``, or ``passive``.
    """

    value: str

    @classmethod
    def parse(cls, body: str) -> SetupRole:
        """Parse a ``setup`` attribute body.

        ``body`` is the text after ``a=setup:`` (leading/trailing whitespace
        is stripped).

        Raises:
            SdpError: If the value is not a recognised setup role.
        """
        role = body.strip()
        if role not in _SETUP_ROLES:
            msg = f"unknown a=setup role: {role!r} (expected actpass/active/passive)"
            raise SdpError(msg)
        return cls(value=role)

    def render(self) -> str:
        """Render the attribute value for an ``a=setup:`` line (no prefix)."""
        return self.value


@dataclass(frozen=True, slots=True)
class IceCandidate:
    """A parsed ICE candidate (``a=candidate``, RFC 8839 §5.1).

    Attributes:
        foundation: An opaque string identifying the candidate's base.
        component: RTP component id (1 for RTP, 2 for RTCP; typically 1 with
            ``a=rtcp-mux``).
        transport: Transport protocol token (``UDP`` or ``TCP``).
        priority: 32-bit candidate priority.
        address: The candidate IP address (IPv4 or IPv6 literal).
        port: The candidate port (1-65535).
        typ: Candidate type: ``host``, ``srflx``, ``prflx``, or ``relay``.
        raddr: Reflexive/relay base address (``srflx``/``relay``), or ``None``
            for ``host`` candidates.
        rport: Reflexive/relay base port, or ``None`` for ``host`` candidates.
    """

    foundation: str
    component: int
    transport: str
    priority: int
    address: str
    port: int
    typ: str
    raddr: str | None
    rport: int | None

    @classmethod
    def parse(cls, body: str) -> IceCandidate:
        """Parse a ``candidate`` attribute body (text after ``a=candidate:``).

        Raises:
            SdpError: If the body is truncated, a numeric field is non-integer,
                or the port is out of range.
        """
        tokens = body.split()
        if len(tokens) < _ICE_CAND_MIN_FIELDS:
            msg = (
                "malformed a=candidate: expected at least "
                f"{_ICE_CAND_MIN_FIELDS} tokens"
            )
            raise SdpError(msg)
        try:
            component = int(tokens[1])
            priority = int(tokens[3])
            port = int(tokens[5])
        except ValueError as exc:
            msg = "malformed a=candidate: non-integer numeric field"
            raise SdpError(msg) from exc
        if not 1 <= port <= _MAX_PORT:
            msg = f"a=candidate port out of range 1..{_MAX_PORT}: {port}"
            raise SdpError(msg)
        foundation = tokens[0]
        transport = tokens[2]
        address = tokens[4]
        # tokens[6] should be "typ"; tokens[7] is the type value.
        typ = tokens[_ICE_CAND_TYPE_IDX]
        # Parse optional raddr/rport extension for non-host candidates.
        # Extension layout (0-based): ..., "raddr", <addr>, "rport", <port>
        # i.e. indices 8, 9, 10, 11 (when all four tokens are present).
        raddr: str | None = None
        rport: int | None = None
        if (
            len(tokens) > _ICE_CAND_RADDR_IDX
            and tokens[_ICE_CAND_RADDR_IDX] == "raddr"
            and len(tokens) > _ICE_CAND_RADDR_VAL_IDX
        ):
            raddr = tokens[_ICE_CAND_RADDR_VAL_IDX]
            if (
                len(tokens) > _ICE_CAND_RPORT_VAL_IDX
                and tokens[_ICE_CAND_RPORT_KW_IDX] == "rport"
            ):
                rport = int(tokens[_ICE_CAND_RPORT_VAL_IDX])
        return cls(
            foundation=foundation,
            component=component,
            transport=transport,
            priority=priority,
            address=address,
            port=port,
            typ=typ,
            raddr=raddr,
            rport=rport,
        )

    def render(self) -> str:
        """Render the attribute body for an ``a=candidate:`` line (no prefix)."""
        base = (
            f"{self.foundation} {self.component} {self.transport} {self.priority} "
            f"{self.address} {self.port} typ {self.typ}"
        )
        if self.raddr is not None and self.rport is not None:
            return f"{base} raddr {self.raddr} rport {self.rport}"
        return base


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
        protocol: The transport profile (``RTP/AVP``, ``RTP/SAVP``,
            ``UDP/TLS/RTP/SAVPF``, ...).
        codecs: The offered codecs in offer order.
        crypto: Raw ``a=crypto`` lines (SDES) for an SRTP profile, verbatim and
            in offer order — kept for diagnostics even when malformed.  Always
            empty on a WebRTC (``SAVPF``) m-line (RFC 8827 §6.5 / ADR-0016).
        crypto_attrs: The subset of ``crypto`` lines that parse and validate as
            a supported :class:`CryptoAttribute`, in offer order. Parsing is
            lenient: a malformed or unsupported crypto line stays in ``crypto``
            but is excluded here, so this carries only usable offered keys.
            Always empty on WebRTC m-lines.
        ptime: Packetisation time in ms, if declared (``a=ptime``).
        direction: ``sendrecv`` / ``sendonly`` / ``recvonly`` / ``inactive``.
        connection_address: The effective connection address for this media.
            ``None`` on a WebRTC m-line (address is conveyed by ICE candidates).
        fingerprint: Parsed ``a=fingerprint`` (DTLS-SRTP, RFC 4572), or ``None``
            on an SDES / plain-RTP m-line.
        setup: Parsed ``a=setup`` role (RFC 4145 / RFC 5763), or ``None`` on an
            SDES / plain-RTP m-line.
        ice_ufrag: ICE username fragment (``a=ice-ufrag``), or ``None`` if absent.
        ice_pwd: ICE password (``a=ice-pwd``), or ``None`` if absent.
        ice_candidates: Parsed ICE candidate list (``a=candidate``), in offer order.
        rtcp_mux: ``True`` when ``a=rtcp-mux`` is present (RFC 5761).
        ice_options: Parsed ``a=ice-options`` tokens (e.g. ``("trickle", "ice2")``),
            empty when absent (ADR-0034, RFC 8839 §5.6).
        end_of_candidates: ``True`` when the m-line carried ``a=end-of-candidates``
            (RFC 8838 §8.2) — the candidate generation is complete.
        mid: The media identification (``a=mid``, RFC 5888) for BUNDLE grouping,
            or ``None`` when absent (e.g. a non-BUNDLE SDES offer). ADR-0044.
        maxptime: The maximum packetisation time in ms the peer will accept
            (``a=maxptime``, RFC 4566 §6), or ``None`` when absent. An upper bound
            on the framing the answer may choose; :func:`negotiate_ptime` honours
            it (ADR-0056).

    ``crypto`` and ``crypto_attrs`` are suppressed from ``repr``
    (``field(repr=False)``): both carry SDES inline master key||salt material,
    which must never reach a log line or traceback.
    """

    port: int
    protocol: str
    codecs: tuple[Codec, ...]
    crypto: tuple[str, ...] = field(repr=False)
    ptime: int | None
    direction: str
    connection_address: str | None
    crypto_attrs: tuple[CryptoAttribute, ...] = field(default=(), repr=False)
    fingerprint: Fingerprint | None = None
    setup: SetupRole | None = None
    ice_ufrag: str | None = None
    ice_pwd: str | None = None
    ice_candidates: tuple[IceCandidate, ...] = field(default_factory=tuple)
    rtcp_mux: bool = False
    ice_options: tuple[str, ...] = field(default_factory=tuple)
    end_of_candidates: bool = False
    mid: str | None = None
    maxptime: int | None = None

    @property
    def is_srtp(self) -> bool:
        """True when the transport profile is SDES-secured (SAVP or SAVPF)."""
        return "SAVP" in self.protocol

    @property
    def is_trickle(self) -> bool:
        """True when the peer advertised trickle ICE (``a=ice-options:trickle``).

        RFC 8838 §4.1: a ``trickle`` ICE option signals the peer may send more
        candidates incrementally after the initial offer/answer. Absent the
        option (a classic non-trickle peer) this is ``False``.
        """
        return "trickle" in self.ice_options

    @property
    def is_webrtc(self) -> bool:
        """True when the transport profile is ``UDP/TLS/RTP/SAVPF`` (ADR-0016).

        A WebRTC m-line carries fingerprint + ICE; it MUST NOT carry
        ``a=crypto`` (RFC 8827 §6.5).
        """
        return self.protocol == _WEBRTC_PROFILE


@dataclass(frozen=True, slots=True)
class VideoMedia:
    """A parsed ``m=video`` description (ADR-0044).

    Video is additive: a WebRTC offer may carry an ``m=video`` section alongside
    ``m=audio`` under a BUNDLE group. Only the fields the plugin needs to build a
    BUNDLE video answer are kept; DTLS/ICE credentials are NOT duplicated here —
    they are shared with audio via BUNDLE (the answer reuses the audio
    fingerprint/ICE/setup).

    Attributes:
        port: The RTP port the peer declared for video (advisory under BUNDLE /
            rtcp-mux; the real address is the ICE 5-tuple).
        protocol: The transport profile (``UDP/TLS/RTP/SAVPF`` for WebRTC).
        codecs: The offered video codecs in offer order, with their ``fmtp``
            (e.g. ``profile-level-id``/``packetization-mode`` for H.264).
        direction: ``sendrecv`` / ``sendonly`` / ``recvonly`` / ``inactive``.
        mid: The ``a=mid`` media identification for BUNDLE, or ``None``.
        rtcp_mux: ``True`` when the section carried ``a=rtcp-mux`` (RFC 5761).
    """

    port: int
    protocol: str
    codecs: tuple[Codec, ...]
    direction: str
    mid: str | None
    rtcp_mux: bool

    @property
    def is_webrtc(self) -> bool:
        """True when the transport profile is ``UDP/TLS/RTP/SAVPF`` (ADR-0016)."""
        return self.protocol == _WEBRTC_PROFILE


class VideoAnswerMode(Enum):
    """How we answer an offered ``m=video`` section (ADR-0044).

    * ``SENDONLY`` — a source is configured: we send video (and discard ALL
      inbound video, so never ``a=sendrecv`` — see :func:`_build_video_section`).
    * ``INACTIVE`` — an H.264 codec is acceptable but no source is configured:
      ``a=inactive`` on a real port keeps the BUNDLE'd m-line present, no flow.
    * ``REJECTED`` — the offered video has no codec we can packetise (VP8-only,
      ``packetization-mode=0``, or a non-WebRTC transport profile): the m-line is
      rejected with port 0 (RFC 3264 §6) and excluded from the BUNDLE group
      (RFC 8843 §7.3.3), but KEPT so the answer's m-line count/order matches the
      offer (RFC 3264 §5.1). Dropping the m-line entirely is a malformed answer.
    """

    SENDONLY = "sendonly"
    INACTIVE = "inactive"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class VideoAnswer:
    """Our chosen ``m=video`` answer parameters (ADR-0044).

    Built by the adapter from :func:`negotiate_video_h264` plus the configured
    video source. Use the :meth:`sendonly` / :meth:`inactive` / :meth:`rejected`
    constructors so the ``(mode, codec)`` invariant holds: a REJECTED answer
    carries no negotiated ``codec`` (there is none); SENDONLY/INACTIVE always do.

    Attributes:
        mid: The video media identification to mirror (RFC 8843 BUNDLE).
        mode: How the ``m=video`` is answered (see :class:`VideoAnswerMode`).
        proto: The transport profile echoed on the answer m-line (the offered
            profile — ``UDP/TLS/RTP/SAVPF`` for a WebRTC video stream).
        payload_type: The format listed on the m-line — the negotiated H.264
            payload type (SENDONLY/INACTIVE) or an offered payload type echoed on
            the rejected line (REJECTED).
        codec: The negotiated H.264 codec (payload type + ``H264/90000`` + the
            offered ``fmtp`` we echo) for the rtpmap; ``None`` when REJECTED.
    """

    mid: str
    mode: VideoAnswerMode
    proto: str
    payload_type: int
    codec: Codec | None = None

    @classmethod
    def sendonly(
        cls, codec: Codec, mid: str, *, proto: str = _WEBRTC_PROFILE
    ) -> VideoAnswer:
        """A sourced answer: we send video (``a=sendonly``)."""
        return cls(
            mid=mid,
            mode=VideoAnswerMode.SENDONLY,
            proto=proto,
            payload_type=codec.payload_type,
            codec=codec,
        )

    @classmethod
    def inactive(
        cls, codec: Codec, mid: str, *, proto: str = _WEBRTC_PROFILE
    ) -> VideoAnswer:
        """A codec-accepted but source-less answer (``a=inactive``)."""
        return cls(
            mid=mid,
            mode=VideoAnswerMode.INACTIVE,
            proto=proto,
            payload_type=codec.payload_type,
            codec=codec,
        )

    @classmethod
    def rejected(cls, mid: str, *, proto: str, payload_type: int) -> VideoAnswer:
        """A rejected answer: ``m=video 0`` echoing an offered payload type."""
        return cls(
            mid=mid,
            mode=VideoAnswerMode.REJECTED,
            proto=proto,
            payload_type=payload_type,
            codec=None,
        )


@dataclass(slots=True)
class _VideoAccumulator:
    """Mutable scratch state for one ``m=video`` block while parsing (ADR-0044)."""

    port: int = 0
    protocol: str = ""
    fmt_order: list[int] = field(default_factory=list)
    rtpmaps: dict[int, tuple[str, int, int]] = field(default_factory=dict)
    fmtps: dict[int, str] = field(default_factory=dict)
    direction: str = "sendrecv"
    rtcp_mux: bool = False
    mid: str | None = None

    def set_media_line(self, value: str) -> None:
        """Apply an ``m=video <port> <proto> <fmt...>`` line.

        Raises:
            SdpError: If the line is truncated or carries a non-integer port or
                payload type.
        """
        fields = value.split()
        if len(fields) < _M_AUDIO_MIN_FIELDS:
            msg = f"malformed m=video line: {value!r}"
            raise SdpError(msg)
        try:
            self.port = int(fields[1])
            self.fmt_order = [int(pt) for pt in fields[3:]]
        except ValueError as exc:
            msg = f"malformed m=video line: {value!r}"
            raise SdpError(msg) from exc
        self.protocol = fields[2]

    def add_attribute(self, value: str) -> None:
        """Fold one media-level ``a=`` attribute into the accumulator.

        Raises:
            SdpError: If an ``rtpmap``/``fmtp`` value is malformed.
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
            rate = int(rate_str) if rate_str else VIDEO_DEFAULT_CLOCK_RATE
            channels = int(ch_str) if ch_str else 1
            self.rtpmaps[int(pt_str)] = (encoding, rate, channels)
        elif tag == "fmtp":
            pt_str, _, params = rest.partition(" ")
            self.fmtps[int(pt_str)] = params.strip()
        elif tag == "mid":
            self.mid = rest.strip()
        elif value in _DIRECTIONS:
            self.direction = value
        elif value == "rtcp-mux":
            self.rtcp_mux = True

    def build(self) -> VideoMedia:
        """Resolve the accumulated state into an immutable :class:`VideoMedia`."""
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
        return VideoMedia(
            port=self.port,
            protocol=self.protocol,
            codecs=tuple(codecs),
            direction=self.direction,
            mid=self.mid,
            rtcp_mux=self.rtcp_mux,
        )


@dataclass(slots=True)
class _AudioAccumulator:
    """Mutable scratch state for one ``m=audio`` block while parsing.

    ``crypto`` accumulates raw ``a=crypto`` bodies, which carry the SDES inline
    master key||salt; it is suppressed from ``repr`` (``field(repr=False)``) so a
    debug log or traceback local of this scratch object cannot leak key material,
    consistent with the public :class:`CryptoAttribute`/:class:`AudioMedia`.
    """

    port: int = 0
    protocol: str = ""
    fmt_order: list[int] = field(default_factory=list)
    rtpmaps: dict[int, tuple[str, int, int]] = field(default_factory=dict)
    fmtps: dict[int, str] = field(default_factory=dict)
    crypto: list[str] = field(default_factory=list, repr=False)
    ptime: int | None = None
    maxptime: int | None = None
    direction: str = "sendrecv"
    connection: str | None = None
    # WebRTC / DTLS-SRTP + ICE attributes (ADR-0016)
    fingerprint: Fingerprint | None = None
    setup: SetupRole | None = None
    ice_ufrag: str | None = None
    ice_pwd: str | None = None
    ice_candidates: list[IceCandidate] = field(default_factory=list)
    rtcp_mux: bool = False
    # Trickle-ICE SDP primitives (ADR-0034, RFC 8838/8839)
    ice_options: tuple[str, ...] = ()
    end_of_candidates: bool = False
    # BUNDLE media identification (a=mid, RFC 5888 / 8843). ADR-0044.
    mid: str | None = None

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

    def _add_attribute(  # noqa: PLR0912 -- a flat SDP attribute dispatch; each branch maps one a= tag to one field
        self, tag: str, rest: str, value: str
    ) -> None:
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
        elif tag == "maxptime":
            # RFC 4566 §6: the maximum packetisation time the peer accepts (ms),
            # an upper bound negotiate_ptime honours (ADR-0056).
            self.maxptime = int(rest.strip())
        elif value in _DIRECTIONS:
            self.direction = value
        # WebRTC attributes (ADR-0016, RFC 5763/8839).  Malformed lines are
        # tolerated: a bad fingerprint or candidate leaves the field None/empty
        # (same leniency as SDES crypto lines) so a single bad attr does not
        # discard the whole m-line.
        elif tag == "fingerprint":
            with contextlib.suppress(SdpError):
                self.fingerprint = Fingerprint.parse(rest.strip())
        elif tag == "setup":
            with contextlib.suppress(SdpError):
                self.setup = SetupRole.parse(rest.strip())
        elif tag == "ice-ufrag":
            self.ice_ufrag = rest.strip()
        elif tag == "ice-pwd":
            self.ice_pwd = rest.strip()
        elif tag == "candidate":
            with contextlib.suppress(SdpError):
                self.ice_candidates.append(IceCandidate.parse(rest.strip()))
        elif tag == "ice-options":
            # RFC 8839 §5.6: space-separated option tokens (e.g. "trickle ice2").
            self.ice_options = tuple(rest.split())
        elif tag == "mid":
            # RFC 5888 media identification (BUNDLE grouping, RFC 8843). ADR-0044.
            self.mid = rest.strip()
        elif value == "end-of-candidates":
            # RFC 8838 §8.2: a value-less attribute marking the end of the
            # candidate generation (non-trickle / half-trickle).
            self.end_of_candidates = True
        elif value == "rtcp-mux":
            self.rtcp_mux = True

    def build(
        self,
        session_connection: str | None,
        session_defaults: _AudioAccumulator | None = None,
    ) -> AudioMedia:
        """Resolve the accumulated state into an immutable :class:`AudioMedia`.

        ``session_defaults`` carries the SDP **session-level** DTLS/ICE
        attributes (a separate accumulator fed the ``a=`` lines that appear
        before the first ``m=`` line). A media section inherits any of
        ``fingerprint`` / ``setup`` / ``ice_ufrag`` / ``ice_pwd`` /
        ``ice_options`` it does not set itself, with the **media-level value
        taking precedence** (RFC 8122 §5 fingerprint, RFC 8839 §4.2 ICE). This
        is what lets a BUNDLE offer that hoists those credentials to the session
        level (e.g. an Asterisk-based gateway) be recognised as a usable WebRTC
        offer instead of being rejected as "missing fingerprint/ICE".
        """
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
        # Lenient: promote only the crypto lines that validate to typed attrs.
        # Malformed/unsupported lines stay in the raw `crypto` tuple (diagnostics)
        # but are not offered as usable keys.
        # Per-line tolerance: a malformed/unsupported line is skipped here but
        # kept in the raw `crypto` tuple above for diagnostics.
        crypto_attrs: list[CryptoAttribute] = []
        for raw in self.crypto:
            try:
                crypto_attrs.append(CryptoAttribute.parse(raw))
            except SdpError:
                continue
        # WebRTC m-lines use ICE for address/port — no c= connection address.
        effective_conn: str | None
        if self.protocol == _WEBRTC_PROFILE:
            effective_conn = None
        else:
            effective_conn = self.connection or session_connection
        # Inherit session-level DTLS/ICE credentials when this media section does
        # not carry its own (media overrides session — RFC 8122 §5 / RFC 8839
        # §4.2). ``ice_candidates`` / ``end_of_candidates`` are intentionally
        # NOT inherited: candidates are per-media (for a BUNDLE offer they ride
        # the first/tag m-line), so a media section's own candidate set stands.
        defaults = session_defaults
        fingerprint = self.fingerprint or (defaults.fingerprint if defaults else None)
        setup = self.setup or (defaults.setup if defaults else None)
        ice_ufrag = self.ice_ufrag or (defaults.ice_ufrag if defaults else None)
        ice_pwd = self.ice_pwd or (defaults.ice_pwd if defaults else None)
        ice_options = self.ice_options or (defaults.ice_options if defaults else ())
        return AudioMedia(
            port=self.port,
            protocol=self.protocol,
            codecs=tuple(codecs),
            crypto=tuple(self.crypto),
            ptime=self.ptime,
            direction=self.direction,
            connection_address=effective_conn,
            crypto_attrs=tuple(crypto_attrs),
            fingerprint=fingerprint,
            setup=setup,
            ice_ufrag=ice_ufrag,
            ice_pwd=ice_pwd,
            ice_candidates=tuple(self.ice_candidates),
            rtcp_mux=self.rtcp_mux,
            ice_options=ice_options,
            end_of_candidates=self.end_of_candidates,
            mid=self.mid,
            maxptime=self.maxptime,
        )


@dataclass(frozen=True, slots=True)
class SessionDescription:
    """A parsed SDP body, reduced to the audio (and optional video) media.

    ``video`` is the first ``m=video`` section if the offer carries one (ADR-0044);
    it is ``None`` for an audio-only offer. Parsing video is strictly additive —
    the audio path is unchanged.
    """

    connection_address: str | None
    audio: AudioMedia | None
    video: VideoMedia | None = None

    @classmethod
    def parse(cls, text: str) -> SessionDescription:  # noqa: PLR0912 - a flat SDP line dispatch (m=/c=/a= across audio + video sections)
        """Parse an SDP body, extracting the first audio + first video media."""
        session_conn: str | None = None
        acc: _AudioAccumulator | None = None
        vacc: _VideoAccumulator | None = None
        # Collects the SESSION-level a= attributes (those before the first m=
        # line). Only its DTLS/ICE fields are consumed by build(); reusing the
        # accumulator keeps a single attribute-parsing code path (DRY) and an
        # unrelated session-level a= line is parsed harmlessly and ignored.
        session_acc = _AudioAccumulator()
        # The section the current a=/c= lines belong to: "audio", "video", or "".
        section = ""
        seen_media = False  # any m= line has started; session-level c= is over

        for raw in text.replace(_CRLF, "\n").split("\n"):
            line = raw.strip()
            if len(line) < _MIN_LINE_LEN or line[1] != "=":
                continue
            kind, value = line[0], line[2:]
            if kind == "m":
                seen_media = True
                if value.startswith("audio") and acc is None:
                    acc = _AudioAccumulator()
                    acc.set_media_line(value)
                    section = "audio"
                elif value.startswith("video") and vacc is None:
                    vacc = _VideoAccumulator()
                    vacc.set_media_line(value)
                    section = "video"
                else:
                    # A second audio/video, or an unsupported media type: ignore
                    # its attributes (we keep only the first of each).
                    section = ""
            elif kind == "c":
                fields = value.split()
                addr = (
                    fields[_CONN_ADDR_FIELD] if len(fields) > _CONN_ADDR_FIELD else None
                )
                if not seen_media:
                    session_conn = addr  # session-level c= precedes any media
                elif section == "audio" and acc is not None:
                    acc.connection = addr  # media-level c= for the audio section
            elif kind == "a":
                if section == "audio" and acc is not None:
                    acc.add_attribute(value)
                elif section == "video" and vacc is not None:
                    vacc.add_attribute(value)
                elif not seen_media:
                    # Session-level a= (RFC 8122 §5 fingerprint, RFC 8839 §4.2
                    # ICE): captured here and applied to the audio media as a
                    # default in build() when the media block does not override.
                    session_acc.add_attribute(value)

        audio = acc.build(session_conn, session_acc) if acc is not None else None
        video = vacc.build() if vacc is not None else None
        return cls(connection_address=session_conn, audio=audio, video=video)


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


def negotiate_ptime(
    offer_ptime: int | None,
    offer_maxptime: int | None,
    *,
    supported: Sequence[int],
    default: int,
) -> int:
    """Choose the RTP packetisation time (ms) to frame and answer with (ADR-0056).

    The engine no longer assumes 20 ms: it honours the peer's requested framing
    when it can carry it. The peer expresses framing via ``a=ptime`` (its
    preferred packet duration) and optionally ``a=maxptime`` (the largest it will
    accept) — RFC 4566 §6 / RFC 3551. ptime is a *preference*, not a mandate, so
    an unsupported request falls back rather than failing the call.

    Selection:

    * Use ``offer_ptime`` when it is one the engine ``supported`` frame sizes AND
      within ``offer_maxptime`` (if given).
    * Otherwise use ``default`` — but if even ``default`` exceeds
      ``offer_maxptime``, fall to the LARGEST ``supported`` value that fits the
      cap (a peer that caps below 20 ms gets the biggest framing it allows).
    * If nothing fits the cap (a pathological maxptime below every supported
      value), return ``default`` unchanged — the engine still emits valid RTP and
      the peer can reject; we never return an unsupported or non-positive value.

    Args:
        offer_ptime: The peer's ``a=ptime`` in ms, or ``None`` if absent.
        offer_maxptime: The peer's ``a=maxptime`` in ms, or ``None`` if absent.
        supported: The frame sizes (ms) the engine can carry, e.g. ``(20, 30, 40)``.
            Must be non-empty and all positive.
        default: The framing to use when the offer's is unusable (RFC 3551's 20 ms).
            Must be positive and one of ``supported`` — the engine's own default
            must be carriable, so a misconfiguration is a loud error, not a silent
            invalid ptime on the wire.

    Returns:
        The agreed packetisation time in ms.

    Raises:
        ValueError: If ``supported`` is empty or has a non-positive value, or
            ``default`` is non-positive or not in ``supported`` (a programming
            error: the engine cannot frame at its own default).
    """
    supported_set = set(supported)
    if not supported_set:
        msg = "supported ptimes must be a non-empty set of frame sizes"
        raise ValueError(msg)
    if any(p <= 0 for p in supported_set):
        msg = f"supported ptimes must all be positive, got {sorted(supported_set)}"
        raise ValueError(msg)
    if default <= 0:
        msg = f"default ptime must be positive, got {default}"
        raise ValueError(msg)
    if default not in supported_set:
        msg = (
            f"default ptime {default} must be one of the supported frame sizes "
            f"{sorted(supported_set)} (the engine must be able to frame at its default)"
        )
        raise ValueError(msg)

    def _within_cap(value: int) -> bool:
        return offer_maxptime is None or value <= offer_maxptime

    if (
        offer_ptime is not None
        and offer_ptime in supported_set
        and _within_cap(offer_ptime)
    ):
        return offer_ptime
    if _within_cap(default):
        return default
    # The default overruns the cap: pick the largest supported value that fits.
    fitting = [p for p in supported_set if _within_cap(p)]
    if fitting:
        return max(fitting)
    return default


def _fmtp_param(fmtp: str, key: str) -> str | None:
    """Return the value of a ``;``-separated fmtp parameter, or ``None``.

    SDP fmtp lines carry ``key=value`` parameters separated by ``;`` (RFC 6184
    §8.1 for H.264). The key is matched case-insensitively after trimming
    whitespace, and the trimmed value is returned. This is an exact-parameter
    parse — an unanchored substring test mismatches (e.g. ``"packetization-mode=1"
    in "packetization-mode=10"`` is wrongly true).
    """
    for entry in fmtp.split(";"):
        name, sep, value = entry.strip().partition("=")
        if sep and name.strip().lower() == key:
            return value.strip()
    return None


def negotiate_video_h264(offer: VideoMedia) -> Codec | None:
    """Choose the H.264 codec to answer for a video offer (ADR-0044).

    Selects an offered H.264 codec whose ``fmtp`` declares ``packetization-mode``
    exactly ``1`` (FU-A capable — required to fragment large IDR frames). Returns
    ``None`` — declining video — when:

    * the offer carries no H.264 codec (e.g. a VP8-only offer); OR
    * every offered H.264 codec is ``packetization-mode=0`` (or omits
      ``packetization-mode``, whose RFC 6184 §8.1 default is mode 0). Our RFC 6184
      packetiser FU-A-fragments any large NAL, which violates the mode-0
      single-NAL-only contract, so we MUST NOT emit FU-A under mode 0 — we decline
      video and the adapter rejects the m-line (RFC 3264 §6, port 0).

    The fmtp is parsed into its ``;``-separated parameters (see :func:`_fmtp_param`)
    and the value compared exactly to ``"1"`` — not matched as a substring, which
    would wrongly accept ``packetization-mode=10`` / ``x-packetization-mode=1``.

    Args:
        offer: The peer's offered video media.

    Returns:
        A ``packetization-mode=1`` H.264 :class:`Codec` (with its offered ``fmtp``
        preserved), or ``None`` if none is offered.
    """
    for codec in offer.codecs:
        if codec.encoding.lower() != _H264 or codec.fmtp is None:
            continue
        mode = _fmtp_param(codec.fmtp, _PACKETIZATION_MODE_KEY)
        if mode == _PACKETIZATION_MODE_FU_A:
            return codec
    return None


def _order_opus_first(codecs: Sequence[Codec]) -> tuple[Codec, ...]:
    """Reorder to Opus, then G.711 (PCMU/PCMA), then everything else.

    A no-op when no Opus codec is present: the supplied order is preserved
    exactly (ADR-0005 only mandates promoting Opus ahead of G.711 when Opus is
    offered). Relative order within each band is kept stable.
    """
    if not any(c.encoding.lower() == _OPUS for c in codecs):
        return tuple(codecs)

    def band(codec: Codec) -> int:
        if codec.encoding.lower() == _OPUS:
            return 0
        if codec.encoding.upper() in _G711_ENCODINGS:
            return 1
        return 2

    return tuple(sorted(codecs, key=band))


def _coerce_crypto(crypto: CryptoAttribute | str | None) -> CryptoAttribute | None:
    """Normalise the ``crypto`` builder argument to a validated attribute.

    Accepts three shapes:

    * an already-built :class:`CryptoAttribute` (returned as-is — it
      self-validates on construction);
    * a *tagless* key string ``<suite> <key-params>`` (the common caller form:
      the builder supplies the SRTP key and lets the tag default to
      :data:`_DEFAULT_CRYPTO_TAG` for an initial offer);
    * a *tagged* string ``<tag> <suite> <key-params>`` (when the caller wants a
      specific negotiation tag).

    The two string shapes are disambiguated by the first whitespace token: a
    purely decimal first token is the tag; otherwise the default tag is
    prepended. Both then go through :meth:`CryptoAttribute.parse`, so the same
    RFC 4568 validation (supported suite, ``inline:`` key, correct key||salt
    length) applies. ``None`` stays ``None`` (plain ``RTP/AVP``).

    Raises:
        SdpError: If a supplied string fails RFC 4568 validation.
    """
    if crypto is None or isinstance(crypto, CryptoAttribute):
        return crypto
    first = crypto.split(maxsplit=1)
    body = crypto if first and first[0].isdigit() else f"{_DEFAULT_CRYPTO_TAG} {crypto}"
    return CryptoAttribute.parse(body)


def _build_audio_body(  # noqa: PLR0913 - SDP fields are independent; all keyword-only
    *,
    local_address: str,
    port: int,
    codecs: Sequence[Codec],
    direction: str,
    ptime: int,
    session_id: int,
    sess_version: int,
    crypto: CryptoAttribute | None,
) -> str:
    """Emit the SDP body shared by offer and answer (SDES or plain RTP paths).

    The profile is ``RTP/SAVP`` with a single ``a=crypto`` line carrying the
    given attribute's tag/suite/key (RFC 4568 SDES) when ``crypto`` is supplied,
    otherwise plain ``RTP/AVP``. Validation of ``direction``/``codecs``/``port``/
    ``ptime`` happens here so both callers enforce the same invariants; the
    crypto attribute is already validated by construction. The SRTP master
    key/salt is the caller's; this function never generates key material.

    For the WebRTC / DTLS-SRTP path, use :func:`_build_webrtc_body` instead.
    """
    if direction not in _DIRECTIONS:
        msg = f"invalid SDP direction: {direction!r}"
        raise ValueError(msg)
    if not codecs:
        msg = "an SDP audio body needs at least one codec"
        raise ValueError(msg)
    if not 1 <= port <= _MAX_PORT:
        msg = f"RTP port out of range 1..{_MAX_PORT}: {port}"
        raise ValueError(msg)
    if ptime <= 0:
        msg = f"ptime must be positive, got {ptime}"
        raise ValueError(msg)
    profile = _SECURE_PROFILE if crypto is not None else _PLAIN_PROFILE
    payloads = " ".join(str(c.payload_type) for c in codecs)
    lines = [
        "v=0",
        f"o=- {session_id} {sess_version} IN IP4 {local_address}",
        "s=-",
        f"c=IN IP4 {local_address}",
        "t=0 0",
        f"m=audio {port} {profile} {payloads}",
    ]
    for codec in codecs:
        rate = f"{codec.encoding}/{codec.clock_rate}"
        if codec.channels != 1:
            rate = f"{rate}/{codec.channels}"
        lines.append(f"a=rtpmap:{codec.payload_type} {rate}")
        if codec.fmtp is not None:
            lines.append(f"a=fmtp:{codec.payload_type} {codec.fmtp}")
    if crypto is not None:
        lines.append(f"a=crypto:{crypto.render()}")
    lines.append(f"a=ptime:{ptime}")
    lines.append(f"a={direction}")
    return _CRLF.join(lines) + _CRLF


def _rtpmap_lines(codec: Codec) -> list[str]:
    """The ``a=rtpmap`` (+ optional ``a=fmtp``) line(s) for one codec."""
    rate = f"{codec.encoding}/{codec.clock_rate}"
    if codec.channels != 1:
        rate = f"{rate}/{codec.channels}"
    lines = [f"a=rtpmap:{codec.payload_type} {rate}"]
    if codec.fmtp is not None:
        lines.append(f"a=fmtp:{codec.payload_type} {codec.fmtp}")
    return lines


def _build_video_section(video: VideoAnswer) -> list[str]:
    """The ``m=video`` answer section lines (ADR-0044, RFC 6184/8843/3264).

    Three shapes (see :class:`VideoAnswerMode`):

    * ``REJECTED`` — ``m=video 0 <proto> <pt>`` + ``a=mid`` only. RFC 3264 §6:
      port 0 declines the stream; the m-line (and its mid) is KEPT so the answer's
      m-line count/order matches the offer (RFC 3264 §5.1). The rejected stream is
      excluded from the BUNDLE group by :func:`_build_webrtc_body`
      (RFC 8843 §7.3.3), so no fingerprint/ICE/rtpmap is needed here.
    * ``SENDONLY`` / ``INACTIVE`` — a real (non-zero) placeholder port, the
      negotiated H.264 ``a=rtpmap``, ``a=rtcp-mux``, ``a=mid``, and the direction;
      DTLS fingerprint + ICE are shared with audio via BUNDLE (RFC 8843), so they
      are not repeated here. The real media address is the shared ICE 5-tuple.

    We answer ``a=sendonly`` (never ``a=sendrecv``) even when sending: the plugin
    discards ALL inbound video, and ``a=sendrecv`` would solicit inbound video
    onto the shared BUNDLE 5-tuple. The inbound-audio :class:`SrtpSession` is not
    pre-bound to an SSRC (``media/dtls.py`` ``derive_srtp_sessions``), so an early
    inbound video packet would bind it to the video SSRC and then silently drop
    all inbound audio — a silent inbound-audio outage. ``a=sendonly`` tells the
    peer not to send video at all, eliminating the race. ``a=inactive`` (no
    source) is the RFC-correct "present but no media" answer.
    """
    if video.mode is VideoAnswerMode.REJECTED:
        # RFC 3264 §6: reject with port 0, but keep the m-line + mid so the
        # answer mirrors the offer's m-line count/order (RFC 3264 §5.1). No
        # rtpmap/direction — there is no agreed codec. The offered transport
        # profile is echoed (we never force UDP/TLS/RTP/SAVPF onto a profile the
        # offer did not propose).
        return [
            f"m=video 0 {video.proto} {video.payload_type}",
            f"a=mid:{video.mid}",
        ]
    lines = [f"m=video 9 {video.proto} {video.payload_type}"]
    if video.codec is not None:
        lines.extend(_rtpmap_lines(video.codec))
    lines.append("a=rtcp-mux")
    lines.append(f"a=mid:{video.mid}")
    lines.append(
        "a=sendonly" if video.mode is VideoAnswerMode.SENDONLY else "a=inactive"
    )
    return lines


def _build_webrtc_body(  # noqa: PLR0913 - WebRTC SDP fields are independent; all keyword-only
    *,
    local_address: str,
    port: int,
    codecs: Sequence[Codec],
    direction: str,
    ptime: int,
    session_id: int,
    sess_version: int,
    fingerprint: Fingerprint,
    setup: SetupRole,
    ice_ufrag: str,
    ice_pwd: str,
    ice_candidates: Sequence[IceCandidate],
    audio_mid: str | None = None,
    video: VideoAnswer | None = None,
) -> str:
    """Emit a WebRTC SDP body (``UDP/TLS/RTP/SAVPF``, ADR-0016 + ADR-0044 video).

    Per RFC 5763 §5:
    - The ``c=`` session connection-address line is omitted (address is
      conveyed by ICE candidates).
    - The ``a=connection`` attribute (RFC 4145) MUST NOT appear.
    - No ``a=crypto`` (SDES keying is forbidden on WebRTC m-lines per RFC 8827
      §6.5).

    Includes ``a=fingerprint``, ``a=setup``, ``a=ice-ufrag``, ``a=ice-pwd``,
    one ``a=candidate`` per entry, and ``a=rtcp-mux``.

    When ``video`` is supplied (ADR-0044), an ``a=group:BUNDLE`` line, an audio
    ``a=mid``, and a BUNDLE'd ``m=video`` section sharing this fingerprint/ICE are
    added (RFC 8843). When it is ``None`` the body is byte-identical to the
    audio-only WebRTC answer (no group, no mid, no video) — the audio-only
    regression invariant.

    ``local_address`` is used in the ``o=`` origin line only (RFC 4566 §5.2);
    it does not appear in a ``c=`` line (which is omitted per RFC 5763 §5).
    """
    if direction not in _DIRECTIONS:
        msg = f"invalid SDP direction: {direction!r}"
        raise ValueError(msg)
    if not codecs:
        msg = "an SDP audio body needs at least one codec"
        raise ValueError(msg)
    if not 1 <= port <= _MAX_PORT:
        msg = f"RTP port out of range 1..{_MAX_PORT}: {port}"
        raise ValueError(msg)
    if ptime <= 0:
        msg = f"ptime must be positive, got {ptime}"
        raise ValueError(msg)
    payloads = " ".join(str(c.payload_type) for c in codecs)
    # RFC 5763 §5: no c= line on a DTLS-SRTP m-line (connection address is
    # conveyed by ICE candidates, not a c= attribute).
    lines = [
        "v=0",
        f"o=- {session_id} {sess_version} IN IP4 {local_address}",
        "s=-",
        "t=0 0",
    ]
    # BUNDLE group line (session level, RFC 8843) — only when answering video, so
    # an audio-only answer stays byte-identical to before (no a=group). A REJECTED
    # video (port 0) is excluded from the group (RFC 8843 §7.3.3): only the audio
    # mid is bundled, though the rejected m-line is still emitted for m-line
    # correspondence.
    if video is not None and audio_mid is not None:
        if video.mode is VideoAnswerMode.REJECTED:
            lines.append(f"a=group:BUNDLE {audio_mid}")
        else:
            lines.append(f"a=group:BUNDLE {audio_mid} {video.mid}")
    lines.append(f"m=audio {port} {_WEBRTC_PROFILE} {payloads}")
    for codec in codecs:
        lines.extend(_rtpmap_lines(codec))
    # DTLS-SRTP keying attributes (RFC 5763 §5, RFC 4572).
    lines.append(f"a=fingerprint:{fingerprint.render()}")
    lines.append(f"a=setup:{setup.render()}")
    # ICE credentials, options, and candidates (RFC 8839 §5.4, §5.6, §5.1).
    lines.append(f"a=ice-ufrag:{ice_ufrag}")
    lines.append(f"a=ice-pwd:{ice_pwd}")
    # Advertise trickle + ICE2 (RFC 8838 §4.1 / RFC 8445). Safe in a full-candidate
    # answer: we list all candidates and then mark end-of-candidates, the
    # half-trickle degenerate case that interoperates with classic + trickle peers.
    lines.append("a=ice-options:trickle ice2")
    for cand in ice_candidates:
        lines.append(f"a=candidate:{cand.render()}")
    # end-of-candidates (RFC 8838 §8.2): our candidate generation is complete.
    lines.append("a=end-of-candidates")
    # rtcp-mux (RFC 5761): RTP + RTCP share the one ICE-selected 5-tuple.
    lines.append("a=rtcp-mux")
    lines.append(f"a=ptime:{ptime}")
    lines.append(f"a={direction}")
    if video is not None and audio_mid is not None:
        lines.append(f"a=mid:{audio_mid}")
        lines.extend(_build_video_section(video))
    return _CRLF.join(lines) + _CRLF


def build_audio_offer(  # noqa: PLR0913 - SDP fields are independent; all keyword-only
    *,
    local_address: str,
    port: int,
    codecs: Sequence[Codec],
    direction: str = "sendrecv",
    ptime: int = 20,
    session_id: int = 0,
    version: int | None = None,
    crypto: CryptoAttribute | str | None = None,
) -> str:
    """Build an SDP audio offer/answer body (RTP/AVP, or RTP/SAVP with crypto).

    This is the SDES / plain-RTP path only (SIP-over-TLS transport, ADR-0013).
    For the WebRTC / DTLS-SRTP path (``UDP/TLS/RTP/SAVPF``), use
    :func:`build_webrtc_answer`.

    Args:
        local_address: The address we receive RTP on (IPv4 literal).
        port: The RTP port we receive on.
        codecs: The codecs to offer, in preference order. When an Opus codec is
            present it is promoted ahead of G.711 (ADR-0005); otherwise the given
            order is preserved.
        direction: Media direction attribute.
        ptime: Packetisation time in ms.
        session_id: SDP ``o=`` session id (constant for the life of a dialog).
        version: SDP ``o=`` session version. A re-offer keeps ``session_id``
            constant and increments ``version`` (RFC 4566 §5.2, ADR-0011
            invariant 1). Defaults to ``session_id`` for an initial offer.
        crypto: SDES keying for SRTP (RFC 4568), as a validated
            :class:`CryptoAttribute` or its string body
            (``<tag> AES_CM_128_HMAC_SHA1_80 inline:<base64-key||salt>``). When
            supplied, the profile becomes ``RTP/SAVP`` and the attribute's own
            tag/suite/key is emitted as the ``a=crypto`` line — a string is
            validated (suite + 30-octet inline key) and rejected if malformed.
            The key material is the caller's, never generated here.

    Returns:
        The SDP body terminated by CRLF, ready to attach to an INVITE/200.

    Raises:
        ValueError: If ``direction`` is invalid, ``codecs`` is empty, ``port`` is
            outside ``1..65535``, or ``ptime`` is not positive.
        SdpError: If ``crypto`` is a string that fails RFC 4568 validation.
    """
    sess_version = session_id if version is None else version
    return _build_audio_body(
        local_address=local_address,
        port=port,
        codecs=_order_opus_first(codecs),
        direction=direction,
        ptime=ptime,
        session_id=session_id,
        sess_version=sess_version,
        crypto=_coerce_crypto(crypto),
    )


def build_audio_answer(  # noqa: PLR0913 - SDP fields are independent; all keyword-only
    offer: SessionDescription,
    *,
    local_address: str,
    port: int,
    supported: Sequence[str],
    ptime: int = 20,
    session_id: int = 0,
    version: int | None = None,
    crypto: CryptoAttribute | str | None = None,
) -> str:
    """Build an SDP answer to ``offer`` (RFC 3264 §6.1).

    This is the SDES / plain-RTP answer path (SIP-over-TLS transport,
    ADR-0013).  For a WebRTC / DTLS-SRTP offer (``UDP/TLS/RTP/SAVPF``), use
    :func:`build_webrtc_answer` instead.

    Negotiates the codecs common to the offer and ``supported`` (in the offer's
    preference order, via :func:`negotiate_audio`), mirrors the offered
    direction (sendrecv->sendrecv, sendonly->recvonly, recvonly->sendonly,
    inactive->inactive), and answers a secured (``RTP/SAVP``) offer by selecting
    a supported offered crypto and emitting an ``a=crypto`` that echoes that
    accepted offer's **tag and suite** (RFC 4568: the answerer identifies its
    choice by the offer's tag) but carries **our own** key material from
    ``crypto``. The answer's payload ordering follows the offer, not
    ``supported`` — the answerer honours the offerer's preference.

    Args:
        offer: The parsed peer offer to answer; must carry audio media.
        local_address: The address we receive RTP on (IPv4 literal).
        port: The RTP port we receive on.
        supported: Encoding names we can handle (case-insensitive).
        ptime: Packetisation time in ms.
        session_id: SDP ``o=`` session id for the answer.
        version: SDP ``o=`` session version (defaults to ``session_id``).
        crypto: Our SDES keying (RFC 4568), as a :class:`CryptoAttribute` or its
            string body — required to answer an ``RTP/SAVP`` offer, ignored for a
            plain ``RTP/AVP`` offer. Only its key material is used; the emitted
            tag and suite come from the accepted offer. Validated (suite +
            30-octet inline key); a malformed key is rejected.

    Returns:
        The SDP answer body terminated by CRLF, ready to attach to a 200 OK.

    Raises:
        SdpError: If the offer has no audio media, shares no usable voice codec
            (e.g. a telephone-event-only offer), or is secured (``RTP/SAVP``)
            while no ``crypto`` was supplied or no supported, well-formed crypto
            was offered to key the answer.
    """
    audio = offer.audio
    if audio is None:
        msg = "cannot answer an offer with no audio media"
        raise SdpError(msg)
    try:
        chosen = negotiate_audio(audio, supported)
    except ValueError as exc:
        # negotiate_audio raises plain ValueError; surface as SdpError so callers
        # handle one inbound-SDP error type. Preserves the "no common audio
        # codec" message (covers the telephone-event-only rejection).
        raise SdpError(str(exc)) from exc
    answer_crypto = _negotiate_answer_crypto(audio, crypto) if audio.is_srtp else None
    sess_version = session_id if version is None else version
    return _build_audio_body(
        local_address=local_address,
        port=port,
        codecs=chosen,
        direction=_ANSWER_DIRECTION[audio.direction],
        ptime=ptime,
        session_id=session_id,
        sess_version=sess_version,
        crypto=answer_crypto,
    )


def build_webrtc_answer(  # noqa: PLR0913 - WebRTC SDP fields are independent; all keyword-only
    offer: SessionDescription,
    *,
    local_address: str,
    port: int,
    supported: Sequence[str],
    fingerprint: Fingerprint,
    setup: SetupRole,
    ice_ufrag: str,
    ice_pwd: str,
    ice_candidates: Sequence[IceCandidate],
    ptime: int = 20,
    session_id: int = 0,
    version: int | None = None,
    video: VideoAnswer | None = None,
) -> str:
    """Build a WebRTC SDP answer to a ``UDP/TLS/RTP/SAVPF`` offer (ADR-0016).

    Produces a ``UDP/TLS/RTP/SAVPF`` answer carrying DTLS-SRTP keying
    attributes (``a=fingerprint``, ``a=setup``), ICE credentials and candidates
    (``a=ice-ufrag``, ``a=ice-pwd``, ``a=candidate``), and ``a=rtcp-mux``.

    Per RFC 5763 §5:
    - The answer carries NO ``a=crypto`` (SDES keying is forbidden on WebRTC
      m-lines, RFC 8827 §6.5).
    - No ``c=`` connection-address line (RFC 4145 ``a=connection`` is also
      forbidden; address is conveyed by ICE candidates).
    - ``setup`` MUST be ``active`` or ``passive`` — the answerer MUST NOT
      offer ``actpass`` (RFC 5763 §5).

    The direction mirrors the offer's direction per RFC 3264 §6.1 (not a
    caller-supplied value — the answerer has no independent direction choice on
    a WebRTC answer).

    Codec negotiation (via :func:`negotiate_audio`) follows the offer's
    preference order, not ``supported``.

    Args:
        offer: The parsed peer WebRTC offer; must carry ``UDP/TLS/RTP/SAVPF``
            audio media.
        local_address: Used in the ``o=`` origin line (RFC 4566 §5.2); ICE
            candidates carry the real media addresses so no ``c=`` is emitted.
        port: The port in the ``m=`` line.  The actual media address and port
            are conveyed by ``ice_candidates``.
        supported: Encoding names we handle (case-insensitive).
        fingerprint: Our DTLS certificate fingerprint.
        setup: Our DTLS role (``active`` or ``passive``; never ``actpass``).
        ice_ufrag: Our ICE username fragment.
        ice_pwd: Our ICE password.
        ice_candidates: Our gathered ICE candidates (all of them for non-trickle
            MVP, RFC 8838 §3).
        ptime: Packetisation time in ms.
        session_id: SDP ``o=`` session id.
        version: SDP ``o=`` session version (defaults to ``session_id``).
        video: When supplied AND the offer carries an ``m=video`` section
            (ADR-0044), a BUNDLE'd ``m=video`` answer is appended sharing this
            answer's fingerprint/ICE/setup (RFC 8843), plus the ``a=group:BUNDLE``
            line and the audio ``a=mid``. When ``None`` — or when the offer has no
            video — the answer is audio-only and byte-identical to before (no
            group/mid/video).

    Returns:
        The SDP answer body terminated by CRLF, ready to attach to a 200 OK.

    Raises:
        SdpError: If the offer has no audio media, the offer is not
            ``UDP/TLS/RTP/SAVPF``, no common voice codec is shared, or
            ``setup`` is ``actpass`` (forbidden for an answerer).
    """
    audio = offer.audio
    if audio is None:
        msg = "cannot answer an offer with no audio media"
        raise SdpError(msg)
    if not audio.is_webrtc:
        msg = (
            "build_webrtc_answer requires a WebRTC (UDP/TLS/RTP/SAVPF) offer; "
            f"got profile {audio.protocol!r}"
        )
        raise SdpError(msg)
    if setup.value == "actpass":
        msg = (
            "build_webrtc_answer: setup=actpass is forbidden for an answerer "
            "(RFC 5763 §5); use active or passive"
        )
        raise SdpError(msg)
    try:
        chosen = negotiate_audio(audio, supported)
    except ValueError as exc:
        raise SdpError(str(exc)) from exc
    sess_version = session_id if version is None else version
    # Only answer video when the offer actually carried an m=video AND the caller
    # supplied a VideoAnswer. A video param for an audio-only offer is ignored
    # (no video is invented). The audio a=mid comes from the offer, defaulting to
    # "0" if the offer omitted it (a BUNDLE offer always carries mids).
    answer_video = video if offer.video is not None else None
    audio_mid = audio.mid or "0"
    return _build_webrtc_body(
        local_address=local_address,
        port=port,
        codecs=chosen,
        direction=_ANSWER_DIRECTION[audio.direction],
        ptime=ptime,
        session_id=session_id,
        sess_version=sess_version,
        fingerprint=fingerprint,
        setup=setup,
        ice_ufrag=ice_ufrag,
        ice_pwd=ice_pwd,
        ice_candidates=ice_candidates,
        audio_mid=audio_mid if answer_video is not None else None,
        video=answer_video,
    )


def build_webrtc_offer(  # noqa: PLR0913 - WebRTC SDP fields are independent; all keyword-only
    *,
    local_address: str,
    port: int,
    codecs: Sequence[Codec],
    fingerprint: Fingerprint,
    setup: SetupRole,
    ice_ufrag: str,
    ice_pwd: str,
    ice_candidates: Sequence[IceCandidate],
    direction: str = "sendrecv",
    ptime: int = 20,
    session_id: int = 0,
    version: int | None = None,
) -> str:
    """Build a WebRTC SDP *offer* (``UDP/TLS/RTP/SAVPF``, ADR-0049).

    The outbound (UAC) counterpart to :func:`build_webrtc_answer`: it emits the
    same ``UDP/TLS/RTP/SAVPF`` body (shared :func:`_build_webrtc_body`) carrying
    our DTLS-SRTP keying (``a=fingerprint`` + ``a=setup``), ICE credentials and
    candidates (``a=ice-ufrag`` / ``a=ice-pwd`` / ``a=candidate``), and
    ``a=rtcp-mux`` — but with OUR codec menu (``codecs``, Opus-promoted per
    ADR-0005) rather than a negotiated subset of a peer's offer, and a
    caller-chosen ``direction``.

    Unlike :func:`build_webrtc_answer`, ``setup`` MAY be ``actpass`` (RFC 5763 §5:
    only the *answerer* MUST NOT offer ``actpass``). The outbound origination path
    offers a concrete ``active`` (we are the DTLS CLIENT) so the
    :class:`~hermes_voip.media.dtls.DtlsEndpoint` role — fixed at construction
    along with the fingerprint we put here — needs no post-answer switch.

    Per RFC 5763 §5 / RFC 8827 §6.5: NO ``a=crypto`` (SDES is forbidden on a WebRTC
    m-line) and NO ``c=`` connection-address line (the address is conveyed by ICE).

    Args:
        local_address: Used in the ``o=`` origin line only (RFC 4566 §5.2); the
            media address/port are conveyed by ``ice_candidates``.
        port: The advisory port in the ``m=`` line.
        codecs: Our offered codecs in preference order (Opus is promoted ahead of
            G.711 per ADR-0005 when present).
        fingerprint: Our DTLS certificate fingerprint.
        setup: Our DTLS role (``active``, ``passive``, or ``actpass``).
        ice_ufrag: Our ICE username fragment.
        ice_pwd: Our ICE password.
        ice_candidates: Our gathered ICE candidates (full set; half-trickle MVP).
        direction: Media direction attribute (default ``sendrecv``).
        ptime: Packetisation time in ms.
        session_id: SDP ``o=`` session id.
        version: SDP ``o=`` session version (defaults to ``session_id``).

    Returns:
        The SDP offer body terminated by CRLF, ready to attach to an INVITE.

    Raises:
        ValueError: If ``direction`` is invalid, ``codecs`` is empty, ``port`` is
            out of range, or ``ptime`` is not positive.
    """
    sess_version = session_id if version is None else version
    return _build_webrtc_body(
        local_address=local_address,
        port=port,
        codecs=_order_opus_first(codecs),
        direction=direction,
        ptime=ptime,
        session_id=session_id,
        sess_version=sess_version,
        fingerprint=fingerprint,
        setup=setup,
        ice_ufrag=ice_ufrag,
        ice_pwd=ice_pwd,
        ice_candidates=ice_candidates,
    )


def generate_answer_crypto(accepted: CryptoAttribute) -> CryptoAttribute:
    """Mint a fresh SDES answer ``a=crypto`` for an accepted offer crypto (RFC 4568).

    Generates a cryptographically-random master key||salt of the length the
    accepted suite requires and returns a :class:`CryptoAttribute` echoing the
    accepted tag + suite with **our** key. Per RFC 4568 §6.1 each direction is
    keyed by the *sender*: this is the key WE use to encrypt our **outbound** SRTP
    (and advertise in the answer); the offerer's key (theirs) keys our inbound.

    The returned attribute's key never reaches a log/`repr` (``key_params`` is
    ``field(repr=False)``), and is generated with :mod:`secrets`, not ``random``.

    Args:
        accepted: The offered crypto we accepted (``audio.crypto_attrs[0]``),
            already validated as a supported, well-formed suite.

    Returns:
        Our answer :class:`CryptoAttribute` (same tag + suite, our random key).
    """
    octets = _SRTP_KEY_SALT_OCTETS[accepted.suite]
    key_b64 = base64.b64encode(secrets.token_bytes(octets)).decode("ascii")
    return CryptoAttribute(
        tag=accepted.tag,
        suite=accepted.suite,
        key_params=f"{_INLINE_PREFIX}{key_b64}",
    )


def _negotiate_answer_crypto(
    audio: AudioMedia, our_crypto: CryptoAttribute | str | None
) -> CryptoAttribute:
    """Pick the answer's ``a=crypto`` for a secured (SAVP) offer (RFC 4568).

    Selects the first supported, well-formed crypto the offer carried
    (``audio.crypto_attrs`` already excludes malformed/unsupported lines), and
    returns a :class:`CryptoAttribute` echoing that accepted offer's tag + suite
    with our own key material taken from ``our_crypto``.

    Raises:
        SdpError: If ``our_crypto`` is missing, the offer carried no supported
            well-formed crypto, or our supplied key fails validation for the
            accepted suite.
    """
    if our_crypto is None:
        msg = "cannot answer an RTP/SAVP offer without a crypto key"
        raise SdpError(msg)
    if not audio.crypto_attrs:
        msg = "RTP/SAVP offer carried no supported, well-formed a=crypto to accept"
        raise SdpError(msg)
    accepted = audio.crypto_attrs[0]
    our_attr = _coerce_crypto(our_crypto)
    # our_attr is non-None: our_crypto was non-None above and _coerce_crypto only
    # returns None for a None input.
    assert our_attr is not None  # noqa: S101 - narrowing for the type checker
    # Echo the accepted offer's tag + suite, but with OUR key material. Rebuilding
    # re-validates our key length against the accepted suite.
    return CryptoAttribute(
        tag=accepted.tag, suite=accepted.suite, key_params=our_attr.key_params
    )
