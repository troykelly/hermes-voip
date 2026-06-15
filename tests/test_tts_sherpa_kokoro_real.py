"""Real-engine smoke test for SherpaKokoroTTS (skipped without weights/ml extra).

The model-free behavioural suite (``test_tts_sherpa_kokoro.py``) covers the
streaming/flush/cancel contract with a fake backend. This test exercises the
*actual* sherpa-onnx + Kokoro path end to end, so it is skipped unless both the
``ml`` extra is installed and a Kokoro model directory is provided via
``HERMES_VOIP_TTS_MODEL`` (a real dir with ``model.onnx``/``voices.bin``/
``tokens.txt``). CI without weights skips; a developer with weights gets real
coverage. No weights are committed (PUBLIC repo).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from hermes_voip.providers.audio import PcmFrame

pytest.importorskip("sherpa_onnx", reason="ml extra not installed")
pytest.importorskip("numpy", reason="ml extra not installed")

# Safe to import at top: the importorskips above skip the whole module when the
# ml extra is absent (the CI case), and the module exists once sherpa is present.
from hermes_voip.tts.sherpa_kokoro import SherpaKokoroTTS

_MODEL_DIR_ENV = "HERMES_VOIP_TTS_MODEL"


def _model_dir() -> Path | None:
    raw = os.environ.get(_MODEL_DIR_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


@pytest.mark.asyncio
async def test_real_kokoro_synthesises_audio() -> None:
    """The real engine turns one short sentence into 24 kHz PCM frames."""
    model_dir = _model_dir()
    if model_dir is None:
        pytest.skip(f"set {_MODEL_DIR_ENV} to a Kokoro model dir to run this")

    tts = SherpaKokoroTTS(model_dir=str(model_dir))
    assert tts.output_sample_rate == 24_000

    async def _text() -> AsyncIterator[str]:
        yield "Hello. "

    frames: list[PcmFrame] = [f async for f in tts.synthesize(_text(), voice="af")]
    assert frames
    assert all(f.sample_rate == 24_000 for f in frames)
    assert sum(f.sample_count for f in frames) > 0
