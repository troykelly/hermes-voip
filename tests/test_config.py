"""Tests for the indexed ``HERMES_SIP_*`` gateway/extension config scheme (ADR-0011).

Parsing is a pure function of an env :class:`~collections.abc.Mapping`; no process
environment is read here. Fakes only (host ``pbx.example.test``, ext ``1000``).
"""

from __future__ import annotations

import dataclasses

import pytest

from hermes_voip.config import (
    DEFAULT_GREETING,
    DEFAULT_ICE_STUN_URLS,
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
    # ADR-0038: the WebSocket upgrade path defaults to /ws when unset.
    assert cfg.ws_path == "/ws"
    # No separate WSS password unset ⇒ None (the digest falls back to the SIP pw).
    assert cfg.ws_password is None


def test_ws_path_override() -> None:
    """ADR-0038: HERMES_SIP_WS_PATH overrides the default WebSocket upgrade path."""
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION="1000",
            HERMES_SIP_PASSWORD="x",
            HERMES_SIP_TRANSPORT="wss",
            HERMES_SIP_WS_PATH="/sip-ws",
        )
    )
    assert cfg.ws_path == "/sip-ws"


def test_ws_path_default_on_tls() -> None:
    """ws_path is parsed even on tls (harmless default; only WSS reads it)."""
    cfg = load_gateway_config(
        _base(HERMES_SIP_EXTENSION="1000", HERMES_SIP_PASSWORD="x")
    )
    assert cfg.ws_path == "/ws"


def test_ws_password_parsed_and_repr_suppressed() -> None:
    """ADR-0038: HERMES_SIP_WS_PASSWORD is read and NEVER appears in repr (a secret)."""
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION="1000",
            HERMES_SIP_PASSWORD="sip-pw",
            HERMES_SIP_TRANSPORT="wss",
            HERMES_SIP_WS_PASSWORD="wss-only-pw",
        )
    )
    assert cfg.ws_password == "wss-only-pw"
    # rule 34: the WSS password must never reach a log line — repr-suppressed.
    assert "wss-only-pw" not in repr(cfg)


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


# ---- provisioning-alias host/port keys (launch-blocker fix) ----------------
#
# The 1Password-provisioned .env emits HERMES_SIP_SERVER_HOST / HERMES_SIP_TLS_PORT,
# but the canonical keys are HERMES_SIP_HOST / HERMES_SIP_PORT. Accept the provisioner
# names as fallbacks so a first live launch from the sanctioned secret registers; the
# canonical names win when both are set (runbook 0001).


def test_server_host_alias_loads_when_canonical_host_unset() -> None:
    """A config with ONLY HERMES_SIP_SERVER_HOST (no HERMES_SIP_HOST) loads the host.

    This is the exact .env the 1Password provisioner writes — it must register, not
    fail with 'HERMES_SIP_HOST is required'.
    """
    cfg = load_gateway_config(
        {
            "HERMES_SIP_SERVER_HOST": "pbx.example.test",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "secret",
        }
    )
    assert cfg.host == "pbx.example.test"


def test_tls_port_alias_loads_when_canonical_port_unset() -> None:
    """A config with ONLY HERMES_SIP_TLS_PORT (no HERMES_SIP_PORT) loads the port."""
    cfg = load_gateway_config(
        {
            "HERMES_SIP_SERVER_HOST": "pbx.example.test",
            "HERMES_SIP_TLS_PORT": "5061",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "secret",
        }
    )
    assert cfg.port == 5061


def test_provisioner_env_shape_loads_host_and_port() -> None:
    """The full provisioner shape (SERVER_HOST + TLS_PORT, no canonical keys) loads."""
    cfg = load_gateway_config(
        {
            "HERMES_SIP_SERVER_HOST": "pbx.example.test",
            "HERMES_SIP_TLS_PORT": "5061",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "secret",
        }
    )
    assert cfg.host == "pbx.example.test"
    assert cfg.port == 5061


def test_canonical_host_wins_over_server_host_alias() -> None:
    """When both names are set, the canonical HERMES_SIP_HOST takes precedence."""
    cfg = load_gateway_config(
        {
            "HERMES_SIP_HOST": "canonical.example.test",
            "HERMES_SIP_SERVER_HOST": "alias.example.test",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "secret",
        }
    )
    assert cfg.host == "canonical.example.test"


