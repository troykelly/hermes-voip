"""Self-contained pure-Python ITU-T G.722 codec — 64 kbit/s mode 1 (ADR-0022).

The telephony wire can negotiate G.722 (RFC 3551 payload type 9): a 16 kHz
wideband sub-band ADPCM codec at the same 64 kbit/s as G.711. Python's
``audioop``/``audioop-lts`` has no G.722, and the only PyPI option is a C
extension (no musl/macOS wheels, must be a base dependency); the engine must run
in the default no-extra environment. So this module vendors a small, fully-typed,
dependency-free port of the **public-domain** G.722 reference, validated bit-exact
against that reference (see ``tests/g722_kat_vectors.py``).

It implements only the **standard 64 kbit/s mode (8 bits/sample, unpacked,
non-test-mode)** — exactly RTP G.722. The encoder consumes PCM16-LE @ 16 kHz and
emits one octet per input sample-pair; the decoder is the inverse. State (the QMF
history + the per-band adaptive predictor/quantiser) is carried across calls, so
feeding a continuous stream frame-by-frame produces the same bytes/samples as one
pass — one ``G722Encoder``/``G722Decoder`` belongs to one direction of one call.

The RFC 3551 framing quirk: G.722's RTP **clock rate is 8000** even though the
audio is sampled at **16000** — so a 20 ms frame is 320 input samples -> 160
octets and the RTP timestamp advances by 160 (the 8 kHz clock), not 320. This
module only does sample<->octet transcoding; the engine
(:mod:`hermes_voip.media.engine`) owns the RTP timestamp/rate bookkeeping via its
codec descriptor.

Provenance / licence (public domain — no copyleft, no vendor lock-in)::

    The G.722 algorithm here is ported from the public-domain reference codec by
    Steve Underwood <steveu@coppice.org> ("I place my own contributions to this
    code in the public domain for the benefit of all mankind"), based on a single
    channel 64 kbit/s G.722 codec Copyright (c) CMU 1993 (Computer Science, Speech
    Group, Chengxiang Lu and Alex Hauptmann), whose notice states: "Use of this
    program, for any research or commercial purpose, is completely unrestricted.
    If you make use of or redistribute this material, we would appreciate
    acknowlegement of its origin." (As packaged permissively — Public-Domain +
    BSD-2 — by the sippy/libg722 project.)  The ITU-T STL reference and its
    conformance vectors, which are copyleft (modified GPL), are deliberately NOT
    used.
"""

from __future__ import annotations

import struct
from typing import Final

from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE

__all__ = [
    "G722_RTP_CLOCK_RATE",
    "G722_SAMPLE_RATE",
    "G722Decoder",
    "G722Encoder",
]

#: The TRUE audio sample rate of G.722 (16 kHz wideband).
G722_SAMPLE_RATE: Final[int] = 16_000

#: The RTP clock rate G.722 declares (RFC 3551 §4.5.2): 8 kHz, even though the
#: audio is 16 kHz. The value "was erroneously assigned in RFC 1890 and must
#: remain unchanged for backward compatibility." RTP timestamps advance at this
#: rate (160 per 20 ms frame), not at the 16 kHz audio sample rate.
G722_RTP_CLOCK_RATE: Final[int] = 8_000

# Rule 22 / ADR-0094: measured hot-path cost for ONE 20 ms / 16 kHz frame
# (320 PCM16 samples -> 160 G.722 octets on encode; inverse on decode), recorded
# in source so the CPU budget is explicit and reviewable. Measured 2026-06-28 on
# CPython 3.13.5 in the project devcontainer via repeated
# G722Encoder().encode(frame) / G722Decoder().decode(frame) calls over 2000-3000
# iterations on the stateful continuous-stream path. The budget gate in
# tests/test_media_g722_budget.py asserts these constants exist and stay below one
# 20 ms frame in aggregate; its 15 ms wall-clock ceiling is the secondary safety
# net for catastrophic regressions only.
_G722_ENCODE_MEASURED_US_PER_FRAME_16K: Final[float] = 3_400.0
_G722_DECODE_MEASURED_US_PER_FRAME_16K: Final[float] = 3_300.0

_INT16_MAX: Final[int] = 32_767
_INT16_MIN: Final[int] = -32_768

# --- Quantiser / predictor tables (verbatim from the public-domain reference) ---

