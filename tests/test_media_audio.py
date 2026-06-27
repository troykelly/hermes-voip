"""Tests for hermes_voip.media.audio — G.711 codec + resampling (ADR-0004/0005).

The media layer owns the wire codec (G.711 mu-law/a-law, 1 byte/sample) and
8<->16 kHz resampling so providers only ever see PCM16 at a declared rate. These
tests pin the structural contract (byte ratios, rate math), the near-lossless
round-trip (G.711 is ~12-bit accurate), streaming-state continuity, and the
PcmFrame integration.
"""

import struct
import unittest.mock

import audioop
import pytest

from hermes_voip.media.audio import (
    G711_SAMPLE_RATE,
    Resampler,
    alaw_to_frame,
    decode_alaw,
    decode_ulaw,
    encode_alaw,
    encode_ulaw,
    frame_to_alaw,
    frame_to_ulaw,
    linear_fade_out,
    resample_frame,
    ulaw_to_frame,
)
from hermes_voip.providers.audio import PcmFrame


def _pcm16(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def _samples(pcm: bytes) -> tuple[int, ...]:
    return struct.unpack(f"<{len(pcm) // 2}h", pcm)


def test_ulaw_is_one_byte_per_sample() -> None:
    pcm = _pcm16(0, 1000, -1000, 16000, -16000)
    encoded = encode_ulaw(pcm)
    assert len(encoded) == 5  # one G.711 byte per 16-bit sample
    assert len(decode_ulaw(encoded)) == len(pcm)  # back to 2 bytes/sample


def test_alaw_is_one_byte_per_sample() -> None:
    pcm = _pcm16(0, 1000, -1000, 16000, -16000)
    assert len(encode_alaw(pcm)) == 5
    assert len(decode_alaw(encode_alaw(pcm))) == len(pcm)


def test_ulaw_round_trip_is_near_lossless_for_midrange() -> None:
    pcm = _pcm16(0, 2000, -2000, 8000, -8000, 100, -100)
    back = _samples(decode_ulaw(encode_ulaw(pcm)))
    for original, restored in zip(_samples(pcm), back, strict=True):
        # G.711 mu-law is ~12-bit accurate; quantisation error grows with level
        assert abs(original - restored) <= max(64, abs(original) // 16)


def test_resampler_8k_to_16k_roughly_doubles_samples() -> None:
    pcm = _pcm16(*([1000, -1000] * 80))  # 160 samples @ 8 kHz = 20 ms
    out = Resampler(8000, 16000).resample(pcm)
    out_samples = len(out) // 2
    # audioop.ratecv 8k->16k of 160 samples yields 319; allow ±4 for edge rounding
    assert 315 <= out_samples <= 323, (
        f"8k->16k: expected ~319 output samples, got {out_samples}"
    )


def test_resampler_16k_to_8k_roughly_halves_samples() -> None:
    pcm = _pcm16(*([500] * 320))  # 320 samples @ 16 kHz = 20 ms
    out = Resampler(16000, 8000).resample(pcm)
    out_samples = len(out) // 2
    # audioop.ratecv 16k->8k of 320 samples yields 160; allow ±4 for edge rounding
    assert 156 <= out_samples <= 164, (
        f"16k->8k: expected ~160 output samples, got {out_samples}"
    )


# ---------------------------------------------------------------------------
# linear_fade_out — click-free ramp on the final frames of a barge-in cut
# ---------------------------------------------------------------------------


def test_linear_fade_out_ramps_last_samples_to_zero() -> None:
    """The final ``fade_samples`` ramp linearly from full gain down to ~0.

    A constant full-scale signal is the worst case for a hard cut (max click). The
    fade must leave the head untouched and bring the tail monotonically to near
    silence, with the very last sample at (or essentially at) zero.
    """
    const = 10_000
    pcm = _pcm16(*([const] * 100))
    faded = _samples(linear_fade_out(pcm, fade_samples=40))

    # The 60 samples BEFORE the fade window are untouched (full amplitude).
    assert all(s == const for s in faded[:60])
    # The fade window ramps DOWN monotonically (each sample <= the previous).
    tail = faded[60:]
    assert all(tail[i] <= tail[i - 1] for i in range(1, len(tail)))
    # It starts near full and ends at (essentially) zero.
    assert tail[0] >= const - const // 20  # ~first faded sample still near full
    assert tail[-1] == 0  # last sample is silence — no residual step to click


def test_linear_fade_out_is_symmetric_for_negative_signal() -> None:
    """A full-scale NEGATIVE constant ramps up toward zero (magnitude shrinks)."""
    const = -12_000
    pcm = _pcm16(*([const] * 60))
    faded = _samples(linear_fade_out(pcm, fade_samples=30))
    tail = faded[30:]
    # Magnitude shrinks monotonically toward zero (samples rise toward 0).
    assert all(abs(tail[i]) <= abs(tail[i - 1]) for i in range(1, len(tail)))
    assert tail[-1] == 0


def test_linear_fade_out_clamps_fade_to_buffer_length() -> None:
    """A fade longer than the buffer fades the WHOLE buffer (no overrun/error)."""
    pcm = _pcm16(*([8000] * 10))
    faded = _samples(linear_fade_out(pcm, fade_samples=100))
    assert len(faded) == 10
    assert all(faded[i] <= faded[i - 1] for i in range(1, len(faded)))
    assert faded[-1] == 0


def test_linear_fade_out_zero_fade_returns_input_unchanged() -> None:
    """fade_samples=0 is a no-op (returns the bytes unchanged)."""
    pcm = _pcm16(1, 2, 3, 4)
    assert linear_fade_out(pcm, fade_samples=0) == pcm


def test_linear_fade_out_preserves_byte_length() -> None:
    """The faded buffer has the same number of PCM16 samples as the input."""
    pcm = _pcm16(*([5000] * 50))
    assert len(linear_fade_out(pcm, fade_samples=20)) == len(pcm)


@pytest.mark.parametrize(
    ("from_rate", "to_rate"),
    [
        (8000, 16000),
        (16000, 8000),
        # Non-integer-ratio TTS paths (ADR-0004/0005): boundary clicks appear when
        # sub-sample phase is discarded between frames; the stateful Resampler must
        # carry the phase forward so streaming == single-pass.
        (24000, 8000),  # integer-ratio (3:1) 24kHz TTS -> 8kHz wire
        (16000, 24000),  # non-integer-ratio (2:3) 16kHz -> 24kHz TTS output
        (24000, 16000),  # non-integer-ratio (3:2) 24kHz -> 16kHz ASR input
    ],
)
def test_resampler_state_continuity_matches_single_pass(
    from_rate: int, to_rate: int
) -> None:
    # Streaming repeated 20ms chunks must produce identical output to one-shot
    # conversion — proves the Resampler carries sub-sample phase across frame
    # boundaries, eliminating click artefacts at every supported rate pair.
    chunk = _pcm16(*range(-160, 160))  # 320 samples
    streamed = Resampler(from_rate, to_rate)
    streamed_out = b"".join(streamed.resample(chunk) for _ in range(4))
    single = Resampler(from_rate, to_rate).resample(chunk * 4)
    assert streamed_out == single


def test_resampler_rejects_odd_length_pcm() -> None:
    with pytest.raises(ValueError, match="whole 16-bit samples"):
        Resampler(8000, 16000).resample(b"\x00\x01\x02")  # 3 bytes


def test_encoders_reject_odd_length_pcm() -> None:
    with pytest.raises(ValueError, match="whole 16-bit samples"):
        encode_ulaw(b"\x00")
    with pytest.raises(ValueError, match="whole 16-bit samples"):
        encode_alaw(b"\x00\x01\x02")


def test_resampler_reset_clears_state() -> None:
    r = Resampler(8000, 16000)
    first = r.resample(_pcm16(*range(160)))
    r.reset()
    after_reset = r.resample(_pcm16(*range(160)))
    assert (
        after_reset == first
    )  # identical input from a clean state => identical output


def test_frame_helpers_round_trip_through_ulaw() -> None:
    # G.711 is intrinsically 8 kHz: ulaw_to_frame always stamps the wire rate.
    pcm = _pcm16(0, 4000, -4000, 1234)
    ulaw = encode_ulaw(pcm)
    frame = ulaw_to_frame(ulaw, monotonic_ts_ns=42)
    assert isinstance(frame, PcmFrame)
    assert frame.sample_rate == G711_SAMPLE_RATE == 8000
    assert frame.monotonic_ts_ns == 42
    assert frame.sample_count == 4
    assert frame_to_ulaw(frame) == ulaw  # frame -> wire is the inverse of wire -> frame


def test_frame_to_ulaw_rejects_non_8k_frame() -> None:
    # encoding a 16 kHz frame to G.711 would silently halve its duration
    frame = PcmFrame(samples=_pcm16(0, 1, 2), sample_rate=16000, monotonic_ts_ns=0)
    with pytest.raises(ValueError, match="8000 Hz"):
        frame_to_ulaw(frame)


def test_resampler_rejects_equal_rates() -> None:
    with pytest.raises(ValueError, match="differ"):
        Resampler(8000, 8000)


@pytest.mark.parametrize("bad_rate", [0, -8000])
def test_resampler_rejects_non_positive_from_rate(bad_rate: int) -> None:
    # A config-derived rate of 0/negative must fail fast at construction with a
    # ValueError, not lie dormant until ratecv raises audioop.error mid-call.
    with pytest.raises(ValueError, match="positive"):
        Resampler(bad_rate, 16000)


@pytest.mark.parametrize("bad_rate", [0, -16000])
def test_resampler_rejects_non_positive_to_rate(bad_rate: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        Resampler(8000, bad_rate)


def test_alaw_is_one_byte_per_sample_via_frame_bridge() -> None:
    pcm = _pcm16(0, 4000, -4000, 1234)
    alaw = encode_alaw(pcm)
    frame = alaw_to_frame(alaw, monotonic_ts_ns=7)
    assert isinstance(frame, PcmFrame)
    assert frame.sample_rate == G711_SAMPLE_RATE == 8000
    assert frame.monotonic_ts_ns == 7
    assert frame.sample_count == 4
    # frame -> wire is the exact inverse of wire -> frame
    assert frame_to_alaw(frame) == alaw


def test_frame_to_alaw_rejects_non_8k_frame() -> None:
    frame = PcmFrame(samples=_pcm16(0, 1, 2), sample_rate=16000, monotonic_ts_ns=0)
    with pytest.raises(ValueError, match="8000 Hz"):
        frame_to_alaw(frame)


def test_alaw_frame_bridge_is_distinct_from_ulaw() -> None:
    # PCMA and PCMU are different codecs: the a-law wire bytes for a non-trivial
    # frame must differ from the mu-law bytes (guards a copy-paste mu/a swap).
    frame = PcmFrame(
        samples=_pcm16(1000, -1000, 8000), sample_rate=8000, monotonic_ts_ns=0
    )
    assert frame_to_alaw(frame) != frame_to_ulaw(frame)


# ---------------------------------------------------------------------------
# Resampler — integer-type guard (coherent ValueError contract)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("from_rate", "to_rate"),
    [
        (1.5, 16000),  # float from_rate
        (16000, 2.5),  # float to_rate
        # bool: isinstance(True, int) is True, but bool is not a valid sample rate
        (True, 16000),
        (16000, False),
    ],
)
def test_resampler_rejects_non_integer_and_bool_rates(
    from_rate: object, to_rate: object
) -> None:
    """Resampler.__init__ raises ValueError for non-int or bool rates at construction.

    A float rate passes the positivity check (1.5 > 0) but audioop.ratecv later
    raises TypeError deep inside the C layer — NOT the ValueError the module
    contract advertises. A bool rate (True/False) silently runs as 1/0, violating
    the typed contract (rule 39). Both are caught at construction with ValueError.
    """
    with pytest.raises(ValueError, match="integer"):
        Resampler(from_rate, to_rate)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# decode_ulaw / decode_alaw — empty-input behaviour (no validation on decode)
# ---------------------------------------------------------------------------


def test_decode_ulaw_empty_input_returns_empty() -> None:
    """decode_ulaw(b"") returns b"" — empty payload produces empty PCM frame.

    Deliberate design: unlike encode_ulaw/encode_alaw (which validate alignment),
    decode_ulaw and decode_alaw do NOT validate input length. An empty G.711
    payload from the wire (e.g. comfort noise, keep-alive) silently decodes to
    an empty PCM buffer, leaving jitter-buffer or silence-concealment logic in
    the transport/call-loop layer to handle the zero-duration frame. Validation
    at decode time would require the decoder to know the framing semantics (ptime,
    packet expectations) — those live higher up. This test pins the no-validation
    behaviour so a future "helpful" validation cannot silently break the RX path.
    """
    assert decode_ulaw(b"") == b""


def test_decode_alaw_empty_input_returns_empty() -> None:
    """decode_alaw(b"") returns b"" — same deliberate no-validation-on-decode policy.

    See test_decode_ulaw_empty_input_returns_empty for the rationale: length
    validation belongs in the transport/jitter layer, not the codec layer.
    """
    assert decode_alaw(b"") == b""


def test_decode_ulaw_single_byte_returns_two_bytes() -> None:
    """A single mu-law byte decodes to exactly 2 PCM16 bytes (one sample)."""
    result = decode_ulaw(b"\xff")
    assert len(result) == 2  # one sample = 2 bytes of PCM16-LE


def test_decode_alaw_single_byte_returns_two_bytes() -> None:
    """A single a-law byte decodes to exactly 2 PCM16 bytes (one sample)."""
    result = decode_alaw(b"\xff")
    assert len(result) == 2


# ---------------------------------------------------------------------------
# a-law round-trip near-lossless value assertion
# ---------------------------------------------------------------------------


def test_alaw_round_trip_is_near_lossless_for_midrange() -> None:
    """G.711 a-law encode + decode preserves sample values within quantisation error.

    G.711 a-law is ~12-bit accurate (same as mu-law); this test pins the actual
    reconstructed values so a codec swap (lin2ulaw <-> lin2alaw) or sign-flip
    cannot survive mutation. The tolerance mirrors the mu-law test: each restored
    sample must be within max(64, |original| / 16) of the original.
    """
    pcm = _pcm16(0, 2000, -2000, 8000, -8000, 100, -100)
    back = _samples(decode_alaw(encode_alaw(pcm)))
    for original, restored in zip(_samples(pcm), back, strict=True):
        tol = max(64, abs(original) // 16)
        assert abs(original - restored) <= tol, (
            f"a-law round-trip: {original} -> {restored}, "
            f"diff={abs(original - restored)} > tol={tol}"
        )


# ---------------------------------------------------------------------------
# encode_ulaw vs encode_alaw discrimination
# ---------------------------------------------------------------------------


def test_encode_ulaw_differs_from_encode_alaw() -> None:
    """mu-law and a-law produce distinct wire bytes for a non-trivial signal.

    Guards a copy-paste or argument-order swap between lin2ulaw and lin2alaw
    at the encode path. The tests for one-byte-per-sample only checked lengths;
    this test checks the actual encoded byte values differ, catching a codec
    substitution that preserves the length contract but swaps the algorithm.
    """
    pcm = _pcm16(1000, -1000, 8000, -8000, 16000)
    assert encode_ulaw(pcm) != encode_alaw(pcm)


# ---------------------------------------------------------------------------
# Resampler — 24kHz sample-count sanity (TTS paths per ADR-0004/0005)
# ---------------------------------------------------------------------------


def test_resampler_24k_to_8k_sample_count() -> None:
    """24kHz -> 8kHz (3:1 integer ratio) produces ~1/3 the input samples.

    The TTS-to-wire path resamples 24kHz synthesis output to the 8kHz G.711
    wire rate. A 480-sample (20ms @ 24kHz) input must yield ~160 samples
    (20ms @ 8kHz). audioop.ratecv yields exactly 160; allow ±4 for edge.
    """
    pcm = _pcm16(*([1000, -1000] * 240))  # 480 samples @ 24 kHz = 20 ms
    out = Resampler(24000, 8000).resample(pcm)
    out_samples = len(out) // 2
    assert 156 <= out_samples <= 164, (
        f"24k->8k: expected ~160 output samples (1/3 of 480), got {out_samples}"
    )


# ---------------------------------------------------------------------------
# resample_frame — PcmFrame-level sample-rate converter helper
# ---------------------------------------------------------------------------


def test_resample_frame_8k_to_16k_returns_pcmframe() -> None:
    """resample_frame returns a PcmFrame with sample_rate set to the target rate."""
    pcm = _pcm16(*([1000, -1000] * 80))  # 160 samples @ 8 kHz = 20 ms
    frame = PcmFrame(samples=pcm, sample_rate=8000, monotonic_ts_ns=0)
    out = resample_frame(frame, target_rate=16000)
    assert isinstance(out, PcmFrame)
    assert out.sample_rate == 16000


def test_resample_frame_8k_to_16k_preserves_duration() -> None:
    """8kHz -> 16kHz: output has ~double the samples, preserving 20ms duration.

    audioop.ratecv of 160 samples 8k->16k yields 319; allow ±4 for edge rounding.
    """
    pcm = _pcm16(*([1000, -1000] * 80))  # 160 samples @ 8 kHz = 20 ms
    frame = PcmFrame(samples=pcm, sample_rate=8000, monotonic_ts_ns=0)
    out = resample_frame(frame, target_rate=16000)
    out_samples = out.sample_count
    assert 315 <= out_samples <= 323, (
        f"8k->16k: expected ~319 samples, got {out_samples}"
    )


def test_resample_frame_16k_to_8k_preserves_duration() -> None:
    """16kHz -> 8kHz: output has ~half the samples, preserving 20ms duration.

    audioop.ratecv of 320 samples 16k->8k yields 160; allow ±4 for edge rounding.
    """
    pcm = _pcm16(*([500] * 320))  # 320 samples @ 16 kHz = 20 ms
    frame = PcmFrame(samples=pcm, sample_rate=16000, monotonic_ts_ns=0)
    out = resample_frame(frame, target_rate=8000)
    out_samples = out.sample_count
    assert 156 <= out_samples <= 164, (
        f"16k->8k: expected ~160 samples, got {out_samples}"
    )


def test_resample_frame_preserves_monotonic_ts() -> None:
    """resample_frame preserves monotonic_ts_ns unchanged across a rate change.

    monotonic_ts_ns is a rate-independent wall-clock nanosecond presentation
    timestamp — the same contract that FrameUpsampler.upsample and
    MediaEngine._to_wire_rate both follow.  Scaling it by target_rate/source_rate
    would break the shared monotonic timebase used by VAD, endpointing, A/V-sync,
    and barge-in (cross-review finding, corrected from the original scaled spec).

    A frame at 8000 Hz with ts=20_000_000 ns resampled to 16000 Hz must carry
    the same ts=20_000_000 ns — the wall-clock moment is rate-independent.
    """
    # One 20ms frame in at 8kHz: 160 samples @ 8000 Hz = 20ms = 20_000_000 ns
    pcm = _pcm16(*([1000] * 160))
    ts_in = 20_000_000
    frame = PcmFrame(samples=pcm, sample_rate=8000, monotonic_ts_ns=ts_in)
    out = resample_frame(frame, target_rate=16000)
    assert out.monotonic_ts_ns == ts_in


def test_resample_frame_ts_zero_stays_zero() -> None:
    """resample_frame with ts=0 preserves ts=0 (zero is a valid wall-clock ts)."""
    pcm = _pcm16(*([1000] * 160))
    frame = PcmFrame(samples=pcm, sample_rate=8000, monotonic_ts_ns=0)
    out = resample_frame(frame, target_rate=16000)
    assert out.monotonic_ts_ns == 0


def test_resample_frame_identity_returns_pcmframe() -> None:
    """target_rate == frame.sample_rate returns a PcmFrame with correct fields.

    Identity case: the samples and ts are preserved exactly (no DSP applied).
    The return value is still a PcmFrame (not the original object) — frozen
    dataclasses are immutable so the returned instance may be the same object,
    but the contract guarantees it is a PcmFrame with the correct field values.
    """
    pcm = _pcm16(*([1000, -1000] * 80))  # 160 samples @ 8 kHz
    frame = PcmFrame(samples=pcm, sample_rate=8000, monotonic_ts_ns=42_000_000)
    out = resample_frame(frame, target_rate=8000)
    assert isinstance(out, PcmFrame)
    assert out.sample_rate == 8000
    assert out.samples == pcm
    assert out.monotonic_ts_ns == 42_000_000


def test_resample_frame_rejects_zero_target_rate() -> None:
    """target_rate=0 raises the same ValueError as Resampler(rate, 0)."""
    frame = PcmFrame(samples=_pcm16(1, 2, 3), sample_rate=8000, monotonic_ts_ns=0)
    with pytest.raises(ValueError, match="positive"):
        resample_frame(frame, target_rate=0)


def test_resample_frame_rejects_negative_target_rate() -> None:
    """target_rate < 0 raises ValueError from the underlying Resampler."""
    frame = PcmFrame(samples=_pcm16(1, 2, 3), sample_rate=8000, monotonic_ts_ns=0)
    with pytest.raises(ValueError, match="positive"):
        resample_frame(frame, target_rate=-8000)


def test_resample_frame_rejects_bool_target_rate() -> None:
    """Bool target_rate raises the same ValueError as Resampler rejects bool rates."""
    frame = PcmFrame(samples=_pcm16(1, 2, 3), sample_rate=8000, monotonic_ts_ns=0)
    with pytest.raises(ValueError, match="integer"):
        resample_frame(
            frame, target_rate=True
        )  # bool is subtype of int; runtime guard catches it


def test_resample_frame_rejects_float_target_rate() -> None:
    """Float target_rate raises ValueError (non-int rate contract from Resampler)."""
    frame = PcmFrame(samples=_pcm16(1, 2, 3), sample_rate=8000, monotonic_ts_ns=0)
    with pytest.raises(ValueError, match="integer"):
        resample_frame(frame, target_rate=16000.0)  # type: ignore[arg-type]


def test_resample_frame_does_not_mutate_input() -> None:
    """resample_frame never mutates the input frame (PcmFrame is frozen)."""
    pcm = _pcm16(*([1000] * 160))
    frame = PcmFrame(samples=pcm, sample_rate=8000, monotonic_ts_ns=0)
    out = resample_frame(frame, target_rate=16000)
    # Input frame is unchanged.
    assert frame.samples == pcm
    assert frame.sample_rate == 8000
    # Output is a different PcmFrame.
    assert out is not frame or out.sample_rate != frame.sample_rate


# ---------------------------------------------------------------------------
# audioop.error wrapping — domain ValueError contract at codec/resample boundaries
# ---------------------------------------------------------------------------


def test_encode_ulaw_wraps_audioop_error_as_value_error() -> None:
    """encode_ulaw re-raises audioop.error as ValueError (domain exception contract)."""
    with unittest.mock.patch("hermes_voip.media.audio.audioop") as mock_audioop:
        mock_audioop.lin2ulaw.side_effect = audioop.error("simulated ulaw encode error")
        with pytest.raises(ValueError, match="simulated ulaw encode error") as exc_info:
            encode_ulaw(_pcm16(0, 1))
    assert isinstance(exc_info.value.__cause__, audioop.error)


def test_encode_alaw_wraps_audioop_error_as_value_error() -> None:
    """encode_alaw re-raises audioop.error as ValueError (domain exception contract)."""
    with unittest.mock.patch("hermes_voip.media.audio.audioop") as mock_audioop:
        mock_audioop.lin2alaw.side_effect = audioop.error("simulated alaw encode error")
        with pytest.raises(ValueError, match="simulated alaw encode error") as exc_info:
            encode_alaw(_pcm16(0, 1))
    assert isinstance(exc_info.value.__cause__, audioop.error)


def test_decode_ulaw_wraps_audioop_error_as_value_error() -> None:
    """decode_ulaw re-raises audioop.error as ValueError (domain exception contract).

    audioop.error is a raw stdlib exception (not ValueError); leaking it to callers
    breaks the module's coherent exception contract (all bad-input errors are
    ValueError).  When the underlying audioop.ulaw2lin raises audioop.error the
    function must catch it and re-raise as ``ValueError`` with ``from exc`` so the
    original cause is preserved in the chain.

    Raises:
        ValueError: When audioop raises audioop.error internally.
    """
    with unittest.mock.patch("hermes_voip.media.audio.audioop") as mock_audioop:
        mock_audioop.ulaw2lin.side_effect = audioop.error("simulated ulaw decode error")
        with pytest.raises(ValueError, match="simulated ulaw decode error"):
            decode_ulaw(b"\xff\xff")


def test_decode_alaw_wraps_audioop_error_as_value_error() -> None:
    """decode_alaw re-raises audioop.error as ValueError (domain exception contract).

    Same contract as decode_ulaw: audioop.alaw2lin errors are wrapped so callers
    only ever see ValueError from this module's public API.

    Raises:
        ValueError: When audioop raises audioop.error internally.
    """
    with unittest.mock.patch("hermes_voip.media.audio.audioop") as mock_audioop:
        mock_audioop.alaw2lin.side_effect = audioop.error("simulated alaw decode error")
        with pytest.raises(ValueError, match="simulated alaw decode error"):
            decode_alaw(b"\xff\xff")


def test_resampler_resample_wraps_audioop_error_as_value_error() -> None:
    """Resampler.resample re-raises audioop.error as ValueError.

    audioop.ratecv can raise audioop.error for edge-case inputs (e.g. an input
    buffer whose byte count is not a whole number of frames for the given channel
    width).  The Resampler pre-validates input with _validate_pcm16 to catch the
    common case, but a defensive wrap ensures that any residual audioop.error from
    audioop.ratecv propagates as ValueError — the module's coherent error type —
    rather than leaking the raw C-layer exception to callers.

    Raises:
        ValueError: When audioop raises audioop.error during rate conversion.
    """
    r = Resampler(8000, 16000)
    with unittest.mock.patch("hermes_voip.media.audio.audioop") as mock_audioop:
        mock_audioop.ratecv.side_effect = audioop.error("simulated ratecv error")
        pcm = _pcm16(*([0] * 160))
        with pytest.raises(ValueError, match="simulated ratecv error"):
            r.resample(pcm)


def test_decode_ulaw_audioop_error_preserves_cause_chain() -> None:
    """Wrapped ValueError from decode_ulaw preserves the original audioop.error cause.

    ``raise ValueError(...) from exc`` sets ``__cause__`` so debuggers and
    tracebacks can still show the original audioop detail while callers see
    only ValueError.
    """
    original = audioop.error("original cause")
    with unittest.mock.patch("hermes_voip.media.audio.audioop") as mock_audioop:
        mock_audioop.ulaw2lin.side_effect = original
        with pytest.raises(ValueError, match="original cause") as exc_info:
            decode_ulaw(b"\x00")
    assert exc_info.value.__cause__ is original


def test_resampler_resample_audioop_error_preserves_cause_chain() -> None:
    """Wrapped ValueError from Resampler.resample preserves audioop.error cause."""
    original = audioop.error("original ratecv cause")
    r = Resampler(8000, 16000)
    with unittest.mock.patch("hermes_voip.media.audio.audioop") as mock_audioop:
        mock_audioop.ratecv.side_effect = original
        pcm = _pcm16(*([0] * 160))
        with pytest.raises(ValueError, match="original ratecv cause") as exc_info:
            r.resample(pcm)
    assert exc_info.value.__cause__ is original
