"""RTP packetisation and a reordering jitter buffer (RFC 3550).

The transport (ADR-0005) packs outbound PCM payloads into RTP and feeds inbound
packets through a :class:`JitterBuffer` that reorders by sequence number, drops
duplicates and late arrivals, and signals loss (so the media loop can conceal)
— all using RFC 1982 serial-number arithmetic so 16-bit sequence wraparound is
handled. SRTP keying lives in the transport; this module is the plaintext RTP
shape.
"""

from __future__ import annotations

__all__ = [
    "JitterBuffer",
    "JitterOutput",
    "Lost",
    "RtpPacket",
]

import struct
from dataclasses import dataclass

_RTP_VERSION = 2
_HEADER_LEN = 12
_EXT_HEADER_LEN = 4
_MAX_PAYLOAD_TYPE = 0x7F
_MARKER_BIT = 0x80
_PADDING_BIT = 0x20
_EXTENSION_BIT = 0x10
_CSRC_MASK = 0x0F
_CSRC_WORD = 4
_SEQ_MOD = 1 << 16
_SEQ_HALF = 1 << 15
_MAX_SEQ = 0xFFFF
_U32 = 0xFFFFFFFF
_DEFAULT_MAX_AHEAD = 256

# Adaptive jitter depth (ADR-0056). The ceiling used when adaptation is enabled
# but no explicit ``max_depth`` is given: 10 packets = 200 ms at the standard
# 20 ms ptime — generous reorder tolerance without an unbounded latency tax.
_DEFAULT_MAX_DEPTH = 10
# Consecutive clean pops (a real in-order packet, no loss and no late drop)
# before the adaptive depth shrinks one step back toward the floor. A run, not a
# single pop, so a calm patch between bursts does not thrash the depth down and
# straight back up; one full window's worth of clean packets is a reasonable
# "the link has settled" signal.
_SHRINK_AFTER = 50


