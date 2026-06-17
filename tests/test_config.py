"""Tests for the indexed ``HERMES_SIP_*`` gateway/extension config scheme (ADR-0011).

Parsing is a pure function of an env :class:`~collections.abc.Mapping`; no process
environment is read here. Fakes only (host ``pbx.example.test``, ext ``1000``).
"""

from __future__ import annotations

import pytest

from hermes_voip.config import (
    DEFAULT_GREETING,
    ConfigError,
    ExtensionConfig,
    GatewayConfig,
    MediaConfig,
    load_gateway_config,
    load_media_config,
)


def _base(**over: str) -> dict[str, str]:
    env = {"HERMES_SIP_HOST": "pbx.example.test"}
    env.update(over)
    return env


# ---- happy paths -----------------------------------------------------------


def test_single_extension_backcompat() -> None:
    cfg = load_gateway_config(
        _base(HERMES_SIP_EXTENSION="1000", HERMES_SIP_PASSWORD="secret")
    )
    assert cfg.host == "pbx.example.test"
    assert cfg.transport == "tls"
    assert cfg.via_transport == "TLS"
    assert cfg.port == 5061
    assert cfg.expires == 300
    assert len(cfg.extensions) == 1
    ext = cfg.extensions[0]
    assert ext.extension == "1000"
    assert ext.username == "1000"  # defaults to the extension number
    assert ext.password == "secret"
    assert ext.index == 0
    assert cfg.default_extension is ext


def test_username_override_backcompat() -> None:
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION="1000",
            HERMES_SIP_PASSWORD="secret",
            HERMES_SIP_USERNAME="dialin",
        )
    )
    assert cfg.extensions[0].username == "dialin"


def test_n_extensions_indexed_sorted() -> None:
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION_2="1002",
            HERMES_SIP_PASSWORD_2="p2",
            HERMES_SIP_EXTENSION_1="1001",
            HERMES_SIP_PASSWORD_1="p1",
            HERMES_SIP_EXTENSION_10="1010",
            HERMES_SIP_PASSWORD_10="p10",
        )
    )
    assert [e.extension for e in cfg.extensions] == ["1001", "1002", "1010"]
    assert [e.index for e in cfg.extensions] == [1, 2, 10]
    assert cfg.extensions[0].password == "p1"


def test_indexed_username_override() -> None:
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION_1="1001",
            HERMES_SIP_PASSWORD_1="p1",
            HERMES_SIP_USERNAME_1="agent-one",
        )
    )
    assert cfg.extensions[0].username == "agent-one"


def test_transport_wss_defaults() -> None:
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION="1000",
            HERMES_SIP_PASSWORD="x",
            HERMES_SIP_TRANSPORT="wss",
        )
    )
    assert cfg.transport == "wss"
    assert cfg.via_transport == "WSS"
    assert cfg.port == 443


def test_explicit_port_expires_and_user_agent() -> None:
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION="1000",
            HERMES_SIP_PASSWORD="x",
            HERMES_SIP_PORT="5070",
            HERMES_SIP_EXPIRES="120",
            HERMES_SIP_USER_AGENT="hermes-voip/test",
        )
    )
    assert cfg.port == 5070
    assert cfg.expires == 120
    assert cfg.user_agent == "hermes-voip/test"


def test_default_extension_explicit() -> None:
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION_1="1001",
            HERMES_SIP_PASSWORD_1="p1",
            HERMES_SIP_EXTENSION_2="1002",
            HERMES_SIP_PASSWORD_2="p2",
            HERMES_SIP_DEFAULT_EXTENSION="1002",
        )
    )
    assert cfg.default_extension.extension == "1002"


def test_default_extension_defaults_to_lowest_index() -> None:
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION_2="1002",
            HERMES_SIP_PASSWORD_2="p2",
            HERMES_SIP_EXTENSION_1="1001",
            HERMES_SIP_PASSWORD_1="p1",
        )
    )
    assert cfg.default_extension.extension == "1001"


def test_registration_config_builder() -> None:
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION="1000",
            HERMES_SIP_PASSWORD="secret",
            HERMES_SIP_TRANSPORT="tls",
            HERMES_SIP_PORT="5061",
        )
    )
    ext = cfg.extensions[0]
    rc = cfg.registration_config(
        ext,
        contact="<sip:1000@198.51.100.7:5061;transport=tls>",
        local_sent_by="198.51.100.7:5061",
    )
    assert rc.aor == "sip:1000@pbx.example.test"
    assert rc.username == "1000"
    assert rc.password == "secret"
    assert rc.transport == "TLS"
    assert rc.expires == 300
    assert rc.user_agent == "hermes-voip/0"
    assert rc.contact == "<sip:1000@198.51.100.7:5061;transport=tls>"
    assert rc.local_sent_by == "198.51.100.7:5061"


# ---- rejection cases -------------------------------------------------------


def test_missing_host_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(
            {"HERMES_SIP_EXTENSION": "1000", "HERMES_SIP_PASSWORD": "x"}
        )


def test_no_extensions_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(_base())


def test_missing_password_backcompat_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(_base(HERMES_SIP_EXTENSION="1000"))


