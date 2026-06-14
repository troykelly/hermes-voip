"""Tests for the indexed ``HERMES_SIP_*`` gateway/extension config scheme (ADR-0011).

Parsing is a pure function of an env :class:`~collections.abc.Mapping`; no process
environment is read here. Fakes only (host ``pbx.example.test``, ext ``1000``).
"""

from __future__ import annotations

import pytest

from hermes_voip.config import (
    ConfigError,
    ExtensionConfig,
    GatewayConfig,
    load_gateway_config,
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