@dataclass(frozen=True, slots=True)
class RtpPacket:
    """An RTP packet's fixed header fields plus its payload (RFC 3550 §5.1).

    Attributes:
        payload_type: The 7-bit payload type (e.g. 0 = PCMU, 101 = telephone-event).
        sequence_number: The 16-bit sequence number.
        timestamp: The 32-bit RTP timestamp.
        ssrc: The 32-bit synchronisation source identifier.
        payload: The media payload bytes.
        marker: The marker bit (e.g. first packet of a talk-spurt / DTMF end).
    """

    payload_type: int
    sequence_number: int
    timestamp: int
    ssrc: int
    payload: bytes
    marker: bool = False

    def __post_init__(self) -> None:
        """Validate that every fixed-width field fits its RFC 3550 range."""
        if not 0 <= self.payload_type <= _MAX_PAYLOAD_TYPE:
            msg = f"payload_type out of range 0..127: {self.payload_type}"
            raise ValueError(msg)
        if not 0 <= self.sequence_number <= _MAX_SEQ:
            msg = f"sequence_number out of range 0..65535: {self.sequence_number}"
            raise ValueError(msg)
        if not 0 <= self.timestamp <= _U32:
            msg = f"timestamp out of range 0..2^32-1: {self.timestamp}"
            raise ValueError(msg)
        if not 0 <= self.ssrc <= _U32:
            msg = f"ssrc out of range 0..2^32-1: {self.ssrc}"
            raise ValueError(msg)

    def pack(self) -> bytes:
        """Serialise to wire bytes (12-byte header, no CSRC/extension/padding)."""
        byte0 = _RTP_VERSION << 6  # V=2, P=0, X=0, CC=0
        byte1 = (_MARKER_BIT if self.marker else 0) | self.payload_type
        header = struct.pack(
            "!BBHII",
            byte0,
            byte1,
            self.sequence_number,
            self.timestamp,
            self.ssrc,
        )
        return header + self.payload

    @classmethod
    def parse(cls, data: bytes) -> RtpPacket:
        """Parse wire bytes into an :class:`RtpPacket`.

        Skips any CSRC list and the RFC 3550 §5.3.1 extension header (if the X
        bit is set), and strips padding, so ``payload`` is the media bytes only.

        When the padding (P) bit is set, RFC 3550 §5.1 puts the pad count in the
        last payload octet and counts that octet itself, so a valid count is
        ``1..len(payload)``. A count of zero, a count exceeding the payload, or
        the P bit with no payload at all is malformed and raises — consistent
        with every other parse path, never a silent leak of the bogus byte.

        Raises:
            ValueError: If the data is too short for its declared fields, the
                RTP version is not 2, or the padding is malformed.
        """
        if len(data) < _HEADER_LEN:
            msg = f"RTP packet too short: {len(data)} bytes"
            raise ValueError(msg)
        byte0, byte1, seq, timestamp, ssrc = struct.unpack("!BBHII", data[:_HEADER_LEN])
        version = byte0 >> 6
        if version != _RTP_VERSION:
            msg = f"unsupported RTP version: {version}"
            raise ValueError(msg)
        offset = _HEADER_LEN + (byte0 & _CSRC_MASK) * _CSRC_WORD
        if len(data) < offset:
            msg = "RTP packet too short for its CSRC count"
            raise ValueError(msg)
        if byte0 & _EXTENSION_BIT:
            if len(data) < offset + _EXT_HEADER_LEN:
                msg = "RTP packet too short for its extension header"
                raise ValueError(msg)
            ext_words = int.from_bytes(data[offset + 2 : offset + 4], "big")
            offset += _EXT_HEADER_LEN + ext_words * _CSRC_WORD
            if len(data) < offset:
                msg = "RTP packet too short for its extension data"
                raise ValueError(msg)
        payload = data[offset:]
        if byte0 & _PADDING_BIT:
            if not payload:
                msg = "RTP padding bit set but no payload to hold a pad count"
                raise ValueError(msg)
            pad = payload[-1]
            if not 0 < pad <= len(payload):
                msg = (
                    f"malformed RTP padding: count {pad} not in 1..{len(payload)} "
                    "(the count octet counts itself, RFC 3550 §5.1)"
                )
                raise ValueError(msg)
            payload = payload[:-pad]
        return cls(
            payload_type=byte1 & _MAX_PAYLOAD_TYPE,
            sequence_number=seq,
            timestamp=timestamp,
            ssrc=ssrc,
            payload=payload,
            marker=bool(byte1 & _MARKER_BIT),
        )


@dataclass(frozen=True, slots=True)
class Lost:
    """A jitter-buffer signal that one or more consecutive packets are lost.

    Attributes:
        sequence: The first (lowest) sequence number in the lost run.
        count: How many consecutive packets starting at ``sequence`` are lost.
            Defaults to ``1`` for backward-compatibility; callers that only
            handle single-packet loss can ignore this field.  When ``count > 1``
            the buffer has already advanced its anchor past the entire run, so
            only one :class:`Lost` event is emitted for the whole gap.
    """

    sequence: int
    count: int = 1

    def __post_init__(self) -> None:
        """Validate ``count`` is a whole packet count (>= 1).

        A zero or negative run is meaningless and silently breaks the consumer's
        per-slot concealment loop (``for _ in range(count)`` emits nothing for 0
        and raises for a negative), so it is rejected at construction. ``bool`` is
        an ``int`` subclass in Python so it is checked first; a non-int ``count``
        (e.g. a ``float`` from an untyped caller) is a type error — matching the
        runtime-and-type-check validation style used elsewhere in this module.
        """
        count_raw: object = self.count
        if isinstance(count_raw, bool):
            msg = "Lost.count must be a positive int, not bool"
            raise TypeError(msg)
        if not isinstance(count_raw, int):
            kind = type(count_raw).__name__
            msg = f"Lost.count must be a positive int, got {kind}"
            raise TypeError(msg)
        if self.count < 1:
            msg = f"Lost.count must be >= 1, got {self.count}"
            raise ValueError(msg)


