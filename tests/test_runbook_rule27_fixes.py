"""Tests that enforce rule-27 fixes in runbooks 0007/0013/0014/0015.

Rule 27: no aspirational or contradictory docs — runbooks must describe what IS.
Each test checks that a false claim has been removed AND a correct replacement
is present.
"""

from pathlib import Path

_RUNBOOKS = Path(__file__).parent.parent / "docs" / "runbooks"


# ---------------------------------------------------------------------------
# Issue (1): runbook-0013 §6 and runbook-0014 Error-handling — provider errors
#            falsely described as spoken verbatim / "NOT FIXED"
#            ADR-0063 SHIPPED provider_error.py + adapter.py intercept.
# ---------------------------------------------------------------------------


def test_runbook_0013_section6_provider_error_not_spoken_verbatim() -> None:
    """runbook-0013 §6 must NOT claim provider errors are spoken verbatim.

    ADR-0063 shipped is_provider_error + resolve_error_apology in adapter.py
    (~line 1435), which intercepts the raw error and plays a safe apology phrase
    instead of speaking the error string to the caller.
    """
    content = (_RUNBOOKS / "0013-voip-incident-oncall.md").read_text()

    # FALSE claims that must no longer appear
    assert "spoken verbatim to the caller" not in content, (
        "runbook-0013 still says provider errors are 'spoken verbatim to the caller'. "
        "ADR-0063 shipped the intercept; the error is NOT spoken to the caller."
    )
    assert "known leak (Task #26" not in content, (
        "runbook-0013 still references 'Task #26 / ADR backlog' for the error-leak. "
        "ADR-0063 SHIPPED the fix; the 'NOT FIXED' tracking note is now a false claim."
    )

    # PRESENCE: correct behaviour must be described
    assert "ADR-0063" in content, (
        "runbook-0013 must reference ADR-0063 (the shipped provider-error intercept)."
    )
    assert "apology" in content.lower() or "safe" in content.lower(), (
        "runbook-0013 must describe the safe/apology spoken replacement (ADR-0063)."
    )


def test_runbook_0014_error_handling_not_fixed_claim_removed() -> None:
    """runbook-0014 Error-handling must NOT say 'NOT FIXED' or Task #26.

    ADR-0063 shipped the error intercept; the section now tracks an instrumentation
    gap (counter not yet wired), not a missing error-intercept.
    """
    content = (_RUNBOOKS / "0014-voip-slo-metrics.md").read_text()

    # FALSE claims
    assert "current known leak" not in content, (
        "runbook-0014 still labels the provider-error path a 'current known leak'. "
        "ADR-0063 SHIPPED the intercept — callers no longer hear raw errors."
    )
    assert "NOT FIXED" not in content, (
        "runbook-0014 still says 'NOT FIXED' for the provider-error path. "
        "ADR-0063 shipped; update to describe what IS."
    )
    assert "Task #26" not in content, (
        "runbook-0014 still references 'Task #26'. "
        "The tracking note is stale — ADR-0063 shipped the fix."
    )

    # PRESENCE: section must acknowledge ADR-0063 shipped
    assert "ADR-0063" in content, (
        "runbook-0014 must reference ADR-0063 (shipped provider-error intercept)."
    )


# ---------------------------------------------------------------------------
# Issue (2): runbook-0014 Packet-loss paragraph falsely says SRTCP does not exist.
#            ADR-0066 SHIPPED src/hermes_voip/media/srtcp.py + adapter wiring;
#            HERMES_VOIP_SECURED_RTCP_ENABLED gates it (config.py ~470).
# ---------------------------------------------------------------------------


def test_runbook_0014_packet_loss_srtcp_not_nonexistent() -> None:
    """runbook-0014 packet-loss section must NOT claim SRTCP/secured RTCP is missing.

    ADR-0066 shipped srtcp.py and wired it in adapter.py; it is opt-in via
    HERMES_VOIP_SECURED_RTCP_ENABLED (default off after a live finding that
    SRTCP breaks audio on non-mux gateways).
    """
    content = (_RUNBOOKS / "0014-voip-slo-metrics.md").read_text()

    # FALSE claims
    assert "has no SRTCP" not in content, (
        "runbook-0014 still says 'has no SRTCP'. "
        "ADR-0066 shipped srtcp.py; the claim is false."
    )
    assert "no SRTCP (RFC 3711" not in content, (
        "runbook-0014 still says 'no SRTCP (RFC 3711…)'. "
        "ADR-0066 shipped the SRTCP transform."
    )
    assert "SRTCP follow-up" not in content, (
        "runbook-0014 still tracks 'SRTCP follow-up' as a future item. "
        "ADR-0066 shipped; remove the future-tense tracking."
    )

    # PRESENCE: correct opt-in posture must be described
    assert "HERMES_VOIP_SECURED_RTCP_ENABLED" in content, (
        "runbook-0014 must mention HERMES_VOIP_SECURED_RTCP_ENABLED "
        "(the ADR-0066 opt-in flag, default off)."
    )
    assert "ADR-0066" in content, (
        "runbook-0014 must reference ADR-0066 (shipped SRTCP transform)."
    )