def test_indexed_missing_password_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(_base(HERMES_SIP_EXTENSION_1="1001"))


def test_duplicate_extension_number_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(
                HERMES_SIP_EXTENSION_1="1001",
                HERMES_SIP_PASSWORD_1="p1",
                HERMES_SIP_EXTENSION_2="1001",
                HERMES_SIP_PASSWORD_2="p2",
            )
        )


def test_garbled_index_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(HERMES_SIP_EXTENSION_x="1001", HERMES_SIP_PASSWORD_x="p1")
        )


def test_mixing_bare_and_indexed_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(
                HERMES_SIP_EXTENSION="1000",
                HERMES_SIP_PASSWORD="x",
                HERMES_SIP_EXTENSION_1="1001",
                HERMES_SIP_PASSWORD_1="p1",
            )
        )


def test_orphan_indexed_password_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(
                HERMES_SIP_EXTENSION_1="1001",
                HERMES_SIP_PASSWORD_1="p1",
                HERMES_SIP_PASSWORD_2="p2",
            )
        )


def test_garbled_port_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(
                HERMES_SIP_EXTENSION="1000",
                HERMES_SIP_PASSWORD="x",
                HERMES_SIP_PORT="notaport",
            )
        )


def test_port_out_of_range_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(
                HERMES_SIP_EXTENSION="1000",
                HERMES_SIP_PASSWORD="x",
                HERMES_SIP_PORT="70000",
            )
        )


def test_invalid_transport_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(
                HERMES_SIP_EXTENSION="1000",
                HERMES_SIP_PASSWORD="x",
                HERMES_SIP_TRANSPORT="udp",
            )
        )


def test_empty_extension_value_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(_base(HERMES_SIP_EXTENSION="", HERMES_SIP_PASSWORD="x"))


def test_unknown_default_extension_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(
                HERMES_SIP_EXTENSION="1000",
                HERMES_SIP_PASSWORD="x",
                HERMES_SIP_DEFAULT_EXTENSION="9999",
            )
        )


def test_garbled_expires_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(
                HERMES_SIP_EXTENSION="1000",
                HERMES_SIP_PASSWORD="x",
                HERMES_SIP_EXPIRES="soon",
            )
        )


def test_gateway_config_is_frozen() -> None:
    cfg = load_gateway_config(
        _base(HERMES_SIP_EXTENSION="1000", HERMES_SIP_PASSWORD="x")
    )
    assert isinstance(cfg, GatewayConfig)
    with pytest.raises((AttributeError, TypeError)):
        cfg.host = "evil.example.test"  # type: ignore[misc]


# ---- review hardening: stray-bare mixing, self-validating type, foreign ext


def test_stray_bare_password_with_indexed_rejected() -> None:
    # A stray bare credential alongside the indexed scheme is a likely typo, not
    # a valid mix; it must not be silently ignored (codex MEDIUM).
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(
                HERMES_SIP_EXTENSION_1="1001",
                HERMES_SIP_PASSWORD_1="p1",
                HERMES_SIP_PASSWORD="stray",
            )
        )


def test_stray_bare_username_with_indexed_rejected() -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(
                HERMES_SIP_EXTENSION_1="1001",
                HERMES_SIP_PASSWORD_1="p1",
                HERMES_SIP_USERNAME="stray",
            )
        )


def _ext(index: int, number: str) -> ExtensionConfig:
    return ExtensionConfig(index=index, extension=number, username=number, password="p")


def test_gateway_config_rejects_empty_extensions() -> None:
    with pytest.raises(ConfigError):
        GatewayConfig(
            host="pbx.example.test",
            port=5061,
            transport="tls",
            expires=300,
            user_agent="hermes-voip/0",
            extensions=(),
            default_index=0,
        )


def test_gateway_config_rejects_unknown_default_index() -> None:
    with pytest.raises(ConfigError):
        GatewayConfig(
            host="pbx.example.test",
            port=5061,
            transport="tls",
            expires=300,
            user_agent="hermes-voip/0",
            extensions=(_ext(1, "1001"),),
            default_index=99,
        )


def test_gateway_config_rejects_duplicate_indices() -> None:
    with pytest.raises(ConfigError):
        GatewayConfig(
            host="pbx.example.test",
            port=5061,
            transport="tls",
            expires=300,
            user_agent="hermes-voip/0",
            extensions=(_ext(1, "1001"), _ext(1, "1002")),
            default_index=1,
        )


def test_registration_config_rejects_foreign_extension() -> None:
    cfg = load_gateway_config(
        _base(HERMES_SIP_EXTENSION="1000", HERMES_SIP_PASSWORD="secret")
    )
    foreign = _ext(7, "7777")
    with pytest.raises(ConfigError):
        cfg.registration_config(
            foreign,
            contact="<sip:7777@198.51.100.7:5061;transport=tls>",
            local_sent_by="198.51.100.7:5061",
        )


# ===========================================================================
# Media / provider / feature config (ADR-0006..0010): load_media_config
# ===========================================================================
#
# A second, independent parser over the same env Mapping. It is purely a
# function of its input (no process env), additive to the gateway scheme above,
# and never logs a secret: the cloud API keys live in repr-suppressed fields.