def test_canonical_port_wins_over_tls_port_alias() -> None:
    """When both names are set, the canonical HERMES_SIP_PORT takes precedence."""
    cfg = load_gateway_config(
        {
            "HERMES_SIP_HOST": "pbx.example.test",
            "HERMES_SIP_PORT": "5070",
            "HERMES_SIP_TLS_PORT": "5061",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "secret",
        }
    )
    assert cfg.port == 5070


def test_blank_canonical_host_falls_back_to_server_host_alias() -> None:
    """A present-but-blank HERMES_SIP_HOST falls back to the alias (not 'required')."""
    cfg = load_gateway_config(
        {
            "HERMES_SIP_HOST": "   ",
            "HERMES_SIP_SERVER_HOST": "alias.example.test",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "secret",
        }
    )
    assert cfg.host == "alias.example.test"


def test_blank_canonical_port_falls_back_to_tls_port_alias() -> None:
    """A present-but-blank HERMES_SIP_PORT falls back to the TLS-port alias."""
    cfg = load_gateway_config(
        {
            "HERMES_SIP_HOST": "pbx.example.test",
            "HERMES_SIP_PORT": "  ",
            "HERMES_SIP_TLS_PORT": "5061",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "secret",
        }
    )
    assert cfg.port == 5061


def test_neither_host_name_set_rejected() -> None:
    """Neither HERMES_SIP_HOST nor HERMES_SIP_SERVER_HOST set => a clear ConfigError."""
    with pytest.raises(ConfigError, match="HERMES_SIP_HOST"):
        load_gateway_config(
            {"HERMES_SIP_EXTENSION": "1000", "HERMES_SIP_PASSWORD": "secret"}
        )


def test_tls_port_alias_out_of_range_rejected() -> None:
    """A malformed value via the TLS-port alias is validated like the canonical key."""
    with pytest.raises(ConfigError):
        load_gateway_config(
            {
                "HERMES_SIP_SERVER_HOST": "pbx.example.test",
                "HERMES_SIP_TLS_PORT": "70000",
                "HERMES_SIP_EXTENSION": "1000",
                "HERMES_SIP_PASSWORD": "secret",
            }
        )


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


def test_registration_config_wss_password_override() -> None:
    """ADR-0038: on wss with a WS password set, the digest uses the WS password."""
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION="1000",
            HERMES_SIP_PASSWORD="sip-pw",
            HERMES_SIP_TRANSPORT="wss",
            HERMES_SIP_WS_PASSWORD="wss-only-pw",
        )
    )
    ext = cfg.extensions[0]
    rc = cfg.registration_config(
        ext,
        contact="<sip:1000@aaa.invalid;transport=ws>",
        local_sent_by="aaa.invalid",
    )
    assert rc.transport == "WSS"
    # The WSS endpoint authenticates with the SEPARATE WSS credential, not the
    # per-extension SIP password.
    assert rc.password == "wss-only-pw"


def test_registration_config_wss_password_falls_back_to_sip_password() -> None:
    """ADR-0038: on wss with NO WS password, the digest falls back to the SIP pw."""
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION="1000",
            HERMES_SIP_PASSWORD="sip-pw",
            HERMES_SIP_TRANSPORT="wss",
        )
    )
    ext = cfg.extensions[0]
    rc = cfg.registration_config(
        ext,
        contact="<sip:1000@aaa.invalid;transport=ws>",
        local_sent_by="aaa.invalid",
    )
    assert rc.password == "sip-pw"


def test_registration_config_password_repr_suppressed() -> None:
    """ADR-0038: the digest password NEVER appears in a RegistrationConfig repr.

    registration_config() copies the SIP/WSS secret into RegistrationConfig.password,
    so that field must be repr-suppressed too or repr(rc) would leak it (rule 34).
    """
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION="1000",
            HERMES_SIP_PASSWORD="sip-secret-pw",
            HERMES_SIP_TRANSPORT="wss",
            HERMES_SIP_WS_PASSWORD="wss-secret-pw",
        )
    )
    rc = cfg.registration_config(
        cfg.extensions[0],
        contact="<sip:1000@aaa.invalid;transport=ws>",
        local_sent_by="aaa.invalid",
    )
    # The WSS secret was selected for the digest...
    assert rc.password == "wss-secret-pw"
    # ...but neither it nor the SIP password may reach a log line via repr.
    assert "wss-secret-pw" not in repr(rc)
    assert "sip-secret-pw" not in repr(rc)


