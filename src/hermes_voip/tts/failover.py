"""Automatic TTS failover: cloud primary → self-host fallback (ADR-0025).

The live incident: on a real call the ElevenLabs streaming request returned **HTTP
400** during the greeting synthesis; the error rose out of the call's
``TaskGroup``, cancelled the whole call, and the caller heard **silence** (no
``rtp tx``). A single recoverable TTS fault should not kill the call — the
self-host synthesiser is right there and could have spoken instead.

:class:`FailoverTTS` wraps a primary :class:`~hermes_voip.providers.tts.StreamingTTS`
with a **lazily-built** fallback (ADR-0004 seam). When the primary raises during
synthesis (the HTTP 400, a timeout, a connection error, or **any** exception from
the primary stream), the wrapper synthesises the *same* utterance via the fallback
instead, so the call still gets audio — never silence, never a failed call (rule
37: the primary error is **logged**, never swallowed, while the call recovers).

Design (ADR-0025):

* **Buffer + replay.** The agent's ``text`` iterator is single-use, so the wrapper
  tees it — recording each chunk as the primary pulls it. On a primary failure
  *before any frame was emitted for the utterance* (the 400 case: ``urlopen``
  raises before audio), the recorded chunks **plus the not-yet-consumed remainder**
  of the original iterator are replayed to the fallback — the full utterance is
  re-synthesised. If the primary fails *after* emitting ≥1 frame, that partial
  audio is already on the wire, so the utterance is **not** replayed (no
  double-speak); the wrapper latches and recovers for subsequent utterances.
* **Latch per call.** After the first primary failure the wrapper **latches**:
  every later ``synthesize`` goes straight to the fallback (no primary retry), so
  the call never flaps between the cloud and local voices mid-stream.
  :meth:`reset_failover` clears the latch at the start of each call (the providers
  are process-wide), so a fresh call retries the primary.
* **Zero happy-path cost.** The fallback is built by ``fallback_factory`` **only on
  the first failover** and cached, so the self-host model is never loaded unless the
  primary actually fails. When the primary succeeds, the wrapper adds only the
  bookkeeping of the in-flight utterance's text chunks.
* **Codec-gated rate.** The per-call negotiated wire ``sample_rate`` (ADR-0022) is
  forwarded to whichever provider synthesises, so the fallback (Kokoro) emits at the
  negotiated rate too.

The wrapper is generic over ``StreamingTTS`` — not ElevenLabs-specific — so it works
for any primary/fallback pair (rule 40: no vendor lock-in in the core).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from typing import Protocol, runtime_checkable

from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.tts import StreamingTTS, TtsStream

__all__ = [
    "FailoverTTS",
    "SupportsAudioTags",
    "SupportsCallReset",
    "reset_failover_if_supported",
]

_log = logging.getLogger("hermes_voip.tts.failover")


@runtime_checkable
class SupportsCallReset(Protocol):
    """A provider with a per-call reset hook (the failover latch, ADR-0025).

    The call loop calls :func:`reset_failover_if_supported` at the start of each
    call; a provider that implements ``reset_failover`` (i.e. :class:`FailoverTTS`)
    clears its per-call state, while a plain provider is left untouched.
    """

    def reset_failover(self) -> None:
        """Reset per-call failover state so a fresh call retries the primary."""
        ...


@runtime_checkable
class SupportsAudioTags(Protocol):
    """A provider that declares whether it preserves ElevenLabs v3 audio tags.

    Concrete providers (:class:`~hermes_voip.tts.elevenlabs.ElevenLabsTTS`,
    :class:`~hermes_voip.tts.sherpa_kokoro.SherpaKokoroTTS`) expose this so the
    wrapper can mirror the primary's capability (ADR-0027). It is not on the core
    ``StreamingTTS`` seam — the tag decision is made *inside* each provider's
    ``synthesize`` — so this is a duck-typed capability check, not a requirement.
    """

    @property
    def preserves_audio_tags(self) -> bool:
        """Whether this provider renders v3 audio tags (kept) or strips them."""
        ...


def reset_failover_if_supported(tts: StreamingTTS) -> None:
    """Reset ``tts``'s per-call failover latch if it supports the hook (else no-op).

    Called by ``CallLoop.run()`` at call start so a :class:`FailoverTTS` retries the
    primary on a fresh call. Duck-typed via :class:`SupportsCallReset` so a plain
    ``StreamingTTS`` (which has no such method) is left untouched.
    """
    if isinstance(tts, SupportsCallReset):
        tts.reset_failover()


class _TeeText:
    """An ``AsyncIterator[str]`` that records every chunk it yields downstream.

    Wraps the agent's single-use text iterator and appends each pulled chunk to a
    shared ``recorded`` list, so a failover can replay exactly the text the primary
    consumed plus whatever remains. ``exhausted`` flips true once the source ends.
    """

    def __init__(self, source: AsyncIterator[str], recorded: list[str]) -> None:
        self._source = source
        self._recorded = recorded
        self.exhausted = False

    def __aiter__(self) -> AsyncIterator[str]:
        return self

    async def __anext__(self) -> str:
        try:
            chunk = await self._source.__anext__()
        except StopAsyncIteration:
            self.exhausted = True
            raise
        self._recorded.append(chunk)
        return chunk


async def _replay_text(
    recorded: list[str], source: AsyncIterator[str], exhausted: bool
) -> AsyncIterator[str]:
    """Yield the recorded chunks, then drain any remainder of the original source.

    Reconstructs the full utterance text for the fallback: ``recorded`` is exactly
    what the primary pulled before it failed, and (if the source was not yet
    ``exhausted``) the rest of ``source`` is the un-consumed remainder. Together they
    are the lossless original stream — no text dropped, none duplicated.
    """
    for chunk in recorded:
        yield chunk
    if not exhausted:
        async for chunk in source:
            yield chunk


class FailoverTTS:
    """A ``StreamingTTS`` that falls back to a self-host provider on primary failure.

    Wraps ``primary`` with a lazily-built fallback (from ``fallback_factory``). A
    primary synthesis failure recovers by synthesising the same utterance via the
    fallback; after the first failure the wrapper latches to the fallback for the
    rest of the call (cleared by :meth:`reset_failover`). See the module docstring /
    ADR-0025 for the full design.
    """

    def __init__(
        self,
        *,
        primary: StreamingTTS,
        fallback_factory: Callable[[], StreamingTTS],
    ) -> None:
        """Wire the wrapper.

        Args:
            primary: The preferred provider (e.g. ElevenLabs). Tried first on every
                utterance until a failure latches the wrapper to the fallback.
            fallback_factory: A zero-arg factory returning the fallback provider
                (e.g. sherpa-Kokoro). Invoked **only on the first failover** and the
                result cached — so the fallback's model is not loaded on the happy
                path, but can load on demand when the primary fails.
        """
        self._primary = primary
        self._fallback_factory = fallback_factory
        self._fallback: StreamingTTS | None = None
        # Per-call latch: once the primary has failed this call, every later
        # utterance goes straight to the fallback (no flapping). Reset per call.
        self._latched = False

    @property
    def output_sample_rate(self) -> int:
        """Mirror the primary's declared default rate (the media layer reconciles)."""
        return self._primary.output_sample_rate

    @property
    def preserves_audio_tags(self) -> bool:
        """Report the PRIMARY's audio-tag capability (ADR-0027).

        The actual per-utterance tag fate is decided inside whichever provider
        synthesises: the primary keeps or strips tags per its own model, and on a
        failover the fallback (always a non-tag self-host model) strips them when it
        re-synthesises the replayed utterance. This property reflects the primary so
        the configured capability is observable; it is **not** consulted to drive the
        stripping (each provider does that itself), so a latched fallback does not
        change it. Duck-typed via :class:`SupportsAudioTags` — a primary without the
        property is treated as not tag-capable.
        """
        primary = self._primary
        if isinstance(primary, SupportsAudioTags):
            return primary.preserves_audio_tags
        return False

    def reset_failover(self) -> None:
        """Clear the per-call failover latch so a fresh call retries the primary."""
        self._latched = False

    def _ensure_fallback(self) -> StreamingTTS:
        """Build the fallback on first use and cache it (lazy; zero happy-path cost)."""
        if self._fallback is None:
            self._fallback = self._fallback_factory()
        return self._fallback

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        """Synthesise via the primary, falling back to self-host on a primary failure.

        Returns a :class:`~hermes_voip.providers.tts.TtsStream`. While not latched,
        the primary is tried first and its frames are streamed; if it raises before
        any frame, the buffered+remaining text is replayed to the fallback. Once
        latched (a prior failure this call), synthesis goes straight to the fallback.
        ``sample_rate`` (the per-call negotiated wire rate) is forwarded to whichever
        provider synthesises (ADR-0022).
        """
        if self._latched:
            # A prior failure this call: skip the primary entirely (no flapping).
            fallback = self._ensure_fallback()
            return fallback.synthesize(text, voice, sample_rate=sample_rate)
        return _FailoverStream(self, text, voice, sample_rate)