# Low-band 6-bit log-PCM quantiser decision levels (encoder QUANTL).
_Q6: Final[tuple[int, ...]] = (
    0, 35, 72, 110, 150, 190, 233, 276, 323, 370, 422, 473, 530, 587, 650, 714,
    786, 858, 940, 1023, 1121, 1219, 1339, 1458, 1612, 1765, 1980, 2195, 2557,
    2919, 0, 0,
)  # fmt: skip
# Low-band index maps for negative / positive error (encoder QUANTL output).
_ILN: Final[tuple[int, ...]] = (
    0, 63, 62, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19,
    18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 0,
)  # fmt: skip
_ILP: Final[tuple[int, ...]] = (
    0, 61, 60, 59, 58, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47,
    46, 45, 44, 43, 42, 41, 40, 39, 38, 37, 36, 35, 34, 33, 32, 0,
)  # fmt: skip
# Low-band log scale-factor multipliers (LOGSCL) and 4-level index map.
_WL: Final[tuple[int, ...]] = (-60, -30, 58, 172, 334, 538, 1198, 3042)
_RL42: Final[tuple[int, ...]] = (0, 7, 6, 5, 4, 3, 2, 1, 7, 6, 5, 4, 3, 2, 1, 0)
# Shared scale-factor table (SCALEL/SCALEH).
_ILB: Final[tuple[int, ...]] = (
    2048, 2093, 2139, 2186, 2233, 2282, 2332, 2383, 2435, 2489, 2543, 2599,
    2656, 2714, 2774, 2834, 2896, 2960, 3025, 3091, 3158, 3228, 3298, 3371,
    3444, 3520, 3597, 3676, 3756, 3838, 3922, 4008,
)  # fmt: skip
# Low-band inverse-quantiser tables (INVQAL: qm4; decode INVQBL: qm5/qm6).
_QM4: Final[tuple[int, ...]] = (
    0, -20456, -12896, -8968, -6288, -4240, -2584, -1200,
    20456, 12896, 8968, 6288, 4240, 2584, 1200, 0,
)  # fmt: skip
_QM5: Final[tuple[int, ...]] = (
    -280, -280, -23352, -17560, -14120, -11664, -9752, -8184,
    -6864, -5712, -4696, -3784, -2960, -2208, -1520, -880,
    23352, 17560, 14120, 11664, 9752, 8184, 6864, 5712,
    4696, 3784, 2960, 2208, 1520, 880, 280, -280,
)  # fmt: skip
_QM6: Final[tuple[int, ...]] = (
    -136, -136, -136, -136, -24808, -21904, -19008, -16704,
    -14984, -13512, -12280, -11192, -10232, -9360, -8576, -7856,
    -7192, -6576, -6000, -5456, -4944, -4464, -4008, -3576,
    -3168, -2776, -2400, -2032, -1688, -1360, -1040, -728,
    24808, 21904, 19008, 16704, 14984, 13512, 12280, 11192,
    10232, 9360, 8576, 7856, 7192, 6576, 6000, 5456,
    4944, 4464, 4008, 3576, 3168, 2776, 2400, 2032,
    1688, 1360, 1040, 728, 432, 136, -432, -136,
)  # fmt: skip
# High-band 2-bit quantiser maps (QUANTH) and inverse-quantiser (INVQAH: qm2).
_IHN: Final[tuple[int, ...]] = (0, 1, 0)
_IHP: Final[tuple[int, ...]] = (0, 3, 2)
_QM2: Final[tuple[int, ...]] = (-7408, -1616, 7408, 1616)
# High-band log scale-factor multipliers (LOGSCH) and index map.
_WH: Final[tuple[int, ...]] = (0, -214, 798)
_RH2: Final[tuple[int, ...]] = (2, 1, 2, 1)
# 12-tap QMF prototype filter (24-tap symmetric), used both directions.
_QMF: Final[tuple[int, ...]] = (
    3, -11, 12, 32, -210, 951, 3876, -805, 362, -156, 53, -11,
)  # fmt: skip


def _saturate(amp: int) -> int:
    """Clamp ``amp`` to the signed 16-bit range (the reference ``saturate``)."""
    if _INT16_MIN <= amp <= _INT16_MAX:
        return amp
    return _INT16_MAX if amp > _INT16_MAX else _INT16_MIN