def test_registration_config_ws_password_ignored_on_tls() -> None:
    """A stray WS password does NOT override the digest on a tls transport."""
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION="1000",
            HERMES_SIP_PASSWORD="sip-pw",
            HERMES_SIP_TRANSPORT="tls",
            HERMES_SIP_WS_PASSWORD="wss-only-pw",
        )
    )
    ext = cfg.extensions[0]
    rc = cfg.registration_config(
        ext,
        contact="<sip:1000@198.51.100.7:5061;transport=tls>",
        local_sent_by="198.51.100.7:5061",
    )
    assert rc.transport == "TLS"
    # On TLS the WSS password is irrelevant — the SIP password is used.
    assert rc.password == "sip-pw"


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
    assert cfg.barge_in_mode == "gated"
    # AEC-aware barge-in threshold (ADR-0033): with the in-process echo canceller ON
    # by default, the gateway's reflected TTS is cancelled before the VAD, so the
    # 600 ms echo-safety margin (ADR-0023) is unnecessary and the default drops to a
    # responsive 200 ms. (HERMES_VOIP_AEC_ENABLED=false restores 600 ms — see
    # test_config_aec.py.)
    assert cfg.barge_in_min_speech_ms == 200
    assert cfg.barge_in_tail_ms == 250
    # barge-in clean-stop fade (ADR-0028): a short click-free ramp on the cut.
    assert cfg.barge_in_fade_ms == 30
    # in-process acoustic echo cancellation (ADR-0033): ON by default with a 64 ms
    # NLMS filter (the window spans the realistic echo-return delay; the engine caps
    # the tap count for the per-frame CPU budget); this is what lets the barge-in
    # threshold above drop to 200 ms.
    assert cfg.aec_enabled is True
    assert cfg.aec_filter_ms == 64
    assert cfg.aec_bulk_delay_ms == 0
    assert cfg.aec_mu == pytest.approx(0.30)
    # dead-air comfort filler (ADR-0030, extended ADR-0054): ON by default now —
    # the operator wants a slow turn to never leave the caller in silence. The delay
    # is the dead-air threshold AND the periodic repeat interval; the phrases default
    # to the selected language's built-in set (English here, the default language).
    assert cfg.comfort_filler is True
    assert cfg.comfort_filler_delay_ms == 900
    assert cfg.comfort_filler_repeat_ms == 900
    assert cfg.language == "en"
    # The English default set is richer than the original three (random, no-immediate
    # -repeat selection wears better with more variety); these members must be present.
    assert "One moment please." in cfg.comfort_filler_phrases
    assert "Just a moment." in cfg.comfort_filler_phrases
    assert all(p.strip() for p in cfg.comfort_filler_phrases)
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


def test_media_ice_stun_urls_default_public() -> None:
    """No STUN config => the default public IPv6-capable STUN list (ADR-0043).

    Operator-directed (2026-06-18): a NAT'd deployment must gather a
    server-reflexive candidate out of the box, so the default is a small list of
    public dual-stack STUN servers (overridable; an explicit empty value disables).
    """
    cfg = load_media_config({})
    assert cfg.ice_stun_urls == DEFAULT_ICE_STUN_URLS
    assert len(cfg.ice_stun_urls) >= 1


def test_media_ice_stun_urls_explicit_empty_disables() -> None:
    """An explicit empty HERMES_VOIP_ICE_STUN_URLS disables STUN (host-only ICE)."""
    cfg = load_media_config({"HERMES_VOIP_ICE_STUN_URLS": ""})
    assert cfg.ice_stun_urls == ()


def test_media_video_source_default_off() -> None:
    """No video config => no outbound video source, default 10 fps (ADR-0044)."""
    cfg = load_media_config({})
    assert cfg.video_source_path is None
    assert cfg.video_fps == 10


def test_media_video_source_and_fps_parsed() -> None:
    """HERMES_VOIP_VIDEO_SOURCE_PATH / _FPS are read into the media config."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_VIDEO_SOURCE_PATH": "/srv/clip.h264",
            "HERMES_VOIP_VIDEO_FPS": "15",
        }
    )
    assert cfg.video_source_path == "/srv/clip.h264"
    assert cfg.video_fps == 15


def test_media_video_fps_out_of_range_rejected() -> None:
    """An out-of-range HERMES_VOIP_VIDEO_FPS is a ConfigError (1..60)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_VIDEO_FPS": "0"})