# ---------------------------------------------------------------------------
# Issue (3): runbook-0015 ~lines 26-32 falsely says adapter does not yet pass
#            no_input/goodbye kwargs and env wiring is a 'planned follow-on'.
#            Both are fully wired: adapter.py ~5054-5059; config.py ~343-374.
# ---------------------------------------------------------------------------


def test_runbook_0015_adapter_kwargs_wired_not_planned() -> None:
    """runbook-0015 must NOT claim the adapter does not pass no_input/goodbye kwargs.

    adapter.py _run_call_loop (~line 5104-5109) passes all six kwargs from
    media_cfg; config.py (~343-374) exposes all six as HERMES_VOIP_* env vars.
    The 'planned follow-on' claim is false.
    """
    content = (_RUNBOOKS / "0015-voip-silence-reprompt-and-goodbye.md").read_text()

    # FALSE claims
    assert "does not yet pass" not in content, (
        "runbook-0015 still says the adapter 'does not yet pass' the kwargs. "
        "adapter.py _run_call_loop passes all six no_input/goodbye kwargs "
        "from media_cfg."
    )
    assert "planned follow-on" not in content, (
        "runbook-0015 still describes env wiring as a 'planned follow-on'. "
        "config.py fully exposes HERMES_VOIP_NO_INPUT_* and HERMES_VOIP_GOODBYE* "
        "env vars."
    )
    assert "no `HERMES_VOIP_*` env var" not in content, (
        "runbook-0015 still says there is no HERMES_VOIP_* env var for these knobs. "
        "config.py wires all six."
    )

    # PRESENCE: correct wired state must be described
    assert "HERMES_VOIP_NO_INPUT_REPROMPT" in content or (
        "fully wired" in content.lower() or "wired" in content.lower()
    ), (
        "runbook-0015 must describe that the adapter kwargs are wired via config.py "
        "(or mention the env var HERMES_VOIP_NO_INPUT_REPROMPT)."
    )


# ---------------------------------------------------------------------------
# Issue (4): runbook-0013 TTS health-check uses HERMES_VOIP_ELEVENLABS_API_KEY.
#            config.py _ELEVENLABS_API_KEY = 'ELEVENLABS_API_KEY' (no prefix).
# ---------------------------------------------------------------------------


def test_runbook_0013_elevenlabs_env_var_canonical() -> None:
    """runbook-0013 TTS health-check must use ELEVENLABS_API_KEY (no prefix).

    config.py line ~209: _ELEVENLABS_API_KEY = 'ELEVENLABS_API_KEY'.
    The wrong HERMES_VOIP_ELEVENLABS_API_KEY would always be empty, making the
    health-check silently unusable.
    """
    content = (_RUNBOOKS / "0013-voip-incident-oncall.md").read_text()

    # FALSE: the prefixed variant should not appear in the health-check context
    assert "HERMES_VOIP_ELEVENLABS_API_KEY" not in content, (
        "runbook-0013 still uses 'HERMES_VOIP_ELEVENLABS_API_KEY'. "
        "The canonical env var is 'ELEVENLABS_API_KEY' (config.py ~line 209). "
        "The HERMES_VOIP_ prefix variant is never read by the code."
    )

    # PRESENCE: canonical var must appear
    assert "ELEVENLABS_API_KEY" in content, (
        "runbook-0013 must use the canonical 'ELEVENLABS_API_KEY' "
        "in the TTS health-check."
    )


# ---------------------------------------------------------------------------
# Issue (5): runbook-0007 knobs table omits HERMES_VOIP_RING_TIMEOUT_SECS.
#            voip_tools.py ~line 259 reads it; ADR-0086 shipped the feature.
# ---------------------------------------------------------------------------


def test_runbook_0007_ring_timeout_secs_in_knobs() -> None:
    """runbook-0007 knobs table must include HERMES_VOIP_RING_TIMEOUT_SECS.

    voip_tools.py _parse_ring_timeout() reads HERMES_VOIP_RING_TIMEOUT_SECS (ADR-0086).
    An on-call engineer consulting the knobs table would not know this env var exists
    without it being listed.
    """
    content = (_RUNBOOKS / "0007-voip-outbound-calling.md").read_text()

    assert "HERMES_VOIP_RING_TIMEOUT_SECS" in content, (
        "runbook-0007 knobs table must include HERMES_VOIP_RING_TIMEOUT_SECS. "
        "voip_tools.py ~line 259 reads this var (ADR-0086). "
        "Absence means an on-call engineer cannot discover the ring-timeout knob."
    )