class _Band:
    """Per-sub-band adaptive ADPCM predictor + quantiser state.

    Mirrors the reference's ``g722_band`` struct; one low band and one high band
    live in each encoder/decoder.
    """

    __slots__ = (
        "a",
        "ap",
        "b",
        "bp",
        "d",
        "det",
        "nb",
        "p",
        "r",
        "s",
        "sg",
        "sp",
        "sz",
    )

    def __init__(self, det: int) -> None:
        """Initialise; ``det`` is the reference's per-band start (low 32, high 8)."""
        self.s: int = 0
        self.sp: int = 0
        self.sz: int = 0
        self.r: list[int] = [0, 0, 0]
        self.a: list[int] = [0, 0, 0]
        self.ap: list[int] = [0, 0, 0]
        self.p: list[int] = [0, 0, 0]
        self.d: list[int] = [0] * 7
        self.b: list[int] = [0] * 7
        self.bp: list[int] = [0] * 7
        self.sg: list[int] = [0] * 7
        self.nb: int = 0
        self.det: int = det


def _block4(band: _Band, d: int) -> None:
    """Update one sub-band's adaptive predictor with reconstructed difference ``d``.

    A faithful port of the reference ``block4`` (RECONS/PARREC/UPPOL2/UPPOL1/
    UPZERO/DELAYA/FILTEP/FILTEZ/PREDIC): adapts the second-order pole predictor
    (``a``) and sixth-order zero predictor (``b``), shifts the history, and
    recomputes the band's pole/zero estimates (``sp``/``sz``) and predicted
    signal (``s``). Mutates ``band`` in place.
    """
    # RECONS / PARREC.
    band.d[0] = d
    band.r[0] = _saturate(band.s + d)
    band.p[0] = _saturate(band.sz + d)

    # UPPOL2 (second-order pole predictor ap[2]).
    for i in range(3):
        band.sg[i] = band.p[i] >> 15
    wd1 = _saturate(band.a[1] << 2)
    wd2 = -wd1 if band.sg[0] == band.sg[1] else wd1
    # Kept as an explicit clamp (not min()) to mirror the reference line-for-line
    # for verifiability against the public-domain source.
    if wd2 > 32767:  # noqa: PLR2004, PLR1730 — reference INT16_MAX clamp, faithful port
        wd2 = 32767
    wd3 = (wd2 >> 7) + (128 if band.sg[0] == band.sg[2] else -128)
    wd3 += (band.a[2] * 32512) >> 15
    if wd3 > 12288:  # noqa: PLR2004 — reference ap[2] clamp
        wd3 = 12288
    elif wd3 < -12288:  # noqa: PLR2004 — reference ap[2] clamp
        wd3 = -12288
    band.ap[2] = wd3

    # UPPOL1 (first-order pole predictor ap[1]).
    band.sg[0] = band.p[0] >> 15
    band.sg[1] = band.p[1] >> 15
    wd1 = 192 if band.sg[0] == band.sg[1] else -192
    wd2 = (band.a[1] * 32640) >> 15
    band.ap[1] = _saturate(wd1 + wd2)
    wd3 = _saturate(15360 - band.ap[2])
    if band.ap[1] > wd3:
        band.ap[1] = wd3
    elif band.ap[1] < -wd3:
        band.ap[1] = -wd3

    # UPZERO (sixth-order zero predictor bp[1..6]).
    wd1 = 0 if d == 0 else 128
    band.sg[0] = d >> 15
    for i in range(1, 7):
        band.sg[i] = band.d[i] >> 15
        wd2 = wd1 if band.sg[i] == band.sg[0] else -wd1
        wd3 = (band.b[i] * 32640) >> 15
        band.bp[i] = _saturate(wd2 + wd3)

    # DELAYA (history shifts).
    for i in range(6, 0, -1):
        band.d[i] = band.d[i - 1]
        band.b[i] = band.bp[i]
    for i in range(2, 0, -1):
        band.r[i] = band.r[i - 1]
        band.p[i] = band.p[i - 1]
        band.a[i] = band.ap[i]

    # FILTEP (pole prediction sp).
    wd1 = _saturate(band.r[1] + band.r[1])
    wd1 = (band.a[1] * wd1) >> 15
    wd2 = _saturate(band.r[2] + band.r[2])
    wd2 = (band.a[2] * wd2) >> 15
    band.sp = _saturate(wd1 + wd2)

    # FILTEZ (zero prediction sz).
    band.sz = 0
    for i in range(6, 0, -1):
        wd1 = _saturate(band.d[i] + band.d[i])
        band.sz += (band.b[i] * wd1) >> 15
    band.sz = _saturate(band.sz)

    # PREDIC.
    band.s = _saturate(band.sp + band.sz)