def test_media_ice_stun_urls_parsed_comma_separated() -> None:
    """HERMES_VOIP_ICE_STUN_URLS is a comma-separated stun: URL list (trimmed)."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_ICE_STUN_URLS": (
                "stun:stun.example.test:3478, stun:stun2.example.test:19302"
            )
        }
    )
    assert cfg.ice_stun_urls == (
        "stun:stun.example.test:3478",
        "stun:stun2.example.test:19302",
    )


def test_media_ice_stun_urls_blank_members_dropped() -> None:
    """Blank entries between commas are dropped; an all-blank value => empty."""
    cfg = load_media_config({"HERMES_VOIP_ICE_STUN_URLS": " , ,"})
    assert cfg.ice_stun_urls == ()


def test_media_ice_use_ipv6_default_true() -> None:
    """IPv6-first (ADR-0043): IPv6 ICE gathering is ON by default."""
    cfg = load_media_config({})
    assert cfg.ice_use_ipv6 is True


def test_media_ice_use_ipv4_default_true() -> None:
    """IPv4 stays gathered as the fallback family by default (ADR-0043)."""
    cfg = load_media_config({})
    assert cfg.ice_use_ipv4 is True


def test_media_ice_address_families_overridable() -> None:
    """Either address family can be disabled via env (e.g. IPv6-only deployment)."""
    cfg = load_media_config(
        {"HERMES_VOIP_ICE_USE_IPV4": "false", "HERMES_VOIP_ICE_USE_IPV6": "true"}
    )
    assert cfg.ice_use_ipv4 is False
    assert cfg.ice_use_ipv6 is True


# --- WebRTC DTLS answerer role knob (ADR-0050) ---


def test_media_webrtc_dtls_setup_default_auto() -> None:
    """No knob => ``auto`` (the RFC-8842 active-answerer default, ADR-0050)."""
    cfg = load_media_config({})
    assert cfg.webrtc_dtls_setup == "auto"


def test_media_webrtc_dtls_setup_forced_active() -> None:
    """HERMES_VOIP_WEBRTC_DTLS_SETUP=active forces the active answerer role."""
    cfg = load_media_config({"HERMES_VOIP_WEBRTC_DTLS_SETUP": "active"})
    assert cfg.webrtc_dtls_setup == "active"


def test_media_webrtc_dtls_setup_forced_passive() -> None:
    """HERMES_VOIP_WEBRTC_DTLS_SETUP=passive forces the passive (server) role."""
    cfg = load_media_config({"HERMES_VOIP_WEBRTC_DTLS_SETUP": "passive"})
    assert cfg.webrtc_dtls_setup == "passive"


def test_media_webrtc_dtls_setup_is_case_insensitive() -> None:
    """The knob value is normalised (case-insensitive), e.g. ``PASSIVE``."""
    cfg = load_media_config({"HERMES_VOIP_WEBRTC_DTLS_SETUP": "PASSIVE"})
    assert cfg.webrtc_dtls_setup == "passive"


def test_media_webrtc_dtls_setup_rejects_unknown() -> None:
    """An unrecognised value is rejected loudly (rule 27 — no inert knob)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_WEBRTC_DTLS_SETUP": "actpass"})


# --- SIP DTLS-SRTP activation knobs (ADR-0053 Stage 2 §6) ---


def test_media_sip_dtls_srtp_default_on() -> None:
    """No knob => DTLS-SRTP answering is ON (the opportunistic preferred tier)."""
    cfg = load_media_config({})
    assert cfg.sip_dtls_srtp is True


def test_media_sip_dtls_srtp_disabled() -> None:
    """HERMES_VOIP_SIP_DTLS_SRTP=false is the rollback switch (off)."""
    cfg = load_media_config({"HERMES_VOIP_SIP_DTLS_SRTP": "false"})
    assert cfg.sip_dtls_srtp is False


def test_media_sip_dtls_setup_default_auto() -> None:
    """No knob => ``auto`` (the RFC-8842 active-answerer default, mirroring WebRTC)."""
    cfg = load_media_config({})
    assert cfg.sip_dtls_setup == "auto"


def test_media_sip_dtls_setup_forced_active() -> None:
    """HERMES_VOIP_SIP_DTLS_SETUP=active forces the active (DTLS client) role."""
    cfg = load_media_config({"HERMES_VOIP_SIP_DTLS_SETUP": "active"})
    assert cfg.sip_dtls_setup == "active"


# --- Outbound SDES-SRTP offering knob (ADR-0067) ---


def test_media_sip_sdes_offer_default_off() -> None:
    """No knob => the outbound INVITE offers PLAIN RTP/AVP (opt-in, ADR-0067).

    Default-off preserves today's live-validated cleartext outbound offer; turning
    SDES offering on is the operator's explicit opt-in (the fail-closed policy would
    otherwise fail any non-SRTP terminating leg).
    """
    cfg = load_media_config({})
    assert cfg.sip_sdes_offer is False