# ---- happy paths -----------------------------------------------------------


def test_media_defaults_when_env_empty() -> None:
    cfg = load_media_config({})
    assert isinstance(cfg, MediaConfig)
    # STT
    assert cfg.stt_provider == "sherpa-onnx"
    assert cfg.stt_model_dir is None
    # TTS
    assert cfg.tts_provider == "sherpa-kokoro"
    assert cfg.tts_model is None
    assert cfg.tts_voice is None
    # TTS failover (ADR-0025): a self-host primary has no fallback by default
    # (it is already the safe local path), so the knob resolves to None here.
    assert cfg.tts_fallback is None
    # cloud keys absent
    assert cfg.elevenlabs_api_key is None
    assert cfg.deepgram_api_key is None
    # VAD / endpointing / duplex
    assert cfg.vad_threshold == pytest.approx(0.5)
    assert cfg.endpoint_silence_ms == 500
    assert cfg.duplex_mode == "half"
    # greeting (ADR-0002 NAT-latch): a non-empty friendly default
    assert cfg.greeting == DEFAULT_GREETING
    assert cfg.greeting != ""
    # symmetric-RTP (comedia) latching is ON by default
    assert cfg.rtp_symmetric is True
    # echo-robust barge-in (ADR-0023): gated by default, telephony thresholds.
    # The default min-speech clears the longest observed gateway-echo burst
    # (~15 VAD windows ≈ 480 ms), so a 600 ms default has margin above it.
    assert cfg.barge_in_mode == "gated"
    assert cfg.barge_in_min_speech_ms == 600
    assert cfg.barge_in_tail_ms == 250
    # barge-in clean-stop fade (ADR-0028): a short click-free ramp on the cut.
    assert cfg.barge_in_fade_ms == 30
    # injection guard
    assert cfg.injection_guard == "onnx"
    assert cfg.injection_guard_model_dir is None
    # DTMF
    assert cfg.dtmf_mode == "auto"
    assert cfg.dtmf_interdigit_ms is None
    assert cfg.dtmf_inband_enabled is True


def test_media_full_override() -> None:
    cfg = load_media_config(
        {
            "HERMES_VOIP_STT_PROVIDER": "deepgram",
            "HERMES_VOIP_STT_MODEL_DIR": "/models/zipformer",
            "HERMES_VOIP_TTS_PROVIDER": "elevenlabs",
            "HERMES_VOIP_TTS_MODEL": "eleven_flash_v2_5",
            "HERMES_VOIP_TTS_VOICE": "rachel",
            "HERMES_VOIP_TTS_FALLBACK_MODEL": "/models/kokoro",
            "ELEVENLABS_API_KEY": "el-secret-token",
            "DEEPGRAM_API_KEY": "dg-secret-token",
            "HERMES_VOIP_VAD_THRESHOLD": "0.75",
            "HERMES_VOIP_ENDPOINT_SILENCE_MS": "650",
            "HERMES_VOIP_DUPLEX_MODE": "full",
            "HERMES_VOIP_INJECTION_GUARD": "sidecar",
            "HERMES_VOIP_INJECTION_GUARD_MODEL_DIR": "/models/deberta",
            "HERMES_SIP_DTMF_MODE": "rfc4733",
            "HERMES_SIP_DTMF_INTERDIGIT_MS": "120",
            "HERMES_SIP_DTMF_INBAND_ENABLED": "false",
            "HERMES_VOIP_GREETING": "Hi from the test gateway.",
            "HERMES_VOIP_RTP_SYMMETRIC": "false",
            "HERMES_VOIP_BARGE_IN_MODE": "full",
            "HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS": "600",
            "HERMES_VOIP_BARGE_IN_TAIL_MS": "150",
            "HERMES_VOIP_BARGE_IN_FADE_MS": "40",
        }
    )
    assert cfg.stt_provider == "deepgram"
    assert cfg.stt_model_dir == "/models/zipformer"
    assert cfg.tts_provider == "elevenlabs"
    assert cfg.tts_model == "eleven_flash_v2_5"
    assert cfg.tts_voice == "rachel"
    assert cfg.tts_fallback == "sherpa-kokoro"  # cloud primary -> Kokoro fallback
    assert cfg.tts_fallback_model == "/models/kokoro"
    assert cfg.elevenlabs_api_key == "el-secret-token"
    assert cfg.deepgram_api_key == "dg-secret-token"
    assert cfg.vad_threshold == pytest.approx(0.75)
    assert cfg.endpoint_silence_ms == 650
    assert cfg.duplex_mode == "full"
    assert cfg.injection_guard == "sidecar"
    assert cfg.injection_guard_model_dir == "/models/deberta"
    assert cfg.dtmf_mode == "rfc4733"
    assert cfg.dtmf_interdigit_ms == 120
    assert cfg.dtmf_inband_enabled is False
    assert cfg.greeting == "Hi from the test gateway."
    assert cfg.rtp_symmetric is False
    assert cfg.barge_in_mode == "full"
    assert cfg.barge_in_min_speech_ms == 600
    assert cfg.barge_in_tail_ms == 150
    assert cfg.barge_in_fade_ms == 40