class _FailoverStream:
    """One utterance's ``TtsStream``: primary first, fallback on a pre-audio failure.

    Tees the input text so a failover can replay it. On the first frame pull it opens
    the primary stream; if the primary raises **before** yielding any frame, it logs
    the failure (WARNING — never swallowed, rule 37), latches the wrapper, and opens
    the fallback over the replayed text. A primary failure *after* frames have been
    emitted does not replay (no double-speak) — it latches and ends the utterance.
    """

    def __init__(
        self,
        owner: FailoverTTS,
        text: AsyncIterator[str],
        voice: str,
        sample_rate: int | None,
    ) -> None:
        self._owner = owner
        self._voice = voice
        self._sample_rate = sample_rate
        self._recorded: list[str] = []
        self._source = text
        self._tee = _TeeText(text, self._recorded)
        # The provider stream we are currently draining (primary, then fallback).
        self._active: TtsStream | None = None
        # Whether we have already switched to the fallback for this utterance.
        self._failed_over = False
        # Whether the active (primary) stream has emitted ≥1 frame this utterance.
        self._emitted = False
        self._started = False

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        if not await self._ensure_started():
            # The primary's synchronous synthesize() failed AND the utterance could
            # not be replayed (only possible if it had emitted audio — impossible at
            # open). Defensive: end the stream.
            raise StopAsyncIteration  # pragma: no cover - open emits no audio
        while True:
            active = self._active
            if active is None:  # pragma: no cover - defensive; _ensure_started sets it
                raise StopAsyncIteration
            try:
                frame = await active.__anext__()
            except StopAsyncIteration:
                raise
            # Catch ANY primary exception (HTTP 400/timeout/connection/...) so the
            # call recovers via the fallback. It is NOT swallowed: it is logged at
            # WARNING in _begin_fallback, and re-raised if the fallback also fails or
            # the primary already emitted audio (rule 37).
            except Exception as exc:
                if self._failed_over:
                    # The FALLBACK itself failed: nothing left to recover to, so the
                    # error propagates (never silently swallowed — rule 37).
                    raise
                if not await self._begin_fallback(exc):
                    # Cannot replay (primary already emitted audio): end the utterance.
                    raise StopAsyncIteration from None
                # Loop again to pull the first frame from the fallback stream.
                continue
            else:
                self._emitted = True
                return frame

    async def _ensure_started(self) -> bool:
        """Open the primary stream once; recover via the fallback on a SYNC failure.

        The primary's ``synthesize`` is a synchronous factory that can raise eagerly
        (e.g. ElevenLabs rejects an unsupported per-call sample rate) — so opening it
        is wrapped in the same failover path as a streamed failure (codec/ADR-0025).
        Returns ``True`` once a stream (primary or fallback) is active, ``False`` only
        if recovery decided the utterance is finished (never at open, since no audio
        has been emitted yet).
        """
        if self._started:
            return self._active is not None or self._failed_over
        self._started = True
        try:
            self._active = self._owner._primary.synthesize(
                self._tee, self._voice, sample_rate=self._sample_rate
            )
        except StopAsyncIteration:  # pragma: no cover - not raised by a sync factory
            raise
        # A synchronous synthesize() failure (e.g. ElevenLabs rejecting an unsupported
        # per-call rate) must recover via the fallback too — caught broadly on purpose,
        # then logged at WARNING in _begin_fallback (never swallowed; re-raised if the
        # fallback also fails — rule 37).
        except Exception as exc:  # noqa: BLE001 - ANY primary open failure must recover
            return await self._begin_fallback(exc)
        return True

    async def _begin_fallback(self, exc: BaseException) -> bool:
        """Recover from a primary failure: log, latch, and open the fallback.

        Returns ``True`` when the fallback was opened over the replayed utterance
        text (the caller pulls from it next), or ``False`` when the utterance cannot
        be replayed because the primary already emitted audio (no double-speak) — the
        caller then ends the utterance and later utterances use the latched fallback.
        """
        self._failed_over = True
        self._owner._latched = True
        # Close the failed primary stream (best-effort; its own error already raised).
        await self._aclose_active()
        if self._emitted:
            # Partial audio already played: do NOT replay (would double-speak the
            # start). Latch + end this utterance; later utterances use the fallback.
            _log.warning(
                "TTS primary failed mid-utterance after audio; latching to fallback "
                "for the rest of the call (utterance truncated, not replayed): %r",
                exc,
                extra={
                    "event": "tts_primary_failover",
                    "emitted_frames": self._emitted,
                },
            )
            return False
        _log.warning(
            "TTS primary synthesis failed before any audio; falling back to the "
            "self-host TTS for this call so the call still gets audio: %r",
            exc,
            extra={"event": "tts_primary_failover", "emitted_frames": self._emitted},
        )
        fallback = self._owner._ensure_fallback()
        replay = _replay_text(self._recorded, self._source, self._tee.exhausted)
        self._active = fallback.synthesize(
            replay, self._voice, sample_rate=self._sample_rate
        )
        return True

    async def flush(self) -> None:
        """Forward ``flush`` to the active stream (open the primary if not yet started).

        The call loop's first-audio lever forces buffered text to synthesis; ensure a
        stream exists so the flush reaches a real provider stream. Opening goes through
        :meth:`_ensure_started`, so a synchronous primary failure here also recovers
        via the fallback rather than escaping.
        """
        await self._ensure_started()
        if self._active is not None:
            await self._active.flush()

    async def cancel(self) -> None:
        """Barge-in: cancel the active stream (idempotent)."""
        if self._active is not None:
            await self._active.cancel()

    async def aclose(self) -> None:
        """Close the active stream and release its backend (idempotent)."""
        await self._aclose_active()

    async def _aclose_active(self) -> None:
        """Close the active provider stream, if any (idempotent)."""
        active, self._active = self._active, None
        if active is not None:
            await active.aclose()


# Structural conformance to the ADR-0004 seam (mypy + runtime_checkable Protocol).
_: type[StreamingTTS] = FailoverTTS
_reset: type[SupportsCallReset] = FailoverTTS