def test_media_sip_sdes_offer_enabled() -> None:
    """HERMES_VOIP_SIP_SDES_OFFER=true makes the outbound INVITE offer RTP/SAVP."""
    cfg = load_media_config({"HERMES_VOIP_SIP_SDES_OFFER": "true"})
    assert cfg.sip_sdes_offer is True


# --- Secure-media mandate knob (ADR-0070) ---


def test_media_require_secure_media_default_on() -> None:
    """No knob => the secure-media mandate is ON (cleartext RTP/AVP is 488'd).

    Signalling is already TLS/WSS, so the inbound answer path defaults to rejecting
    a cleartext media offer rather than answering it (ADR-0070).
    """
    cfg = load_media_config({})
    assert cfg.require_secure_media is True


def test_media_require_secure_media_disabled() -> None:
    """HERMES_VOIP_REQUIRE_SECURE_MEDIA=false rolls back to opportunistic plaintext."""
    cfg = load_media_config({"HERMES_VOIP_REQUIRE_SECURE_MEDIA": "false"})
    assert cfg.require_secure_media is False


def test_media_sip_dtls_setup_forced_passive() -> None:
    """HERMES_VOIP_SIP_DTLS_SETUP=passive forces the passive (DTLS server) role."""
    cfg = load_media_config({"HERMES_VOIP_SIP_DTLS_SETUP": "passive"})
    assert cfg.sip_dtls_setup == "passive"


def test_media_sip_dtls_setup_is_case_insensitive() -> None:
    """The knob value is normalised (case-insensitive), e.g. ``PASSIVE``."""
    cfg = load_media_config({"HERMES_VOIP_SIP_DTLS_SETUP": "PASSIVE"})
    assert cfg.sip_dtls_setup == "passive"


def test_media_sip_dtls_setup_rejects_unknown() -> None:
    """An unrecognised value is rejected loudly (rule 27 — no inert knob)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_SIP_DTLS_SETUP": "actpass"})


def test_media_sip_dtls_setup_independent_of_webrtc() -> None:
    """The SIP-DTLS role knob is independent of the WebRTC one (separate gateways)."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_SIP_DTLS_SETUP": "passive",
            "HERMES_VOIP_WEBRTC_DTLS_SETUP": "active",
        }
    )
    assert cfg.sip_dtls_setup == "passive"
    assert cfg.webrtc_dtls_setup == "active"


# --- TURN relay config (ADR-0034) ---


def test_media_ice_turn_default_empty() -> None:
    """No TURN config => no relay candidate (empty URLs, no creds) — ADR-0034."""
    cfg = load_media_config({})
    assert cfg.ice_turn_urls == ()
    assert cfg.ice_turn_username is None
    assert cfg.ice_turn_password is None


def test_media_ice_turn_urls_parsed_with_credentials() -> None:
    """HERMES_VOIP_ICE_TURN_URLS + username + password parse into MediaConfig."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_ICE_TURN_URLS": (
                "turn:turn.example.test:3478, turns:turn.example.test:5349"
            ),
            "HERMES_VOIP_ICE_TURN_USERNAME": "relay-user",
            "HERMES_VOIP_ICE_TURN_PASSWORD": "relay-secret",
        }
    )
    assert cfg.ice_turn_urls == (
        "turn:turn.example.test:3478",
        "turns:turn.example.test:5349",
    )
    assert cfg.ice_turn_username == "relay-user"
    assert cfg.ice_turn_password == "relay-secret"


def test_media_ice_turn_blank_members_dropped() -> None:
    """Blank entries between commas are dropped (same parser as STUN)."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_ICE_TURN_URLS": "turn:turn.example.test:3478, ,",
            "HERMES_VOIP_ICE_TURN_USERNAME": "u",
            "HERMES_VOIP_ICE_TURN_PASSWORD": "p",
        }
    )
    assert cfg.ice_turn_urls == ("turn:turn.example.test:3478",)


def test_media_ice_turn_urls_without_username_is_config_error() -> None:
    """TURN URLs set but no username => loud ConfigError (RFC 8656 needs creds)."""
    with pytest.raises(ConfigError, match="TURN"):
        load_media_config(
            {
                "HERMES_VOIP_ICE_TURN_URLS": "turn:turn.example.test:3478",
                "HERMES_VOIP_ICE_TURN_PASSWORD": "p",
            }
        )


