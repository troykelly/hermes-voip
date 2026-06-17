"""AEC config surface + the AEC-aware barge-in threshold default (ADR-0033).

The in-process echo canceller is configured by the ``HERMES_VOIP_AEC_*`` keys,
all with safe defaults (enabled, telephony-sensible filter). With AEC enabled the
default ``HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS`` drops from 600 ms (the echo-safe
ADR-0023 baseline) to 200 ms — responsive barge-in, because the echo is cancelled
before the VAD. An explicit threshold always wins; disabling AEC restores 600 ms.
"""

from __future__ import annotations

import pytest

from hermes_voip.config import ConfigError, MediaConfig, load_media_config


def test_aec_defaults() -> None:
    """AEC is on by default with telephony-sensible filter parameters."""
    cfg = load_media_config({})
    assert cfg.aec_enabled is True
    assert cfg.aec_filter_ms == 32
    assert cfg.aec_bulk_delay_ms == 0
    assert cfg.aec_mu == pytest.approx(0.30)


def test_aec_enabled_lowers_barge_in_threshold_default() -> None:
    """With AEC on and the threshold key unset, the default is the responsive 200 ms."""
    cfg = load_media_config({})
    assert cfg.aec_enabled is True
    assert cfg.barge_in_min_speech_ms == 200


def test_aec_disabled_restores_600ms_barge_in_default() -> None:
    """Disabling AEC restores the echo-safe 600 ms sustained-gate default (ADR-0023)."""
    cfg = load_media_config({"HERMES_VOIP_AEC_ENABLED": "false"})
    assert cfg.aec_enabled is False
    assert cfg.barge_in_min_speech_ms == 600


def test_explicit_barge_in_threshold_overrides_aec_default() -> None:
    """An explicit barge-in threshold always wins over the AEC-aware default."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_AEC_ENABLED": "true",
            "HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS": "450",
        }
    )
    assert cfg.aec_enabled is True
    assert cfg.barge_in_min_speech_ms == 450


def test_aec_full_override() -> None:
    """Every AEC knob is overridable from env."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_AEC_ENABLED": "true",
            "HERMES_VOIP_AEC_FILTER_MS": "48",
            "HERMES_VOIP_AEC_BULK_DELAY_MS": "20",
            "HERMES_VOIP_AEC_MU": "0.7",
        }
    )
    assert cfg.aec_filter_ms == 48
    assert cfg.aec_bulk_delay_ms == 20
    assert cfg.aec_mu == pytest.approx(0.7)


def test_aec_filter_ms_must_be_positive() -> None:
    with pytest.raises(ConfigError, match="aec_filter_ms"):
        MediaConfig(
            stt_provider="sherpa-onnx",
            stt_model_dir=None,
            tts_provider="sherpa-kokoro",
            tts_model=None,
            tts_voice=None,
            elevenlabs_api_key=None,
            deepgram_api_key=None,
            cartesia_api_key=None,
            vad_threshold=0.5,
            endpoint_silence_ms=500,
            duplex_mode="half",
            greeting="",
            rtp_symmetric=True,
            barge_in_mode="gated",
            barge_in_min_speech_ms=200,
            barge_in_tail_ms=250,
            barge_in_fade_ms=30,
            injection_guard="onnx",
            injection_guard_model_dir=None,
            dtmf_mode="auto",
            dtmf_interdigit_ms=None,
            dtmf_inband_enabled=True,
            tone_secs=0.0,
            aec_enabled=True,
            aec_filter_ms=0,
        )


def test_aec_mu_must_be_in_open_unit_interval() -> None:
    """The NLMS step size ``mu`` is in (0, 2); 0 and >= 2 are rejected."""
    with pytest.raises(ConfigError, match="aec_mu"):
        load_media_config({"HERMES_VOIP_AEC_MU": "0"})
    with pytest.raises(ConfigError, match="aec_mu"):
        load_media_config({"HERMES_VOIP_AEC_MU": "2"})
    with pytest.raises(ConfigError, match="aec_mu"):
        load_media_config({"HERMES_VOIP_AEC_MU": "2.5"})


def test_aec_bulk_delay_ms_must_be_non_negative() -> None:
    with pytest.raises(ConfigError, match="aec_bulk_delay_ms"):
        load_media_config({"HERMES_VOIP_AEC_BULK_DELAY_MS": "-5"})
