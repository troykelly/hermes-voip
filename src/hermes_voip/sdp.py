"""SDP (RFC 4566) parsing, building, and offer/answer negotiation (RFC 3264).

Scoped to what a telephony endpoint needs: one audio media description with its
codecs (PCMU/PCMA/telephone-event), the media-security profile (RTP/AVP vs
RTP/SAVP + SDES ``a=crypto`` lines), ptime, and direction. Video and other media
are ignored. Addresses are passed in by the transport; none are hard-coded.
"""

from __future__ import annotations

import base64
import binascii
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
_SECURE_PROFILE = "RTP/SAVP"
_PLAIN_PROFILE = "RTP/AVP"
# RFC 4568 SDES. We negotiate the two AES_CM_128 SRTP crypto-suites: with an
# 80-bit and a 32-bit HMAC-SHA1 auth tag. Both use a 128-bit (16-octet) master
# key and 112-bit (14-octet) master salt — they differ only in the SRTP/SRTCP
# auth-tag length (RFC 4568 §6.2) — so for both the inline key||salt decodes to
# 30 octets. DTLS-SRTP and other suites are out of scope.
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

    DTLS-SRTP fingerprints and ICE are out of scope (deferred to W12); this is
    SDES keying only.

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
        crypto: Raw ``a=crypto`` lines (SDES) for an SRTP profile, verbatim and
            in offer order — kept for diagnostics even when malformed.
        crypto_attrs: The subset of ``crypto`` lines that parse and validate as
            a supported :class:`CryptoAttribute`, in offer order. Parsing is
            lenient: a malformed or unsupported crypto line stays in ``crypto``
            but is excluded here, so this carries only usable offered keys.
        ptime: Packetisation time in ms, if declared.
        direction: ``sendrecv`` / ``sendonly`` / ``recvonly`` / ``inactive``.
        connection_address: The effective connection address for this media.

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

    @property
    def is_srtp(self) -> bool:
        """True when the transport profile is secured (SAVP)."""
        return "SAVP" in self.protocol


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
        return AudioMedia(
            port=self.port,
            protocol=self.protocol,
            codecs=tuple(codecs),
            crypto=tuple(self.crypto),
            ptime=self.ptime,
            direction=self.direction,
            connection_address=self.connection or session_connection,
            crypto_attrs=tuple(crypto_attrs),
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
    """Emit the SDP body shared by offer and answer.

    The profile is ``RTP/SAVP`` with a single ``a=crypto`` line carrying the
    given attribute's tag/suite/key (RFC 4568 SDES) when ``crypto`` is supplied,
    otherwise plain ``RTP/AVP``. Validation of ``direction``/``codecs``/``port``/
    ``ptime`` happens here so both callers enforce the same invariants; the
    crypto attribute is already validated by construction. The SRTP master
    key/salt is the caller's; this function never generates key material.
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
            The key material is the caller's, never generated here. DTLS-SRTP
            fingerprints and ICE candidates are out of scope (deferred to W12).

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

    Negotiates the codecs common to the offer and ``supported`` (in the offer's
    preference order, via :func:`negotiate_audio`), mirrors the offered
    direction (sendrecv->sendrecv, sendonly->recvonly, recvonly->sendonly,
    inactive->inactive), and answers a secured (``RTP/SAVP``) offer by selecting
    a supported offered crypto and emitting an ``a=crypto`` that echoes that
    accepted offer's **tag and suite** (RFC 4568: the answerer identifies its
    choice by the offer's tag) but carries **our own** key material from
    ``crypto``. The answer's payload ordering follows the offer, not
    ``supported`` — the answerer honours the offerer's preference.

    DTLS-SRTP fingerprints and ICE attributes are explicitly out of scope here
    (deferred to W12); this builds SDES-keyed SAVP or plain AVP only.

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
