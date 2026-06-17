"""Tests for hermes_voip.caller_modes — caller classification + persona (ADR-0020).

The module is pure and sans-IO beyond a one-time list-file read. It classifies a
**forgeable** caller-ID into a :class:`CallerMode`, maps the mode to a privilege
bit and a spotlighted persona preamble, and loads the allow/deny/grey lists from
operator-managed JSON files addressed by env-var paths (PII-safe — never inline).

The load-bearing security properties asserted here:

* Classification is **deny-biased**: deny > allow > grey > default(grey). A number
  on both deny and allow is denied (fail safe).
* The default for an unmatched caller is **GREY** (receptionist), never ALLOW —
  privilege is strictly opt-in on a forgeable identifier.
* ``GREY``/``OUTBOUND`` are **not privileged**; only ``ALLOW`` is. ``DENY`` is
  rejected at SIP setup and never produces a persona.

PUBLIC repo: all numbers are obvious fakes (``+15551230000`` etc.); no list file
in the repo ever contains a real number — tests write their own to a tmp path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_voip.caller_modes import (
    CallerClassification,
    CallerMode,
    CallerModeConfig,
    Normalization,
    classify_caller,
    load_caller_modes,
    persona_preamble,
)
from hermes_voip.config import ConfigError

# --- fakes (PUBLIC repo: never a real number) -------------------------------
_TRUSTED = "+15551230001"
_BLOCKED = "+15551230002"
_GREY_PIN = "+15551230003"
_UNKNOWN = "+15551239999"


def _cfg(
    *,
    allow: tuple[str, ...] = (),
    deny: tuple[str, ...] = (),
    grey: tuple[str, ...] = (),
    default_mode: CallerMode = CallerMode.GREY,
    normalization: Normalization = Normalization.E164,
) -> CallerModeConfig:
    return CallerModeConfig(
        allow=allow,
        deny=deny,
        grey=grey,
        default_mode=default_mode,
        normalization=normalization,
    )


# --- classification precedence (deny > allow > grey > default) --------------


def test_unmatched_caller_defaults_to_grey() -> None:
    cls = classify_caller(_UNKNOWN, _cfg())
    assert cls.mode is CallerMode.GREY
    assert cls.source == "default"


def test_allow_listed_caller_is_allow() -> None:
    cls = classify_caller(_TRUSTED, _cfg(allow=(_TRUSTED,)))
    assert cls.mode is CallerMode.ALLOW
    # ADR-0021: the legacy shim synthesises a "operator" group for the allow-list;
    # source is the group name, not the legacy mode name.
    assert cls.source == "operator"
    assert cls.matched_pattern == _TRUSTED


def test_deny_listed_caller_is_deny() -> None:
    cls = classify_caller(_BLOCKED, _cfg(deny=(_BLOCKED,)))
    assert cls.mode is CallerMode.DENY
    # ADR-0021: the legacy shim synthesises a "blocked" group for the deny-list;
    # source is the group name, not the legacy mode name.
    assert cls.source == "blocked"


def test_deny_beats_allow_for_a_number_on_both_lists() -> None:
    # Fail safe: a number on BOTH deny and allow is DENIED.
    cls = classify_caller(_TRUSTED, _cfg(allow=(_TRUSTED,), deny=(_TRUSTED,)))
    assert cls.mode is CallerMode.DENY


def test_explicit_grey_pin_forces_receptionist() -> None:
    # An operator can pin a specific caller to the receptionist (grey) group.
    # (Behaviour change, fix/voip-default-mode-privilege-clamp: the prior version
    # of this test used default_mode=ALLOW to show the pin overriding a privileged
    # default; a privileged default is now rejected at construction — see
    # test_default_mode_allow_is_rejected_at_construction — so the pin is shown
    # against the safe GREY default. The grey-pin-wins assertion is preserved.)
    cfg = _cfg(grey=(_GREY_PIN,))
    cls = classify_caller(_GREY_PIN, cfg)
    assert cls.mode is CallerMode.GREY
    # ADR-0021: the legacy shim synthesises a "receptionist" group for the grey-list;
    # source is the group name, not the legacy mode name.
    assert cls.source == "receptionist"
    assert cls.group.privilege_level == 0


def test_privileged_default_mode_allow_is_refused_not_applied() -> None:
    # Behaviour change (fix/voip-default-mode-privilege-clamp): this test previously
    # asserted that default_mode=ALLOW mapped an unmatched caller to ALLOW
    # ("applies only to unmatched"). That WAS the fail-open privilege-escalation
    # gap — an unknown, forgeable caller reaching operator privilege_level=3. The
    # hardened contract refuses the privileged default at construction instead of
    # applying it, so there is no unmatched-caller-gets-ALLOW path to assert. The
    # assertion is strengthened from "permissively ALLOW" to "hard ConfigError".
    with pytest.raises(ConfigError, match="ALLOW"):
        _cfg(default_mode=CallerMode.ALLOW)


# --- normalization ----------------------------------------------------------


def test_e164_normalization_matches_despite_punctuation() -> None:
    # The From AOR carried punctuation/spaces; the list entry is clean E.164.
    cls = classify_caller("+1 (555) 123-0001", _cfg(allow=(_TRUSTED,)))
    assert cls.mode is CallerMode.ALLOW


def test_e164_prepends_plus_for_a_bare_international_number() -> None:
    # A bare "15551230001" normalizes to "+15551230001" and matches.
    cls = classify_caller("15551230001", _cfg(allow=(_TRUSTED,)))
    assert cls.mode is CallerMode.ALLOW


def test_strip_plus_normalization_matches_digits_only() -> None:
    cfg = _cfg(allow=("15551230001",), normalization=Normalization.STRIP_PLUS)
    cls = classify_caller("+15551230001", cfg)
    assert cls.mode is CallerMode.ALLOW


def test_none_normalization_is_verbatim() -> None:
    # NONE: a bare extension presented verbatim by the gateway matches verbatim.
    cfg = _cfg(allow=("1000",), normalization=Normalization.NONE)
    assert classify_caller("1000", cfg).mode is CallerMode.ALLOW
    # ...and a punctuated form does NOT match under NONE.
    assert classify_caller("+1000", cfg).mode is CallerMode.GREY


def test_raw_form_also_matches_when_gateway_does_not_normalize() -> None:
    # Both the normalized and the raw forms are tried against each list, so a
    # bare-extension entry still matches even under E164 normalization.
    cfg = _cfg(allow=("1000",), normalization=Normalization.E164)
    assert classify_caller("1000", cfg).mode is CallerMode.ALLOW


# --- prefix (block) matching ------------------------------------------------


def test_prefix_pattern_matches_a_block_of_numbers() -> None:
    cfg = _cfg(deny=("+15551230*",))
    assert classify_caller("+15551230055", cfg).mode is CallerMode.DENY
    assert classify_caller("+15551239999", cfg).mode is CallerMode.GREY


def test_prefix_is_literal_startswith_not_regex() -> None:
    # A '.' in a pattern is a literal dot, not a regex wildcard (no ReDoS).
    cfg = _cfg(deny=("+1555123000.*",))
    # The literal '.' cannot match a digit, so this does NOT deny.
    assert classify_caller("+155512300011", cfg).mode is CallerMode.GREY


# --- privilege mapping (the security spine) ---------------------------------


def test_only_allow_is_privileged() -> None:
    assert CallerMode.ALLOW.privileged is True
    assert CallerMode.GREY.privileged is False
    assert CallerMode.OUTBOUND.privileged is False
    assert CallerMode.DENY.privileged is False


# --- fail-loud on a privileged default (the privilege-escalation clamp) ------
#
# Hardening (fix/voip-default-mode-privilege-clamp): an UNMATCHED caller must
# NEVER reach operator privilege (privilege_level=3, the IRREVERSIBLE tier) by
# construction, regardless of config. Caller-ID is forgeable SIP identity — a
# trust HINT, never authentication — so least privilege is the default and
# operator privilege requires an explicit allow-list MATCH, never the catch-all
# default. The legacy path therefore fails LOUD exactly like the N-group JSON
# path (caller_modes._parse_groups_document rejects a default_group with
# privilege_level != 0): `HERMES_VOIP_CALLER_DEFAULT_MODE=allow` maps the
# unmatched caller to the operator group (level 3), which is rejected at config
# CONSTRUCTION so load_caller_modes(), classify_caller(), AND the adapter path
# all fail loud — there is no way to construct the fail-open state.


def test_default_mode_allow_is_rejected_at_construction() -> None:
    # default_mode=ALLOW would map every unmatched (unknown, forgeable) caller to
    # the operator group at privilege_level=3 — the IRREVERSIBLE tier. That is the
    # fail-open privilege-escalation gap; constructing it must raise (rule 37,
    # mirrors the DENY/OUTBOUND rejections already in __post_init__).
    with pytest.raises(ConfigError, match="ALLOW"):
        _cfg(default_mode=CallerMode.ALLOW)


def test_load_rejects_default_mode_allow() -> None:
    # The env-driven loader must fail loud too: HERMES_VOIP_CALLER_DEFAULT_MODE=allow
    # is the documented "loosening" that grants operator privilege to unmatched
    # callers on a forgeable identifier — now refused, matching the N-group JSON
    # path's rejection of a privileged default_group.
    with pytest.raises(ConfigError, match="ALLOW"):
        load_caller_modes({"HERMES_VOIP_CALLER_DEFAULT_MODE": "allow"})


def test_unmatched_caller_can_never_reach_operator_privilege() -> None:
    # The by-construction guarantee: there is NO legacy CallerModeConfig whose
    # default places an unmatched caller at privilege_level >= 2. The only way to
    # construct a config is default_mode=GREY (level 0), so an unknown caller is
    # always the unprivileged receptionist. (A privileged default raises above.)
    cfg = _cfg()  # the only constructible default is GREY
    cls = classify_caller(_UNKNOWN, cfg)
    assert cls.source == "default"
    assert cls.group.privilege_level == 0
    assert cls.group.privilege_level < 2  # never ELEVATED/IRREVERSIBLE
    assert cls.mode is CallerMode.GREY


def test_live_shaped_config_grey_default_is_unaffected() -> None:
    # The LIVE launch uses an explicit allow-list + default=grey (the safe
    # default). This hardening must NOT change that posture: an allow-listed
    # caller still gets the operator tier (level 3); an UNMATCHED caller still
    # falls to the receptionist (level 0), never operator.
    cfg = _cfg(allow=(_TRUSTED,), default_mode=CallerMode.GREY)

    matched = classify_caller(_TRUSTED, cfg)
    assert matched.mode is CallerMode.ALLOW
    assert matched.group.name == "operator"
    assert matched.group.privilege_level == 3

    unmatched = classify_caller(_UNKNOWN, cfg)
    assert unmatched.source == "default"
    assert unmatched.mode is CallerMode.GREY
    assert unmatched.group.name == "receptionist"
    assert unmatched.group.privilege_level == 0


# --- persona preamble (spotlighted, untrusted-data marked) ------------------


def test_receptionist_preamble_for_grey_is_constrained() -> None:
    text = persona_preamble(CallerMode.GREY)
    lowered = text.lower()
    assert "receptionist" in lowered
    # It must instruct screening + forbid privileged actions + mark caller as data.
    assert "who" in lowered  # "ask who is calling"
    assert "untrusted" in lowered
    assert "transfer" in lowered  # explicit prohibition listed


def test_assistant_preamble_for_allow_grants_the_assistant_persona() -> None:
    text = persona_preamble(CallerMode.ALLOW)
    assert "assistant" in text.lower()


def test_outbound_preamble_is_task_scoped_and_resists_steering() -> None:
    text = persona_preamble(CallerMode.OUTBOUND)
    lowered = text.lower()
    # Task-scoped: pursue the operator's task, no operator secrets, untrusted callee.
    assert "task" in lowered
    assert "untrusted" in lowered
    assert "secret" in lowered or "credential" in lowered


def test_deny_has_no_persona() -> None:
    # DENY never reaches a turn; asking for its persona is a programming error.
    with pytest.raises(ValueError, match="DENY"):
        persona_preamble(CallerMode.DENY)


def test_classification_is_frozen() -> None:
    cls = classify_caller(_UNKNOWN, _cfg())
    assert isinstance(cls, CallerClassification)
    # ADR-0021: `mode` is now a @property (read-only) on a frozen=True/slots=True
    # dataclass; Python 3.13 raises TypeError (not AttributeError) when assigning
    # to a property via slots — both indicate immutability as intended.
    with pytest.raises((AttributeError, TypeError)):
        cls.mode = CallerMode.ALLOW  # type: ignore[misc]


# --- list-file loading (PII-safe: paths in env, numbers in gitignored files) -


def test_load_with_no_files_makes_every_caller_grey() -> None:
    cfg = load_caller_modes({})
    assert cfg.allow == ()
    assert cfg.deny == ()
    assert cfg.grey == ()
    assert cfg.default_mode is CallerMode.GREY
    assert cfg.normalization is Normalization.E164
    # The safe default: nothing configured => receptionist for everyone.
    assert classify_caller(_UNKNOWN, cfg).mode is CallerMode.GREY
    assert classify_caller(_TRUSTED, cfg).mode is CallerMode.GREY


def test_load_reads_patterns_from_json_files(tmp_path: Path) -> None:
    allow_file = tmp_path / ".caller-allow.json"
    deny_file = tmp_path / ".caller-deny.json"
    allow_file.write_text(json.dumps({"patterns": [_TRUSTED]}), encoding="utf-8")
    deny_file.write_text(json.dumps({"patterns": [_BLOCKED]}), encoding="utf-8")
    cfg = load_caller_modes(
        {
            "HERMES_VOIP_CALLER_ALLOW_FILE": str(allow_file),
            "HERMES_VOIP_CALLER_DENY_FILE": str(deny_file),
        }
    )
    assert cfg.allow == (_TRUSTED,)
    assert cfg.deny == (_BLOCKED,)
    assert classify_caller(_TRUSTED, cfg).mode is CallerMode.ALLOW
    assert classify_caller(_BLOCKED, cfg).mode is CallerMode.DENY


def test_load_unset_file_path_is_empty() -> None:
    # An UNSET path => empty list (an operator may run with only a deny list, or
    # none). This is logged at INFO, not raised.
    cfg = load_caller_modes({})
    assert cfg.allow == ()


def test_load_configured_but_missing_file_raises(tmp_path: Path) -> None:
    # A path that IS configured but does not exist is a misconfiguration and must
    # fail LOUDLY (rule 37): silently treating a configured-but-missing list as
    # empty would change the security posture without warning — e.g. an operator's
    # allow list vanishing would silently drop every trusted caller to the
    # receptionist (or a missing deny list would stop blocking). unset != missing.
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(ConfigError):
        load_caller_modes({"HERMES_VOIP_CALLER_ALLOW_FILE": str(missing)})


def test_load_malformed_file_raises_config_error(tmp_path: Path) -> None:
    # A present-but-malformed security-relevant file fails LOUDLY (rule 37),
    # never silently treated as empty.
    bad = tmp_path / ".caller-deny.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_caller_modes({"HERMES_VOIP_CALLER_DENY_FILE": str(bad)})


def test_load_wrong_shape_file_raises_config_error(tmp_path: Path) -> None:
    # A JSON file whose "patterns" is not a list of strings is malformed.
    bad = tmp_path / ".caller-allow.json"
    bad.write_text(json.dumps({"patterns": "not-a-list"}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_caller_modes({"HERMES_VOIP_CALLER_ALLOW_FILE": str(bad)})


def test_load_default_mode_from_env() -> None:
    # Behaviour change (fix/voip-default-mode-privilege-clamp): this test previously
    # loaded default=allow and asserted the loader returned CallerMode.ALLOW. A
    # privileged default is now refused (see test_load_rejects_default_mode_allow),
    # so the env-knob-parses-the-default coverage is shown against the only valid
    # explicit value, GREY (the safe receptionist default). Assertion preserved.
    cfg = load_caller_modes({"HERMES_VOIP_CALLER_DEFAULT_MODE": "grey"})
    assert cfg.default_mode is CallerMode.GREY


def test_load_rejects_default_mode_deny() -> None:
    # default=deny would block every unknown caller — a foot-gun; reject it.
    with pytest.raises(ConfigError):
        load_caller_modes({"HERMES_VOIP_CALLER_DEFAULT_MODE": "deny"})


def test_load_rejects_unknown_default_mode() -> None:
    with pytest.raises(ConfigError):
        load_caller_modes({"HERMES_VOIP_CALLER_DEFAULT_MODE": "wat"})


def test_load_normalization_from_env() -> None:
    cfg = load_caller_modes({"HERMES_VOIP_CALLER_NORMALIZATION": "strip-plus"})
    assert cfg.normalization is Normalization.STRIP_PLUS


def test_load_rejects_unknown_normalization() -> None:
    with pytest.raises(ConfigError):
        load_caller_modes({"HERMES_VOIP_CALLER_NORMALIZATION": "rot13"})


def test_load_rejects_inline_number_list_env() -> None:
    # Inline number lists in env would leak PII into shell history / printenv.
    # Only the *_FILE path form is accepted; an inline list var is rejected.
    with pytest.raises(ConfigError, match="HERMES_VOIP_CALLER_ALLOW"):
        load_caller_modes({"HERMES_VOIP_CALLER_ALLOW": _TRUSTED})