def test_media_barge_in_fade_ms_zero_allowed() -> None:
    """A fade of 0 ms is valid (instant hard cut, no ramp)."""
    cfg = load_media_config({"HERMES_VOIP_BARGE_IN_FADE_MS": "0"})
    assert cfg.barge_in_fade_ms == 0


def test_media_barge_in_fade_ms_negative_rejected() -> None:
    """A negative fade is rejected (fail-fast)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_BARGE_IN_FADE_MS": "-5"})


def test_media_barge_in_mode_lowercased_and_validated() -> None:
    """``HERMES_VOIP_BARGE_IN_MODE`` is lower-cased and constrained to the enum."""
    cfg = load_media_config({"HERMES_VOIP_BARGE_IN_MODE": "OFF"})
    assert cfg.barge_in_mode == "off"


def test_media_barge_in_mode_unknown_rejected() -> None:
    """An unknown barge-in mode is rejected (fail-fast, no silent fallback)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_BARGE_IN_MODE": "loud"})


def test_media_barge_in_min_speech_ms_must_be_positive() -> None:
    """A non-positive minimum-speech window is rejected (would be instant)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS": "0"})


def test_media_barge_in_tail_ms_zero_allowed() -> None:
    """A tail of 0 ms is valid (gate disarms the instant TTS ends)."""
    cfg = load_media_config({"HERMES_VOIP_BARGE_IN_TAIL_MS": "0"})
    assert cfg.barge_in_tail_ms == 0


def test_media_barge_in_tail_ms_negative_rejected() -> None:
    """A negative tail is rejected (fail-fast)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_BARGE_IN_TAIL_MS": "-5"})


def test_media_rtp_timeout_defaults_to_20s() -> None:
    """The RTP-inactivity watchdog window defaults to 20 s (ADR-0026)."""
    cfg = load_media_config({})
    assert cfg.media_timeout_secs == 20


def test_media_rtp_timeout_override_accepted() -> None:
    """A valid override within [1, 300] is taken verbatim."""
    cfg = load_media_config({"HERMES_VOIP_RTP_TIMEOUT_SECS": "45"})
    assert cfg.media_timeout_secs == 45


def test_media_rtp_timeout_max_300_accepted() -> None:
    """The maximum (300 s) is accepted (inclusive bound)."""
    cfg = load_media_config({"HERMES_VOIP_RTP_TIMEOUT_SECS": "300"})
    assert cfg.media_timeout_secs == 300