def _validate_pcm16_even(pcm16: bytes) -> None:
    """Raise if ``pcm16`` is not a whole, EVEN number of 16-bit samples.

    G.722's QMF consumes input two samples at a time, so the encoder needs a whole
    number of sample-pairs. A non-sample byte length or an odd sample count is a
    programming error (the caller must frame correctly), not silently truncated.
    """
    if len(pcm16) % PCM16_BYTES_PER_SAMPLE != 0:
        msg = f"PCM16 buffer must be whole 16-bit samples, got {len(pcm16)} bytes"
        raise ValueError(msg)
    n_samples = len(pcm16) // PCM16_BYTES_PER_SAMPLE
    if n_samples % 2 != 0:
        msg = (
            f"G.722 encodes input two samples at a time (the QMF split); the PCM16 "
            f"sample count must be even, got {n_samples}"
        )
        raise ValueError(msg)


class G722Encoder:
    """Stateful G.722 encoder for one continuous stream (64 kbit/s mode 1).

    :meth:`encode` consumes PCM16-LE mono @ 16 kHz and returns one G.722 octet per
    input sample-pair. The QMF history and per-band adaptive state carry across
    calls, so feeding a stream frame-by-frame yields the same bytes as one pass —
    construct a fresh encoder per call (or per stream direction).
    """

    def __init__(self) -> None:
        """Create an encoder in the reference's initial state (low/high det 32/8)."""
        self._lo = _Band(32)
        self._hi = _Band(8)
        # QMF analysis history (24 ints).
        self._x: list[int] = [0] * 24

    def encode(self, pcm16: bytes) -> bytes:  # noqa: PLR0915 — faithful inline port of the reference encode loop
        """Encode PCM16-LE @ 16 kHz to G.722 octets (one per input sample-pair).

        Args:
            pcm16: PCM16-LE mono samples at 16 kHz. The sample count must be even
                (whole sample-pairs); :func:`_validate_pcm16_even` enforces it.

        Returns:
            The G.722 bytes — ``len(pcm16) // 2 // 2`` octets.

        Raises:
            ValueError: If ``pcm16`` is not a whole, even number of 16-bit samples.
        """
        _validate_pcm16_even(pcm16)
        samples = struct.unpack(f"<{len(pcm16) // 2}h", pcm16)
        lo = self._lo
        hi = self._hi
        x = self._x
        out = bytearray()
        for j in range(0, len(samples), 2):
            # QMF analysis: split the 16 kHz pair into low/high 8 kHz sub-bands.
            for i in range(22):
                x[i] = x[i + 2]
            x[22] = samples[j]
            x[23] = samples[j + 1]
            sumeven = 0
            sumodd = 0
            for i in range(12):
                sumodd += x[2 * i] * _QMF[i]
                sumeven += x[2 * i + 1] * _QMF[11 - i]
            xlow = (sumeven + sumodd) >> 14
            xhigh = (sumeven - sumodd) >> 14

            # --- Low band: 6-bit ADPCM ---
            el = _saturate(xlow - lo.s)
            wd = el if el >= 0 else -(el + 1)
            i = 1
            while i < 30:  # noqa: PLR2004 — reference loop bound (29 levels)
                if wd < ((_Q6[i] * lo.det) >> 12):
                    break
                i += 1
            ilow = _ILN[i] if el < 0 else _ILP[i]
            ril = ilow >> 2
            dlow = (lo.det * _QM4[ril]) >> 15
            il4 = _RL42[ril]
            lo.nb = ((lo.nb * 127) >> 7) + _WL[il4]
            if lo.nb < 0:
                lo.nb = 0
            elif lo.nb > 18432:  # noqa: PLR2004 — reference low-band nb cap
                lo.nb = 18432
            wd1 = (lo.nb >> 6) & 31
            wd2 = 8 - (lo.nb >> 11)
            wd3 = (_ILB[wd1] << -wd2) if wd2 < 0 else (_ILB[wd1] >> wd2)
            lo.det = wd3 << 2
            _block4(lo, dlow)

            # --- High band: 2-bit ADPCM ---
            eh = _saturate(xhigh - hi.s)
            wd = eh if eh >= 0 else -(eh + 1)
            mih = 2 if wd >= ((564 * hi.det) >> 12) else 1
            ihigh = _IHN[mih] if eh < 0 else _IHP[mih]
            dhigh = (hi.det * _QM2[ihigh]) >> 15
            ih2 = _RH2[ihigh]
            hi.nb = ((hi.nb * 127) >> 7) + _WH[ih2]
            if hi.nb < 0:
                hi.nb = 0
            elif hi.nb > 22528:  # noqa: PLR2004 — reference high-band nb cap
                hi.nb = 22528
            wd1 = (hi.nb >> 6) & 31
            wd2 = 10 - (hi.nb >> 11)
            wd3 = (_ILB[wd1] << -wd2) if wd2 < 0 else (_ILB[wd1] >> wd2)
            hi.det = wd3 << 2
            _block4(hi, dhigh)

            # Pack: high band -> top 2 bits, low band -> low 6 bits (mode 1).
            out.append(((ihigh << 6) | ilow) & 0xFF)
        return bytes(out)


