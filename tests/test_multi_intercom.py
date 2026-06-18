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

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from hermes_voip.config import ConfigError
from hermes_voip.multi_intercom import (
    MultiIntercomConfig,
    Opening,
    OpeningType,
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


def test_unknown_opening_type_rejected(tmp_path: Path) -> None:
    doc = {"intercoms": {"1000": {"openings": {"door": {"type": "banana"}}}}}
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="type"):
        load_multi_intercom_config(_env(HERMES_VOIP_INTERCOM_CONFIG_FILE=path))


def test_intercom_with_no_openings_rejected(tmp_path: Path) -> None:
    doc = {"intercoms": {"1000": {"openings": {}}}}
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