def test_media_rtp_timeout_above_max_rejected() -> None:
    """A value above the 300 s cap is rejected (fail-fast, not silently clamped)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_RTP_TIMEOUT_SECS": "301"})


def test_media_rtp_timeout_zero_rejected() -> None:
    """0 is rejected: the watchdog floor is 1 s (a 0 here is a misconfiguration).

    (The engine accepts ``media_timeout_secs=0`` as 'disabled', but the operator
    knob requires a positive window in [1, 300] — disabling the safety watchdog is
    not a configuration we expose, since a silent drop would then hang forever.)
    """
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_RTP_TIMEOUT_SECS": "0"})


def test_media_greeting_explicit_empty_disables_greeting() -> None:
    """An explicitly-empty HERMES_VOIP_GREETING means 'no greeting' (kept ``""``).

    Unlike the optional provider/model fields (which collapse blank → ``None``),
    the greeting distinguishes 'unset' (use the friendly default) from
    'explicitly empty' (opt out of any opening greeting). The empty string is
    therefore preserved verbatim, not defaulted.
    """
    cfg = load_media_config({"HERMES_VOIP_GREETING": ""})
    assert cfg.greeting == ""


def test_media_greeting_whitespace_only_disables_greeting() -> None:
    """A whitespace-only greeting also opts out (trimmed to ``""``)."""
    cfg = load_media_config({"HERMES_VOIP_GREETING": "   "})
    assert cfg.greeting == ""


def test_media_greeting_is_trimmed() -> None:
    """A set greeting is trimmed of surrounding whitespace (consistent parser)."""
    cfg = load_media_config({"HERMES_VOIP_GREETING": "  Hello there.  "})
    assert cfg.greeting == "Hello there."


def test_media_values_are_trimmed() -> None:
    cfg = load_media_config(
        {
            "HERMES_VOIP_STT_PROVIDER": "  deepgram  ",
            "DEEPGRAM_API_KEY": "dg-x",  # deepgram (cloud) requires its key
            "HERMES_VOIP_TTS_VOICE": "  rachel  ",
            "HERMES_VOIP_VAD_THRESHOLD": "  0.3 ",
            "HERMES_SIP_DTMF_MODE": "  sip_info  ",
        }
    )
    assert cfg.stt_provider == "deepgram"
    assert cfg.tts_voice == "rachel"
    assert cfg.vad_threshold == pytest.approx(0.3)
    assert cfg.dtmf_mode == "sip_info"


def test_media_provider_tokens_lowercased() -> None:
    cfg = load_media_config(
        {
            "HERMES_VOIP_STT_PROVIDER": "SHERPA-ONNX",
            "HERMES_VOIP_DUPLEX_MODE": "Full",
            "HERMES_VOIP_INJECTION_GUARD": "ONNX",
            "HERMES_SIP_DTMF_MODE": "RFC4733",
        }
    )
    assert cfg.stt_provider == "sherpa-onnx"
    assert cfg.duplex_mode == "full"
    assert cfg.injection_guard == "onnx"
    assert cfg.dtmf_mode == "rfc4733"


# ---- provider enum + cloud-key fail-fast (review) ---------------------------


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("HERMES_VOIP_STT_PROVIDER", "deepgarm"),  # typo
        ("HERMES_VOIP_TTS_PROVIDER", "espeak"),  # unsupported
        ("HERMES_VOIP_INJECTION_GUARD", "none"),  # not a real guard
    ],
)
def test_media_unknown_provider_rejected(key: str, bad: str) -> None:
    with pytest.raises(ConfigError):
        load_media_config({key: bad})


def test_media_deepgram_stt_requires_key() -> None:
    with pytest.raises(ConfigError, match="DEEPGRAM_API_KEY"):
        load_media_config({"HERMES_VOIP_STT_PROVIDER": "deepgram"})


def test_media_elevenlabs_tts_requires_key() -> None:
    with pytest.raises(ConfigError, match="ELEVENLABS_API_KEY"):
        load_media_config({"HERMES_VOIP_TTS_PROVIDER": "elevenlabs"})


def test_media_cartesia_tts_requires_key() -> None:
    with pytest.raises(ConfigError, match="CARTESIA_API_KEY"):
        load_media_config({"HERMES_VOIP_TTS_PROVIDER": "cartesia"})


def test_media_aura2_tts_requires_deepgram_key() -> None:
    with pytest.raises(ConfigError, match="DEEPGRAM_API_KEY"):
        load_media_config({"HERMES_VOIP_TTS_PROVIDER": "aura2"})


def test_media_cloud_provider_with_key_accepted() -> None:
    cfg = load_media_config(
        {
            "HERMES_VOIP_TTS_PROVIDER": "cartesia",
            "HERMES_VOIP_CARTESIA_API_KEY": "c-x",
            # cartesia is a cloud primary, so it defaults to a Kokoro fallback that
            # needs its own model dir (ADR-0025).
            "HERMES_VOIP_TTS_FALLBACK_MODEL": "/models/kokoro",
        }
    )
    assert cfg.tts_provider == "cartesia"
    assert cfg.cartesia_api_key == "c-x"


# ---- TTS failover (ADR-0025): HERMES_VOIP_TTS_FALLBACK ----------------------


def test_media_tts_fallback_defaults_to_kokoro_for_cloud_primary() -> None:
    """A cloud primary (elevenlabs) defaults its fallback to sherpa-kokoro.

    The live incident: ElevenLabs 400'd and the call died silent. With no explicit
    ``HERMES_VOIP_TTS_FALLBACK``, a cloud primary gets the self-host Kokoro fallback
    so a primary failure recovers with audio instead of dropping the call.
    """
    cfg = load_media_config(
        {
            "HERMES_VOIP_TTS_PROVIDER": "elevenlabs",
            "ELEVENLABS_API_KEY": "el-x",
            # the Kokoro fallback needs its own model dir (the shared one is the EL id)
            "HERMES_VOIP_TTS_FALLBACK_MODEL": "/models/kokoro",
        }
    )
    assert cfg.tts_fallback == "sherpa-kokoro"


def test_media_tts_fallback_default_none_for_selfhost_primary() -> None:
    """A self-host primary (sherpa-kokoro) has no fallback by default."""
    cfg = load_media_config({})
    assert cfg.tts_provider == "sherpa-kokoro"
    assert cfg.tts_fallback is None


def test_media_tts_fallback_explicit_none_disables() -> None:
    """``HERMES_VOIP_TTS_FALLBACK=none`` disables failover for a cloud primary."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_TTS_PROVIDER": "elevenlabs",
            "ELEVENLABS_API_KEY": "el-x",
            "HERMES_VOIP_TTS_FALLBACK": "none",
        }
    )
    assert cfg.tts_fallback is None


