"""Tests for the MULTI-intercom opening sets (ADR-0045, issue #38).

Multiple intercom caller-IDs, each with a NAMED set of openings (door / gate /
garage / …). Each opening actuates via EITHER a DTMF code OR a WebHook (GET or
POST with operator-settable headers + body). Config is a JSON document referenced
by ``HERMES_VOIP_INTERCOM_CONFIG_FILE`` mapping caller-id -> openings.

Security posture (ADR-0045): a webhook url / headers / body may carry secrets, so
they are repr-suppressed on the dataclass and never logged; an opening is scoped to
the calling intercom's set, so opening a name not in that set is rejected.

PUBLIC repo: obvious fakes only (``pbx.example.test``, ext ``1000``,
``relay.example.test``).
"""

from __future__ import annotations

import asyncio
import json
import traceback
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_voip.config import ConfigError
from hermes_voip.multi_intercom import (
    MultiIntercomConfig,
    Opening,
    OpeningType,
    WebhookError,
    fire_webhook_opening,
    load_multi_intercom_config,
)

_FAKE_WEBHOOK_URL = "https://relay.example.test/open"
_FAKE_HEADER_TOKEN = "fake-bearer-token-0000"  # obvious fake
_FAKE_BODY = '{"action":"open"}'


def _write(tmp_path: Path, document: object) -> str:
    path = tmp_path / "intercom.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def _env(**kw: str) -> Mapping[str, str]:
    return dict(kw)


# --- default: unconfigured ----------------------------------------------------


def test_default_is_empty() -> None:
    cfg = load_multi_intercom_config(_env())
    assert cfg.entries == ()
    assert cfg.match("1000") is None


def test_configured_but_missing_file_fails_loud() -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_multi_intercom_config(
            _env(HERMES_VOIP_INTERCOM_CONFIG_FILE="/no/such/intercom.json")
        )


# --- loading a multi-intercom document ----------------------------------------


def _doc() -> dict[str, object]:
    return {
        "intercoms": {
            "1000": {
                "openings": {
                    "door": {"type": "dtmf", "dtmf_code": "9"},
                    "gate": {
                        "type": "webhook",
                        "method": "POST",
                        "url": _FAKE_WEBHOOK_URL,
                        "headers": {"Authorization": f"Bearer {_FAKE_HEADER_TOKEN}"},
                        "body": _FAKE_BODY,
                    },
                }
            },
            "1001": {
                "openings": {
                    "garage": {
                        "type": "webhook",
                        "method": "GET",
                        "url": _FAKE_WEBHOOK_URL,
                    }
                }
            },
        }
    }


def test_loads_multiple_intercoms_with_named_openings(tmp_path: Path) -> None:
    path = _write(tmp_path, _doc())
    cfg = load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))
    assert isinstance(cfg, MultiIntercomConfig)
    entry = cfg.match("1000")
    assert entry is not None
    assert set(entry.openings) == {"door", "gate"}
    door = entry.openings["door"]
    assert door.type is OpeningType.DTMF
    assert door.dtmf_code == "9"
    gate = entry.openings["gate"]
    assert gate.type is OpeningType.WEBHOOK
    assert gate.method == "POST"
    assert gate.url == _FAKE_WEBHOOK_URL
    assert gate.headers["Authorization"] == f"Bearer {_FAKE_HEADER_TOKEN}"
    assert gate.body == _FAKE_BODY


def test_opening_names_does_not_expose_secrets(tmp_path: Path) -> None:
    path = _write(tmp_path, _doc())
    cfg = load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))
    entry = cfg.match("1000")
    assert entry is not None
    # The SURFACE to the agent is the opening NAMES, never the codes / urls / tokens.
    assert sorted(entry.opening_names()) == ["door", "gate"]


def test_second_intercom_has_its_own_set(tmp_path: Path) -> None:
    path = _write(tmp_path, _doc())
    cfg = load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))
    entry = cfg.match("1001")
    assert entry is not None
    assert sorted(entry.opening_names()) == ["garage"]
    # 1000's "door" is NOT in 1001's set.
    assert "door" not in entry.openings


# --- secret suppression (repr) ------------------------------------------------


def test_webhook_secrets_suppressed_in_repr() -> None:
    opening = Opening(
        name="gate",
        type=OpeningType.WEBHOOK,
        method="POST",
        url=_FAKE_WEBHOOK_URL,
        headers={"Authorization": f"Bearer {_FAKE_HEADER_TOKEN}"},
        body=_FAKE_BODY,
    )
    text = repr(opening)
    assert _FAKE_HEADER_TOKEN not in text
    assert _FAKE_WEBHOOK_URL not in text
    assert _FAKE_BODY not in text
    # The name + type are NOT secret and are useful in a log.
    assert "gate" in text