class G722Decoder:
    """Stateful G.722 decoder for one continuous stream (64 kbit/s mode 1).

    :meth:`decode` consumes G.722 octets and returns PCM16-LE mono @ 16 kHz — two
    samples per octet (via the QMF synthesis filter). State carries across calls;
    construct a fresh decoder per call/stream direction.
    """

    def __init__(self) -> None:
        """Create a decoder in the reference's initial state (low/high det 32/8)."""
        self._lo = _Band(32)
        self._hi = _Band(8)
        # QMF synthesis history (24 ints).
        self._x: list[int] = [0] * 24

    def decode(self, g722: bytes) -> bytes:  # noqa: PLR0915 — faithful inline port of the reference decode loop
        """Decode G.722 octets to PCM16-LE @ 16 kHz (two samples per octet).

        Args:
            g722: The G.722 bytes (one octet per sample-pair, 64 kbit/s mode 1).

        Returns:
            PCM16-LE mono samples at 16 kHz — ``len(g722) * 2`` samples.
        """
        lo = self._lo
        hi = self._hi
        x = self._x
        out: list[int] = []
        for code in g722:
            # Split the octet: low 6 bits -> low band, top 2 bits -> high band.
            wd1 = code & 0x3F
            ihigh = (code >> 6) & 0x03
            wd2 = _QM6[wd1]
            wd1 >>= 2

            # --- Low band reconstruct ---
            wd2 = (lo.det * wd2) >> 15
            rlow = lo.s + wd2
            if rlow > 16383:  # noqa: PLR2004 — reference RECONS limit
                rlow = 16383
            elif rlow < -16384:  # noqa: PLR2004 — reference RECONS limit
                rlow = -16384
            dlowt = (lo.det * _QM4[wd1]) >> 15
            wd2 = _RL42[wd1]
            wd1 = (lo.nb * 127) >> 7
            wd1 += _WL[wd2]
            if wd1 < 0:
                wd1 = 0
            elif wd1 > 18432:  # noqa: PLR2004 — reference low-band nb cap
                wd1 = 18432
            lo.nb = wd1
            wd1 = (lo.nb >> 6) & 31
            wd2 = 8 - (lo.nb >> 11)
            wd3 = (_ILB[wd1] << -wd2) if wd2 < 0 else (_ILB[wd1] >> wd2)
            lo.det = wd3 << 2
            _block4(lo, dlowt)

            # --- High band reconstruct ---
            dhigh = (hi.det * _QM2[ihigh]) >> 15
            rhigh = dhigh + hi.s
            if rhigh > 16383:  # noqa: PLR2004 — reference RECONS limit
                rhigh = 16383
            elif rhigh < -16384:  # noqa: PLR2004 — reference RECONS limit
                rhigh = -16384
            wd2 = _RH2[ihigh]
            wd1 = (hi.nb * 127) >> 7
            wd1 += _WH[wd2]
            if wd1 < 0:
                wd1 = 0
            elif wd1 > 22528:  # noqa: PLR2004 — reference high-band nb cap
                wd1 = 22528
            hi.nb = wd1
            wd1 = (hi.nb >> 6) & 31
            wd2 = 10 - (hi.nb >> 11)
            wd3 = (_ILB[wd1] << -wd2) if wd2 < 0 else (_ILB[wd1] >> wd2)
            hi.det = wd3 << 2
            _block4(hi, dhigh)

            # QMF synthesis: recombine the two sub-bands into a 16 kHz pair.
            for i in range(22):
                x[i] = x[i + 2]
            x[22] = rlow + rhigh
            x[23] = rlow - rhigh
            xout1 = 0
            xout2 = 0
            for i in range(12):
                xout2 += x[2 * i] * _QMF[i]
                xout1 += x[2 * i + 1] * _QMF[11 - i]
            out.append(_saturate(xout1 >> 11))
            out.append(_saturate(xout2 >> 11))
        return struct.pack(f"<{len(out)}h", *out)