def test_media_tts_fallback_explicit_provider() -> None:
    """An explicit fallback provider token is honoured (lower-cased)."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_TTS_PROVIDER": "elevenlabs",
            "ELEVENLABS_API_KEY": "el-x",
            "HERMES_VOIP_TTS_FALLBACK": "Sherpa-Kokoro",
            "HERMES_VOIP_TTS_FALLBACK_MODEL": "/models/kokoro",
        }
    )
    assert cfg.tts_fallback == "sherpa-kokoro"


def test_media_tts_fallback_unknown_token_rejected() -> None:
    """An unknown fallback provider token fails fast at config load."""
    with pytest.raises(ConfigError, match="tts_fallback"):
        load_media_config(
            {
                "HERMES_VOIP_TTS_PROVIDER": "elevenlabs",
                "ELEVENLABS_API_KEY": "el-x",
                "HERMES_VOIP_TTS_FALLBACK": "espeak",
            }
        )


def test_media_tts_fallback_must_differ_from_primary() -> None:
    """The fallback cannot equal the primary (a same-provider fallback is useless)."""
    with pytest.raises(ConfigError, match="tts_fallback"):
        load_media_config(
            {
                "HERMES_VOIP_TTS_PROVIDER": "elevenlabs",
                "ELEVENLABS_API_KEY": "el-x",
                "HERMES_VOIP_TTS_FALLBACK": "elevenlabs",
            }
        )


def test_media_kokoro_fallback_requires_its_own_model_dir() -> None:
    """A sherpa-kokoro fallback fails loud at startup without its own model dir.

    The shared HERMES_VOIP_TTS_MODEL is the ElevenLabs model id for the primary, NOT
    a Kokoro directory — so the fallback needs HERMES_VOIP_TTS_FALLBACK_MODEL. Without
    it, the Kokoro fallback could not be built, and the call would still die silent on
    the first EL failure. Require it at config load so the failure surfaces at startup.
    """
    with pytest.raises(ConfigError, match="HERMES_VOIP_TTS_FALLBACK_MODEL"):
        load_media_config(
            {
                "HERMES_VOIP_TTS_PROVIDER": "elevenlabs",
                "ELEVENLABS_API_KEY": "el-x",
                # default fallback = sherpa-kokoro, but no fallback model dir set
            }
        )


def test_media_kokoro_fallback_with_model_dir_accepted() -> None:
    """With the fallback model dir set, the cloud + Kokoro-fallback config loads."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_TTS_PROVIDER": "elevenlabs",
            "ELEVENLABS_API_KEY": "el-x",
            "HERMES_VOIP_TTS_FALLBACK_MODEL": "/models/kokoro",
        }
    )
    assert cfg.tts_fallback == "sherpa-kokoro"
    assert cfg.tts_fallback_model == "/models/kokoro"


def test_media_fallback_model_none_when_no_failover() -> None:
    """tts_fallback_model is None when failover is off (self-host primary)."""
    cfg = load_media_config({})
    assert cfg.tts_fallback is None
    assert cfg.tts_fallback_model is None