def test_dtmf_code_suppressed_in_repr() -> None:
    opening = Opening(name="door", type=OpeningType.DTMF, dtmf_code="9")
    assert "9" not in repr(opening)
    assert "door" in repr(opening)


# --- validation (fail-loud, rule 37) ------------------------------------------


def test_dtmf_opening_requires_code(tmp_path: Path) -> None:
    doc = {"intercoms": {"1000": {"openings": {"door": {"type": "dtmf"}}}}}
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="dtmf_code"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_dtmf_opening_rejects_invalid_code(tmp_path: Path) -> None:
    doc = {
        "intercoms": {
            "1000": {"openings": {"door": {"type": "dtmf", "dtmf_code": "xx"}}}
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_webhook_opening_requires_url(tmp_path: Path) -> None:
    doc = {"intercoms": {"1000": {"openings": {"gate": {"type": "webhook"}}}}}
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="url"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_webhook_opening_rejects_non_https_url(tmp_path: Path) -> None:
    doc = {
        "intercoms": {
            "1000": {
                "openings": {
                    "gate": {"type": "webhook", "url": "http://relay.example.test/open"}
                }
            }
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="https"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_webhook_opening_rejects_bad_method(tmp_path: Path) -> None:
    doc = {
        "intercoms": {
            "1000": {
                "openings": {
                    "gate": {
                        "type": "webhook",
                        "method": "DELETE",
                        "url": _FAKE_WEBHOOK_URL,
                    }
                }
            }
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="method"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_webhook_get_with_body_rejected_at_load(tmp_path: Path) -> None:
    """A GET opening with a configured body fails LOUD at load (rule 37).

    A GET carries no body on the wire; silently dropping a configured body would
    mask an operator misconfiguration. The mismatch is rejected at load, not at
    door-open time.
    """
    doc = {
        "intercoms": {
            "1000": {
                "openings": {
                    "gate": {
                        "type": "webhook",
                        "method": "GET",
                        "url": _FAKE_WEBHOOK_URL,
                        "body": _FAKE_BODY,
                    }
                }
            }
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="GET"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_unknown_opening_type_rejected(tmp_path: Path) -> None:
    doc = {"intercoms": {"1000": {"openings": {"door": {"type": "banana"}}}}}
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="type"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_intercom_with_no_openings_rejected(tmp_path: Path) -> None:
    doc: dict[str, object] = {"intercoms": {"1000": {"openings": {}}}}
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="opening"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_document_must_be_object(tmp_path: Path) -> None:
    path = _write(tmp_path, ["not", "an", "object"])
    with pytest.raises(ConfigError, match="object"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_invalid_json_fails_loud(tmp_path: Path) -> None:
    path = tmp_path / "intercom.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=str(path)))


# --- control-char rejection in headers at config load (CRLF/NUL hardening) ----


def test_header_name_with_crlf_rejected_at_load(tmp_path: Path) -> None:
    """A webhook header NAME containing CRLF is rejected at config load (ConfigError).

    http.client raises ValueError('Invalid header name') when a header name
    contains control chars, at the point the HTTP request is sent. That ValueError
    bypasses the existing except chain (HTTPError, URLError, TimeoutError, OSError)
    in _fire_webhook_blocking, propagating uncaught and violating the WebhookError
    contract. Reject the bad value at config load.
    """
    doc = {
        "intercoms": {
            "1000": {
                "openings": {
                    "gate": {
                        "type": "webhook",
                        "url": _FAKE_WEBHOOK_URL,
                        "headers": {"X-Bad\r\nHeader": "value"},
                    }
                }
            }
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="control"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_header_value_with_crlf_rejected_at_load(tmp_path: Path) -> None:
    """A webhook header VALUE containing CRLF is rejected at config load."""
    doc = {
        "intercoms": {
            "1000": {
                "openings": {
                    "gate": {
                        "type": "webhook",
                        "url": _FAKE_WEBHOOK_URL,
                        "headers": {"Authorization": "Bearer tok\r\nen"},
                    }
                }
            }
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="control"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_header_value_with_nul_rejected_at_load(tmp_path: Path) -> None:
    """A webhook header VALUE containing a NUL byte is rejected at config load."""
    doc = {
        "intercoms": {
            "1000": {
                "openings": {
                    "gate": {
                        "type": "webhook",
                        "url": _FAKE_WEBHOOK_URL,
                        "headers": {"Authorization": "Bearer tok\x00en"},
                    }
                }
            }
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="control"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_header_name_with_nul_rejected_at_load(tmp_path: Path) -> None:
    """A webhook header NAME containing a NUL byte is rejected at config load."""
    doc = {
        "intercoms": {
            "1000": {
                "openings": {
                    "gate": {
                        "type": "webhook",
                        "url": _FAKE_WEBHOOK_URL,
                        "headers": {"X-Bad\x00Header": "value"},
                    }
                }
            }
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="control"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


# --- ValueError in _fire_webhook_blocking surfaces as WebhookError -------------


def test_value_error_in_fire_webhook_surfaces_as_webhook_error() -> None:
    """A ValueError raised inside _fire_webhook_blocking is re-raised as WebhookError.

    http.client raises ValueError('Invalid header value') when a header contains
    control chars. If such a value slips through to the network call (defense-in-
    depth), the existing except chain (HTTPError, URLError, TimeoutError, OSError)
    does NOT catch ValueError — it propagates uncaught, violating the documented
    WebhookError contract. The fix wraps ValueError in WebhookError.
    """
    opening = Opening(
        name="gate",
        type=OpeningType.WEBHOOK,
        url=_FAKE_WEBHOOK_URL,
        headers={"Authorization": f"Bearer {_FAKE_HEADER_TOKEN}"},
    )

    def _raise_value_error(_opening: Opening) -> None:
        raise ValueError("Invalid header value")

    with (
        patch(
            "hermes_voip.multi_intercom._fire_webhook_blocking",
            side_effect=_raise_value_error,
        ),
        pytest.raises(WebhookError),
    ):
        asyncio.run(fire_webhook_opening(opening))


# --- the wrapped ValueError MUST NOT leak the offending header value -----------


def test_webhook_error_from_value_error_does_not_leak_secret() -> None:
    """The WebhookError wrapping a ValueError must NOT echo the secret.

    Python's http.client puts the OFFENDING HEADER VALUE into the ValueError text
    (e.g. ``Invalid header value b'Bearer <token>...'``). A webhook header may be
    ``Authorization: Bearer <token>`` — interpolating the raw exception text into
    the user-facing WebhookError message would leak the bearer token into
    logs/callers (PUBLIC-repo invariant violation). The wrapper must use a FIXED,
    generic message carrying NO interpolated exception text; and the secret-bearing
    ValueError cause MUST be suppressed (``from None``) so it cannot resurface in a
    printed traceback (``__cause__ is None``, ``__suppress_context__`` True).
    """
    secret = "SECRET-fake-bearer-token-0000"  # obvious fake
    opening = Opening(
        name="gate",
        type=OpeningType.WEBHOOK,
        url=_FAKE_WEBHOOK_URL,
        headers={"Authorization": f"Bearer {secret}"},
    )

    def _raise_value_error_with_secret(_opening: Opening) -> None:
        # Mirrors http.client: the whole offending header value is in the message.
        raise ValueError(f"Invalid header value b'Bearer {secret}\\r\\nX-Evil: 1'")

    with (
        patch(
            "hermes_voip.multi_intercom._fire_webhook_blocking",
            side_effect=_raise_value_error_with_secret,
        ),
        pytest.raises(WebhookError) as excinfo,
    ):
        asyncio.run(fire_webhook_opening(opening))

    assert secret not in str(excinfo.value), (
        "WebhookError message leaked the bearer token from the ValueError text"
    )
    # The secret-bearing cause is SUPPRESSED (`from None`): chaining it as __cause__
    # would re-expose the token in any printed traceback / logging.exception, so the
    # wrapper drops it (the dedicated traceback test asserts the full traceback is
    # secret-free).
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


# --- broaden control-char rejection to DEL (0x7f) and C1 (0x80-0x9f) -----------


def test_header_value_with_del_rejected_at_load(tmp_path: Path) -> None:
    """A webhook header VALUE containing DEL (0x7f) is rejected at config load.

    DEL and the C1 controls (0x80-0x9f) are not below 0x20, so a check that only
    rejects ``ord < 0x20`` lets them through. They are still header-unsafe, so the
    rejection band must include 0x7f-0x9f.
    """
    doc = {
        "intercoms": {
            "1000": {
                "openings": {
                    "gate": {
                        "type": "webhook",
                        "url": _FAKE_WEBHOOK_URL,
                        "headers": {"Authorization": "Bearer tok\x7fen"},
                    }
                }
            }
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="control"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_header_name_with_c1_control_rejected_at_load(tmp_path: Path) -> None:
    """A webhook header NAME containing a C1 control (0x85 NEL) is rejected at load."""
    doc = {
        "intercoms": {
            "1000": {
                "openings": {
                    "gate": {
                        "type": "webhook",
                        "url": _FAKE_WEBHOOK_URL,
                        "headers": {"X-Bad\x85Header": "value"},
                    }
                }
            }
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="control"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


# --- the secret must not leak via the EXCEPTION CHAIN (traceback), not just str ---


def test_webhook_error_traceback_does_not_leak_secret_via_cause() -> None:
    """The full traceback of the wrapped WebhookError must NOT contain the token.

    ``str(WebhookError)`` is already secret-free, but chaining the original
    ValueError as ``__cause__`` (``raise ... from exc``) means a printed traceback /
    ``logging.exception`` re-exposes the token (the cause ValueError's text is
    ``Invalid header value b'Bearer <token>...'``). The fix suppresses the cause
    (``raise ... from None``). This drives the REAL http.client path so the genuine
    secret-bearing ValueError is produced as the (now-suppressed) cause.
    """
    secret = "SECRET-fake-bearer-token-0000"  # obvious fake
    # An opening carrying a CRLF-injected header value directly (as if it slipped
    # past load-time validation), so the REAL http.client raises the secret-bearing
    # ValueError inside _fire_webhook_blocking.
    opening = Opening(
        name="gate",
        type=OpeningType.WEBHOOK,
        url=_FAKE_WEBHOOK_URL,
        headers={"Authorization": f"Bearer {secret}\r\nX-Evil: 1"},
    )

    with pytest.raises(WebhookError) as excinfo:
        asyncio.run(fire_webhook_opening(opening))

    err = excinfo.value
    formatted = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    assert secret not in formatted, (
        "the bearer token leaked into the wrapped WebhookError's traceback via the "
        "exception cause chain"
    )
    assert secret not in str(err)


# --- the control-char ConfigError must report only the code point, not the raw name -


def test_header_name_control_error_does_not_echo_raw_name(tmp_path: Path) -> None:
    """The header-NAME control-char ConfigError reports only the code point.

    The header name is operator config that may itself be sensitive; the spec
    requires the rejection message to carry ONLY the offending code point (U+XXXX)
    and a generic location, never the raw header name.
    """
    raw_name = "X-SECRET-fake-bearer-token-0000"  # obvious fake, stands in for a name
    doc = {
        "intercoms": {
            "1000": {
                "openings": {
                    "gate": {
                        "type": "webhook",
                        "url": _FAKE_WEBHOOK_URL,
                        "headers": {f"{raw_name}\r\nEvil": "value"},
                    }
                }
            }
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="control") as excinfo:
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))
    assert raw_name not in str(excinfo.value), (
        "the control-char ConfigError echoed the raw header NAME"
    )


def test_header_value_control_error_does_not_echo_raw_name(tmp_path: Path) -> None:
    """The header-VALUE control-char ConfigError does not echo the raw header name."""
    raw_name = "X-SECRET-fake-bearer-token-0000"  # obvious fake
    doc = {
        "intercoms": {
            "1000": {
                "openings": {
                    "gate": {
                        "type": "webhook",
                        "url": _FAKE_WEBHOOK_URL,
                        "headers": {raw_name: "Bearer tok\r\nen"},
                    }
                }
            }
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="control") as excinfo:
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))
    assert raw_name not in str(excinfo.value), (
        "the control-char ConfigError echoed the raw header NAME"
    )


def test_non_string_header_value_error_does_not_echo_raw_name(tmp_path: Path) -> None:
    """A non-string header VALUE is rejected without echoing the raw header name.

    Consistency with the control-char path's "don't echo raw config strings in a
    ConfigError" contract: the rejection reports only a generic location, not the
    raw header name.
    """
    raw_name = "X-SECRET-fake-bearer-token-0000"  # obvious fake
    doc = {
        "intercoms": {
            "1000": {
                "openings": {
                    "gate": {
                        "type": "webhook",
                        "url": _FAKE_WEBHOOK_URL,
                        "headers": {raw_name: 1234},  # non-string value
                    }
                }
            }
        }
    }
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="string") as excinfo:
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))
    assert raw_name not in str(excinfo.value), (
        "the non-string-value ConfigError echoed the raw header NAME"
    )
