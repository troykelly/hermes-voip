"""Test that runbook 0013 §3 (Graceful shutdown) matches ADR-0059 and §Restart.

Rule 27: no aspirational or contradictory docs — runbooks must describe what IS.
This test validates that §3 does NOT falsely claim graceful shutdown is
unimplemented, since ADR-0059 shipped a full BYE-drain disconnect() in
adapter.py.
"""

import re
from pathlib import Path


def test_runbook_0013_section3_graceful_shutdown_documents_shipped_behavior() -> None:
    """Verify that runbook 0013 §3 matches ADR-0059 implementation.

    ADR-0059 shipped graceful shutdown via SIGTERM + BYE-drain (disconnect()).
    Section 3 must NOT falsely claim "graceful shutdown not yet implemented" or
    recommend kill -9 as the primary path.
    """
    runbook_path = (
        Path(__file__).parent.parent
        / "docs"
        / "runbooks"
        / "0013-voip-incident-oncall.md"
    )
    content = runbook_path.read_text()

    # Section 3 spans from the "### 3. Graceful shutdown" header to the next "---"
    section3_match = re.search(
        r"### 3\. Graceful shutdown.*?(?=\n---\n)", content, re.DOTALL
    )
    assert section3_match is not None, "Section 3 (Graceful shutdown) not found"
    section3 = section3_match.group(0)

    # FAIL: Section 3 still contains the false "not yet implemented" claim
    false_claim = "does not currently implement graceful shutdown"
    assert false_claim not in section3, (
        f"Section 3 contains false claim: '{false_claim}'. "
        "This contradicts ADR-0059 which shipped graceful shutdown via "
        "SIGTERM + BYE-drain."
    )

    # FAIL: Section 3 still says "available in future"
    false_future = "available in future"
    assert false_future not in section3, (
        f"Section 3 contains '{false_future}' — this is FALSE. "
        "ADR-0059 shipped graceful shutdown. Rewrite to describe what IS."
    )

    # Section 3 must POSITIVELY document the graceful drain path — a regression to
    # hard-kill-only guidance (no SIGTERM/drain) is itself a rule-27 defect and must
    # FAIL this test (the previous gated check let that regression pass silently).
    assert "kill -TERM" in section3, (
        "Section 3 must document the graceful 'kill -TERM' drain path (ADR-0059), "
        "not only a hard kill."
    )
    assert "drain" in section3.lower() or "graceful" in section3.lower(), (
        "Section 3 must describe the BYE-drain / graceful shutdown (ADR-0059)."
    )

    # When kill -9 is mentioned (the fallback), kill -TERM must come FIRST.
    section3_lines = section3.split("\n")
    sigterm_line = next(
        i for i, line in enumerate(section3_lines) if "kill -TERM" in line
    )
    kill9_line = next(
        (i for i, line in enumerate(section3_lines) if "kill -9" in line), None
    )
    if kill9_line is not None:
        assert sigterm_line < kill9_line, (
            f"Section 3 lists 'kill -TERM' at line {sigterm_line} but "
            f"'kill -9' at line {kill9_line}. The section must recommend SIGTERM "
            "first (graceful), then kill -9 as fallback only."
        )


def test_runbook_0013_restart_and_section3_align() -> None:
    """Verify that §Restart describes consistent shutdown/drain with §3."""
    runbook_path = (
        Path(__file__).parent.parent
        / "docs"
        / "runbooks"
        / "0013-voip-incident-oncall.md"
    )
    content = runbook_path.read_text()

    # Extract §Restart (section 1 under "## Restart")
    restart_match = re.search(
        r"## Restart\s+.*?### 1\. Stop the process.*?(?=\n### [0-9]|\n---|\Z)",
        content,
        re.DOTALL,
    )
    assert restart_match is not None, "Restart section not found"
    restart_section = restart_match.group(0)

    # §Restart should recommend SIGTERM (graceful) FIRST
    assert "kill -TERM" in restart_section or "SIGTERM" in restart_section, (
        "Restart §1 (Stop the process) must recommend SIGTERM/kill -TERM as "
        "the primary method."
    )

    # §Restart should mention "graceful shutdown" or "drain" behavior
    assert (
        "graceful" in restart_section.lower() or "drain" in restart_section.lower()
    ), "Restart §1 must mention graceful shutdown or drain behavior."

    # §Restart should mention ADR-0059
    assert "ADR-0059" in restart_section, "Restart §1 should cross-reference ADR-0059."


def test_runbook_0013_wss_wired_claim_is_accurate() -> None:
    """Verify runbook 0013 does not falsely claim WSS signalling is unwired.

    ADR-0038 (Accepted) shipped WssSipTransport; adapter.py lines ~1135-1146 wire
    it so inbound INVITEs over WSS reach _on_inbound_invite identically to TLS.
    Any claim that 'WSS signalling is not yet wired' or that WebRTC inbound is 'in
    the roadmap' is a rule-27 defect.
    """
    runbook_path = (
        Path(__file__).parent.parent
        / "docs"
        / "runbooks"
        / "0013-voip-incident-oncall.md"
    )
    content = runbook_path.read_text()

    false_claim_unwired = "WSS signalling is not yet wired"
    assert false_claim_unwired not in content, (
        f"Runbook contains false claim: '{false_claim_unwired}'. "
        "ADR-0038 shipped WssSipTransport and it is wired in adapter.py."
    )

    false_claim_roadmap = "WebRTC inbound calls are in the\n  roadmap"
    assert false_claim_roadmap not in content, (
        "Runbook falsely places WebRTC inbound calls 'in the roadmap'. "
        "ADR-0038 shipped this; update to describe what IS."
    )

    # PRESENCE assertion: the corrected WSS-wired statement must appear.
    # Matches the actual corrected text in the runbook (§ "No inbound calls arriving",
    # "Check transport mismatch" subsection) — guards against silent deletion or
    # replacement with another false claim.
    assert "WSS signalling IS wired (ADR-0038)" in content, (
        "Runbook must CONTAIN the corrected WSS-wired statement "
        "'WSS signalling IS wired (ADR-0038)'. "
        "Absence means the section was deleted or rewritten into another false claim."
    )


def test_runbook_0013_tts_fallback_token_is_hyphenated() -> None:
    """Verify the TTS fallback env-var example uses 'sherpa-kokoro' (hyphen).

    config.py _TTS_PROVIDERS uses 'sherpa-kokoro' (hyphen). The runbook previously
    instructed operators to set HERMES_VOIP_TTS_FALLBACK=sherpa_kokoro (underscore),
    which triggers a ConfigError at startup during a TTS outage.
    """
    runbook_path = (
        Path(__file__).parent.parent
        / "docs"
        / "runbooks"
        / "0013-voip-incident-oncall.md"
    )
    content = runbook_path.read_text()

    bad_token = "HERMES_VOIP_TTS_FALLBACK=sherpa_kokoro"
    assert bad_token not in content, (
        f"Runbook contains invalid token '{bad_token}'. "
        "config.py _TTS_PROVIDERS uses 'sherpa-kokoro' (hyphen, not underscore). "
        "Following the runbook as written triggers ConfigError at startup."
    )

    # PRESENCE assertion: the corrected hyphenated token must appear.
    # Guards against silent deletion or replacement with another wrong value.
    good_token = "HERMES_VOIP_TTS_FALLBACK=sherpa-kokoro"
    assert good_token in content, (
        f"Runbook must CONTAIN the valid token '{good_token}' "
        "(hyphen, matching config.py _TTS_PROVIDERS). "
        "Absence means the guidance was deleted or replaced with another wrong value."
    )