def test_media_ice_turn_urls_without_password_is_config_error() -> None:
    """TURN URLs set but no password => loud ConfigError (no silent no-op)."""
    with pytest.raises(ConfigError, match="TURN"):
        load_media_config(
            {
                "HERMES_VOIP_ICE_TURN_URLS": "turn:turn.example.test:3478",
                "HERMES_VOIP_ICE_TURN_USERNAME": "u",
            }
        )


def test_media_ice_turn_credentials_without_urls_ok() -> None:
    """Creds present but no URLs is harmless (no relay gathered); not an error."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_ICE_TURN_USERNAME": "u",
            "HERMES_VOIP_ICE_TURN_PASSWORD": "p",
        }
    )
    assert cfg.ice_turn_urls == ()


def test_media_ice_turn_password_suppressed_from_repr() -> None:
    """The TURN password must never reach a log line / traceback (repr=False)."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_ICE_TURN_URLS": "turn:turn.example.test:3478",
            "HERMES_VOIP_ICE_TURN_USERNAME": "u",
            "HERMES_VOIP_ICE_TURN_PASSWORD": "super-secret-value",
        }
    )
    assert "super-secret-value" not in repr(cfg)


def test_media_barge_in_fade_ms_zero_allowed() -> None:
    """A fade of 0 ms is valid (instant hard cut, no ramp)."""
    cfg = load_media_config({"HERMES_VOIP_BARGE_IN_FADE_MS": "0"})
    assert cfg.barge_in_fade_ms == 0


def test_media_barge_in_fade_ms_negative_rejected() -> None:
    """A negative fade is rejected (fail-fast)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_BARGE_IN_FADE_MS": "-5"})


def test_media_comfort_filler_on_and_overrides() -> None:
    """The comfort filler is opt-in; delay + phrase set are overridable (ADR-0030)."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_TTS_COMFORT_FILLER": "true",
            "HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS": "1200",
            "HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES": "uh,|let me check,|hold on,",
        }
    )
    assert cfg.comfort_filler is True
    assert cfg.comfort_filler_delay_ms == 1200
    assert cfg.comfort_filler_phrases == ("uh,", "let me check,", "hold on,")


def test_media_comfort_filler_default_on_with_default_delay_and_phrases() -> None:
    """Unset → ON (ADR-0054), with the default delay/repeat and English phrase set."""
    cfg = load_media_config({})
    assert cfg.comfort_filler is True
    assert cfg.comfort_filler_delay_ms == 900
    assert cfg.comfort_filler_repeat_ms == 900
    assert cfg.language == "en"
    assert "One moment please." in cfg.comfort_filler_phrases


def test_media_comfort_filler_can_be_disabled() -> None:
    """The operator can still turn the filler OFF explicitly (per ADR-0054)."""
    cfg = load_media_config({"HERMES_VOIP_TTS_COMFORT_FILLER": "false"})
    assert cfg.comfort_filler is False


def test_media_comfort_filler_repeat_ms_override_and_validation() -> None:
    """The periodic repeat interval is overridable and must be positive (ADR-0054)."""
    cfg = load_media_config({"HERMES_VOIP_TTS_COMFORT_FILLER_REPEAT_MS": "1500"})
    assert cfg.comfort_filler_repeat_ms == 1500
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_TTS_COMFORT_FILLER_REPEAT_MS": "0"})


def test_media_language_selects_phrase_set_and_is_validated() -> None:
    """HERMES_VOIP_LANGUAGE selects the phrase set; an unknown code fails (ADR-0054)."""
    cfg = load_media_config({"HERMES_VOIP_LANGUAGE": "EN"})  # case-insensitive
    assert cfg.language == "en"
    assert "One moment please." in cfg.comfort_filler_phrases
    with pytest.raises(ConfigError, match="HERMES_VOIP_LANGUAGE"):
        load_media_config({"HERMES_VOIP_LANGUAGE": "zz"})


def test_media_comfort_filler_explicit_phrases_override_language_default() -> None:
    """An explicit phrase set wins over the language's built-in default (ADR-0054)."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_LANGUAGE": "en",
            "HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES": "uh,|hold on,",
        }
    )
    assert cfg.comfort_filler_phrases == ("uh,", "hold on,")


def test_media_comfort_filler_blank_phrases_fall_back_to_language_default() -> None:
    """A blank phrase override collapses to the language's built-in set, not empty."""
    cfg = load_media_config(
        {
            "HERMES_VOIP_TTS_COMFORT_FILLER": "on",
            "HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES": "   ",
        }
    )
    assert "One moment please." in cfg.comfort_filler_phrases
    assert all(p.strip() for p in cfg.comfort_filler_phrases)