def test_media_non_model_fallback_does_not_require_fallback_model() -> None:
    """A fallback needing no model dir (another cloud) is fine without a dir."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_TTS_PROVIDER": "elevenlabs",
            "ELEVENLABS_API_KEY": "el-x",
            "HERMES_VOIP_TTS_FALLBACK": "cartesia",
            "HERMES_VOIP_CARTESIA_API_KEY": "c-x",
        }
    )
    assert cfg.tts_fallback == "cartesia"
    assert cfg.tts_fallback_model is None


def test_media_blank_optional_is_none_not_empty() -> None:
    # A present-but-blank optional collapses to None (unset), not "".
    cfg = load_media_config(
        {
            "HERMES_VOIP_STT_MODEL_DIR": "   ",
            "HERMES_VOIP_TTS_VOICE": "",
            "ELEVENLABS_API_KEY": "  ",
        }
    )
    assert cfg.stt_model_dir is None
    assert cfg.tts_voice is None
    assert cfg.elevenlabs_api_key is None


def test_media_dtmf_inband_bool_accepts_common_spellings() -> None:
    truthy = ("true", "TRUE", "1", "yes", "on", " True ")
    falsy = ("false", "FALSE", "0", "no", "off", " False ")
    for raw in truthy:
        cfg = load_media_config({"HERMES_SIP_DTMF_INBAND_ENABLED": raw})
        assert cfg.dtmf_inband_enabled is True
    for raw in falsy:
        cfg = load_media_config({"HERMES_SIP_DTMF_INBAND_ENABLED": raw})
        assert cfg.dtmf_inband_enabled is False


def test_media_all_dtmf_modes_accepted() -> None:
    for mode in ("auto", "rfc4733", "sip_info", "inband"):
        assert load_media_config({"HERMES_SIP_DTMF_MODE": mode}).dtmf_mode == mode


def test_media_all_duplex_modes_accepted() -> None:
    for mode in ("half", "full"):
        assert load_media_config({"HERMES_VOIP_DUPLEX_MODE": mode}).duplex_mode == mode


def test_media_vad_threshold_bounds_inclusive() -> None:
    assert load_media_config({"HERMES_VOIP_VAD_THRESHOLD": "0"}).vad_threshold == 0.0
    assert load_media_config({"HERMES_VOIP_VAD_THRESHOLD": "1"}).vad_threshold == 1.0


# ---- secrecy ---------------------------------------------------------------


def test_media_cloud_keys_absent_from_repr() -> None:
    # rule 34 / invariant: a secret env value must never reach a log line. The
    # repr is the most common accidental leak path, so the key fields are
    # repr-suppressed.
    cfg = load_media_config(
        {
            "ELEVENLABS_API_KEY": "el-super-secret",
            "DEEPGRAM_API_KEY": "dg-super-secret",
        }
    )
    text = repr(cfg)
    assert "el-super-secret" not in text
    assert "dg-super-secret" not in text
    # the value is still accessible by reference for the runtime to use
    assert cfg.elevenlabs_api_key == "el-super-secret"
    assert cfg.deepgram_api_key == "dg-super-secret"


# ---- rejection cases -------------------------------------------------------


def test_media_unknown_duplex_mode_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_DUPLEX_MODE": "quarter"})


def test_media_unknown_dtmf_mode_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_SIP_DTMF_MODE": "morse"})


def test_media_vad_threshold_not_a_float_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_VAD_THRESHOLD": "loud"})


def test_media_vad_threshold_above_one_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_VAD_THRESHOLD": "1.5"})


def test_media_vad_threshold_below_zero_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_VAD_THRESHOLD": "-0.1"})


def test_media_vad_threshold_nan_rejected() -> None:
    # NaN slips past a naive lo <= x <= hi check; it must be rejected.
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_VAD_THRESHOLD": "nan"})


def test_media_vad_threshold_inf_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_VAD_THRESHOLD": "inf"})


def test_media_endpoint_silence_not_int_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_ENDPOINT_SILENCE_MS": "soon"})


def test_media_endpoint_silence_zero_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_ENDPOINT_SILENCE_MS": "0"})


def test_media_endpoint_silence_negative_rejected() -> None:
    # The integer parser rejects a leading '-' as a non-digit; still ConfigError.
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_ENDPOINT_SILENCE_MS": "-5"})


def test_media_dtmf_interdigit_zero_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_SIP_DTMF_INTERDIGIT_MS": "0"})


def test_media_dtmf_interdigit_not_int_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_SIP_DTMF_INTERDIGIT_MS": "fast"})


def test_media_dtmf_inband_bad_bool_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_SIP_DTMF_INBAND_ENABLED": "maybe"})


# ---- self-validating type --------------------------------------------------


def test_media_config_is_frozen() -> None:
    cfg = load_media_config({})
    assert isinstance(cfg, MediaConfig)
    with pytest.raises((AttributeError, TypeError)):
        cfg.stt_provider = "evil"  # type: ignore[misc]


def test_media_config_validates_itself_on_direct_construction() -> None:
    # MediaConfig is public; constructing one with an out-of-range threshold
    # must fail in __post_init__, not only via the parser.
    with pytest.raises(ConfigError):
        MediaConfig(
            stt_provider="sherpa-onnx",
            stt_model_dir=None,
            tts_provider="sherpa-kokoro",
            tts_model=None,
            tts_voice=None,
            elevenlabs_api_key=None,
            deepgram_api_key=None,
            cartesia_api_key=None,
            vad_threshold=2.0,
            endpoint_silence_ms=500,
            duplex_mode="half",
            greeting="",
            rtp_symmetric=True,
            barge_in_mode="gated",
            barge_in_min_speech_ms=400,
            barge_in_tail_ms=250,
            barge_in_fade_ms=30,
            injection_guard="onnx",
            injection_guard_model_dir=None,
            dtmf_mode="auto",
            dtmf_interdigit_ms=None,
            dtmf_inband_enabled=True,
            tone_secs=0.0,
        )


def test_media_config_rejects_bad_enum_on_direct_construction() -> None:
    with pytest.raises(ConfigError):
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
            duplex_mode="sideways",
            greeting="",
            rtp_symmetric=True,
            barge_in_mode="gated",
            barge_in_min_speech_ms=400,
            barge_in_tail_ms=250,
            barge_in_fade_ms=30,
            injection_guard="onnx",
            injection_guard_model_dir=None,
            dtmf_mode="auto",
            dtmf_interdigit_ms=None,
            dtmf_inband_enabled=True,
            tone_secs=0.0,
        )


# ---------------------------------------------------------------------------
# HERMES_VOIP_TEST_TONE / tone_secs
# ---------------------------------------------------------------------------


def test_media_tone_secs_default_is_zero() -> None:
    """HERMES_VOIP_TEST_TONE absent -> tone_secs == 0.0 (normal operation)."""
    cfg = load_media_config({})
    assert cfg.tone_secs == 0.0


def test_media_tone_secs_parses_positive_float() -> None:
    """HERMES_VOIP_TEST_TONE=5 -> tone_secs == 5.0."""
    cfg = load_media_config({"HERMES_VOIP_TEST_TONE": "5"})
    assert cfg.tone_secs == 5.0


def test_media_tone_secs_parses_decimal() -> None:
    """HERMES_VOIP_TEST_TONE=2.5 -> tone_secs == 2.5."""
    cfg = load_media_config({"HERMES_VOIP_TEST_TONE": "2.5"})
    assert cfg.tone_secs == 2.5


def test_media_tone_secs_zero_is_accepted() -> None:
    """HERMES_VOIP_TEST_TONE=0 -> tone_secs == 0.0 (off, same as absent)."""
    cfg = load_media_config({"HERMES_VOIP_TEST_TONE": "0"})
    assert cfg.tone_secs == 0.0


def test_media_tone_secs_negative_rejected() -> None:
    """HERMES_VOIP_TEST_TONE=-1 must raise ConfigError (negative duration)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_TEST_TONE": "-1"})