type JitterOutput = RtpPacket | Lost


def _seq_before(a: int, b: int) -> bool:
    """True if ``a`` precedes ``b`` in 16-bit serial-number space (RFC 1982)."""
    return a != b and (b - a) % _SEQ_MOD < _SEQ_HALF


def _seq_next(seq: int) -> int:
    """The sequence number following ``seq`` (16-bit wraparound)."""
    return (seq + 1) % _SEQ_MOD


class JitterBuffer:
    """Reorders inbound RTP and signals loss for concealment.

    ``pop`` returns the next in-order packet when available, a :class:`Lost`
    marker once the current reorder tolerance (the *depth*) of later packets has
    piled up behind a gap (so the caller conceals and moves on), or ``None`` on
    underflow (wait for more).

    The depth is fixed at ``target_depth`` by default. With ``adapt=True`` it
    becomes *adaptive* (ADR-0056): the depth is the live reorder tolerance and
    moves between ``target_depth`` (floor) and ``max_depth`` (ceiling) in
    response to the observed stream — it GROWS when the buffer is shown to have
    declared loss too eagerly (a packet arrives after its slot was already
    emitted as :class:`Lost`, a *late drop*, or an in-window packet arrives
    reordered by a span ≥ the current depth) and SHRINKS one step toward the
    floor after a sustained clean run, so a jittery link stops underrunning and a
    calm link stops paying needless latency. Adaptation is driven ONLY by the
    push/pop sequence stream (no wall clock), so it is fully deterministic.

    The buffer also tracks the stream SSRC: the first packet learns the current
    SSRC, and a later packet with a different SSRC auto-resets sequence state
    before anchoring on the new source. This treats re-INVITE/source resync as a
    new stream instead of comparing it against stale sequence numbers.
    """

    def __init__(
        self,
        target_depth: int = 2,
        max_ahead: int = _DEFAULT_MAX_AHEAD,
        *,
        max_depth: int | None = None,
        adapt: bool = False,
        ssrc_hysteresis: int = 3,
    ) -> None:
        """Create the buffer.

        Args:
            target_depth: Declare loss once this many packets are buffered behind
                a gap. The count is TOTAL buffered occupancy, not the contiguous
                run sitting immediately behind the gap: a single far-future
                cluster (still within ``max_ahead``) is enough to reach the depth
                and declare the immediate gap :class:`Lost`, even when nothing
                contiguous sits right behind it. This total-occupancy rule is
                what guarantees forward progress past a permanent gap — a
                buffered cluster always drains rather than stalling the playout —
                at the cost of declaring a near gap lost eagerly when an isolated
                far cluster is present. Gating loss on the contiguous backlog
                instead is the larger jitter-buffer API redesign (a separate
                backlog item), not done here. When ``adapt`` is on this is the
                FLOOR (minimum) reorder tolerance the adaptive depth never drops
                below.
            max_ahead: Playout window — packets more than this many sequence
                numbers ahead of the next expected one are dropped, bounding
                storage even under a permanent gap or a source resync.
            max_depth: The CEILING for the adaptive depth (only meaningful with
                ``adapt=True``). ``None`` (the default) uses
                :data:`_DEFAULT_MAX_DEPTH` (10 packets ≈ 200 ms at 20 ms ptime).
                Must be ≥ ``target_depth``.
            adapt: Enable adaptive reorder tolerance. ``False`` (the default)
                keeps the depth fixed at ``target_depth`` — byte-for-byte the
                legacy behaviour, so existing call sites are unaffected.
            ssrc_hysteresis: How many CONSECUTIVE packets bearing the same
                foreign SSRC must arrive before the buffer auto-resets and
                adopts that SSRC as the new home. Must be a positive ``int``
                (not ``bool``). Default 3 (ADR-0082). Set to ``1`` to restore
                the pre-hysteresis behaviour (reset on the very first foreign
                packet).
        """
        if target_depth < 1:
            msg = f"target_depth must be >= 1, got {target_depth}"
            raise ValueError(msg)
        if max_ahead < 1:
            msg = f"max_ahead must be >= 1, got {max_ahead}"
            raise ValueError(msg)
        assert max_ahead < _SEQ_HALF, (  # noqa: S101
            f"max_ahead must be < _SEQ_HALF ({_SEQ_HALF}): got {max_ahead}. "
            "A window >= half the 16-bit sequence space breaks RFC 1982 "
            "serial-number comparison logic (defensive invariant that should "
            "never fire in production; configuration error, not runtime error)."
        )
        ceiling = _DEFAULT_MAX_DEPTH if max_depth is None else max_depth
        if adapt and ceiling < target_depth:
            msg = (
                f"max_depth must be >= target_depth (floor), "
                f"got max_depth={ceiling} < target_depth={target_depth}"
            )
            raise ValueError(msg)
        # Validate at runtime as well as at type-check time: bool is a subclass
        # of int in Python so it must be checked first; non-int objects (e.g.
        # float from an untyped caller) are also rejected so the contract is
        # enforced regardless of whether the caller uses mypy.
        _hysteresis_raw: object = ssrc_hysteresis
        if isinstance(_hysteresis_raw, bool):
            msg = "ssrc_hysteresis must be a positive int, not bool"
            raise TypeError(msg)
        if not isinstance(_hysteresis_raw, int):
            kind = type(_hysteresis_raw).__name__
            msg = f"ssrc_hysteresis must be a positive int, got {kind}"
            raise TypeError(msg)
        if ssrc_hysteresis < 1:
            msg = f"ssrc_hysteresis must be >= 1, got {ssrc_hysteresis}"
            raise ValueError(msg)
        self._adapt = adapt
        self._floor = target_depth
        self._ceiling = ceiling
        self._depth = target_depth
        self._max_ahead = max_ahead
        self._ssrc_hysteresis = ssrc_hysteresis
        self._packets: dict[int, RtpPacket] = {}
        self._next: int | None = None
        self._ssrc: int | None = None
        self._emitted = False
        # Highest sequence (RFC 1982 order) ever pushed — the reference for a
        # reorder-span measurement. ``None`` until the first push. Adaptive only.
        self._highest: int | None = None
        # Consecutive clean pops (real in-order packet, no loss/late-drop) since
        # the last grow or shrink. Drives the shrink-toward-floor decision.
        self._clean_run = 0
        # SSRC hysteresis tracking (ADR-0082). A foreign-SSRC packet starts or
        # continues a candidate run; the home SSRC or a DIFFERENT foreign SSRC
        # resets it. Reset fires only when _ssrc_candidate_count reaches
        # _ssrc_hysteresis.
        self._ssrc_candidate: int | None = None
        self._ssrc_candidate_count: int = 0

    def __len__(self) -> int:
        """Return the current buffered packet count."""
        return len(self._packets)

    @property
    def ssrc_hysteresis(self) -> int:
        """Consecutive foreign-SSRC packets required before an SSRC auto-reset fires."""
        return self._ssrc_hysteresis

    @property
    def current_depth(self) -> int:
        """The live reorder tolerance (in packets).

        Equals ``target_depth`` for a non-adaptive buffer; for an adaptive one
        it moves within ``[target_depth, max_depth]`` as the link is observed.
        """
        return self._depth

    def reset(self) -> None:
        """Clear buffered packets, sequencing state, SSRC, and adaptation history."""
        self._packets.clear()
        self._next = None
        self._ssrc = None
        self._emitted = False
        self._highest = None
        self._clean_run = 0
        self._depth = self._floor
        self._ssrc_candidate = None
        self._ssrc_candidate_count = 0

    def _grow(self) -> None:
        """Widen the reorder tolerance by one, bounded by the ceiling (adaptive)."""
        if self._depth < self._ceiling:
            self._depth += 1
        self._clean_run = 0

    def _check_ssrc(self, packet: RtpPacket) -> bool:
        """Apply SSRC hysteresis and return True when the packet may be enqueued.

        Updates the candidate-run state and fires :meth:`reset` (adopting the
        new SSRC) once ``ssrc_hysteresis`` consecutive foreign-SSRC packets have
        been observed.  Returns ``False`` when the foreign packet is silently
        dropped because the hysteresis window is not yet satisfied.
        """
        if self._ssrc is None:
            # First packet ever: learn the home SSRC.
            self._ssrc = packet.ssrc
            self._ssrc_candidate = None
            self._ssrc_candidate_count = 0
            return True
        if packet.ssrc == self._ssrc:
            # Home SSRC: clear any in-progress foreign candidate run.
            self._ssrc_candidate = None
            self._ssrc_candidate_count = 0
            return True
        # Foreign SSRC: accumulate the consecutive-run counter.
        if packet.ssrc == self._ssrc_candidate:
            self._ssrc_candidate_count += 1
        else:
            self._ssrc_candidate = packet.ssrc
            self._ssrc_candidate_count = 1
        if self._ssrc_candidate_count < self._ssrc_hysteresis:
            return False  # hysteresis window not yet satisfied — drop silently
        # N consecutive foreign packets confirmed: reset and adopt new SSRC.
        self.reset()
        self._ssrc = packet.ssrc
        return True

    def push(self, packet: RtpPacket) -> None:
        """Add a packet; duplicate, late, and too-far-ahead packets are dropped.

        The anchor (next expected sequence) is set tentatively at the first
        arrival and revised downward by any earlier sequence that arrives before
        the first :meth:`pop`, so a reordered opening packet (a lower sequence
        arriving after a higher one) is never skipped — the start of a call is
        the most reordering-prone. The playout window is measured against this
        tentative anchor throughout, keeping storage bounded. Once a packet has
        been emitted the anchor only advances, and late arrivals are dropped.

        When ``adapt`` is on, a *late drop* (a packet arriving after its slot was
        emitted as :class:`Lost`) and a *wide reorder* (an in-window packet
        arriving a span ≥ the current depth behind the highest pushed) each grow
        the reorder tolerance — both are evidence the buffer was too eager.
        """
        seq = packet.sequence_number
        if not self._check_ssrc(packet):
            return
        if self._next is not None:
            if _seq_before(seq, self._next):
                if self._emitted:
                    # Too late: this sequence has already been emitted. Under
                    # adaptation, an arrival within the window proves we declared
                    # loss too eagerly — grow so the next such reorder survives.
                    if self._adapt and (self._next - seq) % _SEQ_MOD <= self._max_ahead:
                        self._grow()
                    return
                if (self._next - seq) % _SEQ_MOD > self._max_ahead:
                    return  # too far behind to be a start reorder; bound the window
                self._next = seq  # pre-playout reorder within window: revise anchor
            elif (seq - self._next) % _SEQ_MOD > self._max_ahead:
                return  # outside the playout window: keep storage bounded
            elif (
                self._adapt
                and self._highest is not None
                and _seq_before(seq, self._highest)
                and (self._highest - seq) % _SEQ_MOD >= self._depth
            ):
                # A wide reorder: this in-window packet arrived a span >= the
                # current depth behind the highest we have seen, i.e. the link
                # reorders further than we currently tolerate. Grow toward it by one
                # step. Several packets of one wide burst may each step the depth up,
                # but only until depth exceeds the burst's span (the condition then
                # goes false), so the depth CONVERGES to ~the observed span and never
                # overshoots past it — and _grow() caps it at the ceiling regardless.
                self._grow()
        else:
            self._next = seq  # tentative anchor at the first arrival
        if self._highest is None or _seq_before(self._highest, seq):
            self._highest = seq
        self._packets.setdefault(seq, packet)  # first arrival wins; ignore duplicates

    def pop(self) -> JitterOutput | None:
        """Return the next packet, a :class:`Lost` marker, or ``None`` (underflow).

        Under adaptation a sustained run of clean in-order pops (``_SHRINK_AFTER``
        with no loss and no intervening late drop/wide reorder) shrinks the depth
        one step toward the floor, so a settled link sheds the latency it took on
        during a jittery patch.
        """
        if self._next is None:
            return None
        expected = self._next
        packet = self._packets.pop(expected, None)
        if packet is not None:
            self._emitted = True
            self._next = _seq_next(expected)
            if self._adapt:
                self._clean_run += 1
                if self._clean_run >= _SHRINK_AFTER and self._depth > self._floor:
                    self._depth -= 1
                    self._clean_run = 0
            return packet
        # Total buffered occupancy (NOT the contiguous backlog behind the gap)
        # gates loss; see ``target_depth`` for why — it guarantees the buffer
        # drains past a permanent gap instead of stalling.
        if len(self._packets) >= self._depth:
            self._emitted = True
            if self._adapt:
                self._clean_run = 0  # a declared loss breaks the clean run
            # Run-length coalescing: scan forward from ``expected`` to find the
            # first sequence that IS buffered.  Emit a single Lost(count=N)
            # instead of N separate Lost events — O(1) allocations + iterations
            # per gap regardless of gap size (amortized O(1) per packet).
            # The scan is bounded by _max_ahead so it terminates even for a
            # permanent gap. The gap is capped at EXACTLY _max_ahead: the strict
            # ``<`` stops the loop once ``scan`` reaches ``expected + _max_ahead``
            # (the window edge), so ``gap`` never exceeds _max_ahead and the anchor
            # cannot over-advance past a packet that later arrives at that edge.
            # The loop visits at most _max_ahead steps.
            gap = 1
            scan = _seq_next(expected)
            while (
                scan not in self._packets
                and (scan - expected) % _SEQ_MOD < self._max_ahead
            ):
                gap += 1
                scan = _seq_next(scan)
            # Advance the anchor past all gap slots in one step.
            self._next = (expected + gap) % _SEQ_MOD
            return Lost(expected, count=gap)
        return None

    def peek(self) -> JitterOutput | None:
        """Return the next :meth:`pop` result without consuming buffer state."""
        if self._next is None:
            return None
        expected = self._next
        packet = self._packets.get(expected)
        if packet is not None:
            return packet
        if len(self._packets) >= self._depth:
            return Lost(expected)
        return None

    def flush(self) -> list[RtpPacket]:
        """Drain and return all currently buffered packets in playout order."""
        if self._next is None:
            return []
        anchor = self._next
        ordered_sequences = sorted(
            self._packets,
            key=lambda seq: (seq - anchor) % _SEQ_MOD,
        )
        drained = [self._packets[seq] for seq in ordered_sequences]
        self._packets.clear()
        if drained:
            self._next = _seq_next(drained[-1].sequence_number)
            self._emitted = True
            if self._adapt:
                self._clean_run = 0
        return drained

    def peek_next(self) -> RtpPacket | None:
        """Return the buffered packet at the current anchor WITHOUT consuming it.

        Read-only: it never advances the anchor, declares loss, or mutates the
        buffer. Used by the media engine immediately after :meth:`pop` returns a
        :class:`Lost` to see whether the lost frame's *successor* (which carries
        its Opus in-band FEC copy) has already arrived, so it can recover the
        frame via FEC instead of plain concealment. Returns ``None`` when the
        successor is not (yet) buffered.
        """
        if self._next is None:
            return None
        return self._packets.get(self._next)