def test_media_comfort_filler_phrases_trims_and_drops_empty_members() -> None:
    """Each phrase is trimmed; empty members (e.g. a trailing ``|``) are dropped."""
    cfg = load_media_config(
        {"HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES": "  hmm,  | | let me see, |"}
    )
    assert cfg.comfort_filler_phrases == ("hmm,", "let me see,")


def test_media_comfort_filler_delay_ms_must_be_positive() -> None:
    """A non-positive comfort-filler delay is rejected (fail-fast)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS": "0"})


def test_media_comfort_filler_delay_ms_malformed_rejected() -> None:
    """A malformed (non-integer) delay is rejected (fail-fast)."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS": "soon"})


def test_media_comfort_filler_bad_boolean_rejected() -> None:
    """An unrecognised boolean spelling for the master switch is rejected."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_TTS_COMFORT_FILLER": "maybe"})


def test_mediaconfig_direct_blank_comfort_phrase_rejected() -> None:
    """A directly-constructed MediaConfig with a blank phrase fails fast (post-init).

    The env parser normalises a blank `|`-list to the default set, but a caller that
    constructs MediaConfig directly must not be able to smuggle in a blank phrase —
    a filler with nothing to say is a silent no-op. ``__post_init__`` rejects it.
    """
    base = load_media_config({})
    with pytest.raises(ConfigError, match="comfort_filler_phrases"):
        dataclasses.replace(base, comfort_filler_phrases=("Hmm,", "   "))


def test_mediaconfig_direct_empty_comfort_phrases_rejected() -> None:
    """A directly-constructed MediaConfig with an empty phrase tuple fails fast."""
    base = load_media_config({})
    with pytest.raises(ConfigError, match="comfort_filler_phrases"):
        dataclasses.replace(base, comfort_filler_phrases=())


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
            # A DTMF mode (rfc4733) with surrounding whitespace — trims to the bare
            # token (all four modes load now, ADR-0036; this asserts the trim).
            "HERMES_SIP_DTMF_MODE": "  rfc4733  ",
        }
    )
    assert cfg.stt_provider == "deepgram"
    assert cfg.tts_voice == "rachel"
    assert cfg.vad_threshold == pytest.approx(0.3)
    assert cfg.dtmf_mode == "rfc4733"


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


def test_media_supported_dtmf_modes_accepted() -> None:
    """All four ADR-0010 DTMF modes now load and round-trip (ADR-0036).

    SIP INFO and in-band (send AND receive) are shipped, so ``sip_info`` / ``inband``
    are no longer rejected at load (that rejection was the interim fail-loud state while
    those backends were deferred). The per-call backend is resolved from the mode +
    negotiation in ``hermes_voip.dtmf_config`` (matrix in test_dtmf_mode_resolution.py);
    only an unknown mode is rejected (``test_media_unknown_dtmf_mode_rejected``).
    """
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


def test_extension_config_password_absent_from_repr() -> None:
    """Rule 34: the SIP digest password must NEVER reach a log line via repr.

    ``ExtensionConfig.password`` is a secret (the SIP-TLS digest credential), so it is
    repr-suppressed like every sibling secret — a traceback or config-dump that renders
    an ExtensionConfig must not print the plaintext password.
    """
    ext = ExtensionConfig(
        index=0, extension="1000", username="1000", password="super-secret-pw"
    )
    assert "super-secret-pw" not in repr(ext)
    # the value is still accessible by reference for the digest to use
    assert ext.password == "super-secret-pw"


def test_gateway_config_password_absent_from_repr() -> None:
    """Rule 34: repr(GatewayConfig) renders its extensions tuple — no password leaks.

    GatewayConfig's repr includes ``extensions``, so an un-suppressed per-extension
    password would surface here in any traceback/config-dump. The whole config repr
    must be free of the plaintext digest secret.
    """
    cfg = load_gateway_config(
        _base(HERMES_SIP_EXTENSION="1000", HERMES_SIP_PASSWORD="super-secret-pw")
    )
    assert "super-secret-pw" not in repr(cfg)


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


# ---- admission cap + shutdown drain (ADR-0059) -----------------------------


def test_max_calls_default() -> None:
    """The concurrent-call cap defaults to a sane positive value when unset."""
    cfg = load_gateway_config(
        _base(HERMES_SIP_EXTENSION="1000", HERMES_SIP_PASSWORD="x")
    )
    assert cfg.max_calls == 8


def test_max_calls_override() -> None:
    """HERMES_SIP_MAX_CALLS sets the concurrent-call cap."""
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION="1000",
            HERMES_SIP_PASSWORD="x",
            HERMES_SIP_MAX_CALLS="3",
        )
    )
    assert cfg.max_calls == 3


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "1.5"])
def test_max_calls_rejects_non_positive_or_malformed(bad: str) -> None:
    """A non-positive / malformed cap is rejected fail-fast (rule 37)."""
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(
                HERMES_SIP_EXTENSION="1000",
                HERMES_SIP_PASSWORD="x",
                HERMES_SIP_MAX_CALLS=bad,
            )
        )


def test_shutdown_drain_secs_default() -> None:
    """The shutdown-drain timeout defaults to a sane positive value when unset."""
    cfg = load_gateway_config(
        _base(HERMES_SIP_EXTENSION="1000", HERMES_SIP_PASSWORD="x")
    )
    assert cfg.shutdown_drain_secs == 5.0


def test_shutdown_drain_secs_override() -> None:
    """HERMES_SIP_SHUTDOWN_DRAIN_SECS sets the bounded drain timeout (seconds)."""
    cfg = load_gateway_config(
        _base(
            HERMES_SIP_EXTENSION="1000",
            HERMES_SIP_PASSWORD="x",
            HERMES_SIP_SHUTDOWN_DRAIN_SECS="12.5",
        )
    )
    assert cfg.shutdown_drain_secs == 12.5


@pytest.mark.parametrize("bad", ["0", "-2", "abc", "nan", "inf"])
def test_shutdown_drain_secs_rejects_non_positive_or_malformed(bad: str) -> None:
    """A non-positive / non-finite / malformed drain timeout is rejected."""
    with pytest.raises(ConfigError):
        load_gateway_config(
            _base(
                HERMES_SIP_EXTENSION="1000",
                HERMES_SIP_PASSWORD="x",
                HERMES_SIP_SHUTDOWN_DRAIN_SECS=bad,
            )
        )


# ---- adaptive jitter buffer ceiling (ADR-0063) -----------------------------


def test_jitter_max_depth_default() -> None:
    """The adaptive-jitter ceiling defaults to a sane value (ADR-0063).

    The adapter constructs the media engine's :class:`JitterBuffer` with
    ``adapt=True`` and this value as the ceiling, so a default install gets the
    launch-promoted adaptive reorder tolerance without any env tuning.
    """
    cfg = load_media_config({})
    assert cfg.jitter_max_depth == 10


def test_jitter_max_depth_override() -> None:
    """HERMES_VOIP_JITTER_MAX_DEPTH sets the adaptive-jitter ceiling."""
    cfg = load_media_config({"HERMES_VOIP_JITTER_MAX_DEPTH": "16"})
    assert cfg.jitter_max_depth == 16


@pytest.mark.parametrize("bad", ["0", "-3", "abc"])
def test_jitter_max_depth_rejects_non_positive_or_malformed(bad: str) -> None:
    """A non-positive / malformed adaptive-jitter ceiling is rejected at load."""
    with pytest.raises(ConfigError):
        load_media_config({"HERMES_VOIP_JITTER_MAX_DEPTH": bad})


def test_jitter_max_depth_below_engine_floor_rejected() -> None:
    """A ceiling below the engine's fixed jitter floor (2) is rejected (ADR-0063).

    Codex review BLOCKING: the adapter builds the media engine with the default
    jitter_depth=2 floor and adapt=True; a ceiling of 1 would make
    JitterBuffer(adapt=True, max_depth=1, target_depth=2) raise at engine
    construction (max_depth must be >= target_depth). A "documented-valid" positive
    value must not crash the call — reject it loudly at config load instead.
    """
    with pytest.raises(ConfigError, match="jitter_max_depth"):
        load_media_config({"HERMES_VOIP_JITTER_MAX_DEPTH": "1"})


def test_jitter_max_depth_at_floor_is_accepted() -> None:
    """A ceiling equal to the floor (2) is the minimum valid value."""
    cfg = load_media_config({"HERMES_VOIP_JITTER_MAX_DEPTH": "2"})
    assert cfg.jitter_max_depth == 2


def test_jitter_max_depth_must_be_positive_on_direct_construction() -> None:
    """A directly-constructed MediaConfig validates the ceiling itself."""
    with pytest.raises(ConfigError, match="jitter_max_depth"):
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
            jitter_max_depth=0,
        )