def test_media_tone_secs_non_numeric_rejected() -> None:
    """HERMES_VOIP_TEST_TONE=abc must raise ConfigError (non-numeric)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_TEST_TONE": "abc"})


def test_media_tone_secs_validates_on_direct_construction() -> None:
    """MediaConfig(tone_secs=-1.0) must raise ConfigError in __post_init__."""
    with pytest.raises(ConfigError):
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
            barge_in_min_speech_ms=400,
            barge_in_tail_ms=250,
            barge_in_fade_ms=30,
            injection_guard="onnx",
            injection_guard_model_dir=None,
            dtmf_mode="auto",
            dtmf_interdigit_ms=None,
            dtmf_inband_enabled=True,
            tone_secs=-1.0,
        )


# ---------------------------------------------------------------------------
# ElevenLabs dynamic-voice tuning knobs (HERMES_VOIP_TTS_STABILITY / _STYLE /
# _SIMILARITY / _SPEAKER_BOOST / _STREAMING_LATENCY).  All optional: unset ->
# None, so the ElevenLabs provider applies its own dynamic default.  Set values
# are validated (floats in [0,1]; latency int in [0,4]).  These are the env
# surface that lets the operator A/B-test voice dynamism without a redeploy.
# ---------------------------------------------------------------------------


def test_media_tts_tuning_defaults_are_none() -> None:
    """Unset TTS-tuning knobs default to None (provider supplies the dynamic set)."""
    cfg = load_media_config({})
    assert cfg.tts_stability is None
    assert cfg.tts_style is None
    assert cfg.tts_similarity is None
    assert cfg.tts_speaker_boost is None
    assert cfg.tts_streaming_latency is None


def test_media_tts_tuning_parsed() -> None:
    """Each TTS-tuning knob parses to its typed value."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_TTS_STABILITY": "0.3",
            "HERMES_VOIP_TTS_STYLE": "0.15",
            "HERMES_VOIP_TTS_SIMILARITY": "0.8",
            "HERMES_VOIP_TTS_SPEAKER_BOOST": "false",
            "HERMES_VOIP_TTS_STREAMING_LATENCY": "1",
        }
    )
    assert cfg.tts_stability == pytest.approx(0.3)
    assert cfg.tts_style == pytest.approx(0.15)
    assert cfg.tts_similarity == pytest.approx(0.8)
    assert cfg.tts_speaker_boost is False
    assert cfg.tts_streaming_latency == 1


def test_media_tts_stability_bounds_inclusive() -> None:
    """The stability knob accepts the inclusive [0, 1] endpoints."""
    assert load_media_config({"HERMES_VOIP_TTS_STABILITY": "0"}).tts_stability == 0.0
    assert load_media_config({"HERMES_VOIP_TTS_STABILITY": "1"}).tts_stability == 1.0


@pytest.mark.parametrize(
    "key",
    [
        "HERMES_VOIP_TTS_STABILITY",
        "HERMES_VOIP_TTS_STYLE",
        "HERMES_VOIP_TTS_SIMILARITY",
    ],
)
@pytest.mark.parametrize("bad", ["1.5", "-0.1", "nan", "inf", "loud"])
def test_media_tts_float_knob_out_of_range_rejected(key: str, bad: str) -> None:
    """A float tuning knob outside [0, 1] (or non-numeric/NaN/inf) is rejected."""
    with pytest.raises(ConfigError):
        load_media_config({key: bad})


def test_media_tts_speaker_boost_bool_spellings() -> None:
    """The speaker-boost knob accepts the common boolean spellings."""
    for raw in ("true", "1", "yes", "on", " True "):
        cfg = load_media_config({"HERMES_VOIP_TTS_SPEAKER_BOOST": raw})
        assert cfg.tts_speaker_boost is True
    for raw in ("false", "0", "no", "off"):
        cfg = load_media_config({"HERMES_VOIP_TTS_SPEAKER_BOOST": raw})
        assert cfg.tts_speaker_boost is False


def test_media_tts_speaker_boost_bad_bool_rejected() -> None:
    """A non-boolean speaker-boost value is rejected (fail-fast)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_TTS_SPEAKER_BOOST": "maybe"})


def test_media_tts_streaming_latency_bounds() -> None:
    """optimize_streaming_latency accepts ints in [0, 4]."""
    for value in (0, 1, 2, 3, 4):
        cfg = load_media_config({"HERMES_VOIP_TTS_STREAMING_LATENCY": str(value)})
        assert cfg.tts_streaming_latency == value


@pytest.mark.parametrize("bad", ["5", "-1", "fast", "1.5"])
def test_media_tts_streaming_latency_out_of_range_rejected(bad: str) -> None:
    """A streaming-latency value outside [0, 4] (or non-int) is rejected."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_TTS_STREAMING_LATENCY": bad})


def test_media_tts_tuning_validates_on_direct_construction() -> None:
    """An out-of-range tuning value fails in __post_init__, not only via the parser."""
    with pytest.raises(ConfigError):
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
            barge_in_min_speech_ms=400,
            barge_in_tail_ms=250,
            barge_in_fade_ms=30,
            injection_guard="onnx",
            injection_guard_model_dir=None,
            dtmf_mode="auto",
            dtmf_interdigit_ms=None,
            dtmf_inband_enabled=True,
            tone_secs=0.0,
            tts_stability=1.5,
        )
