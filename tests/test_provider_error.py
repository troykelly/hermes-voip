"""Tests for the provider/runtime-error detector + safe spoken reply (ADR-0063).

Pure, dependency-free module — covered by the default ``mypy --strict`` + pytest
gate (no hermes-runtime import). The detector recognises a backend/provider error
turn so the voip adapter speaks a short safe apology instead of reading a raw
``HTTP 502`` / stack-trace / provider message aloud to the caller (LAUNCH #4).
"""

from __future__ import annotations

import pytest

from hermes_voip.provider_error import (
    is_provider_error,
    resolve_error_apology,
    safe_error_reply,
)

# ---- the detector: real provider/runtime error shapes are recognised --------

# Shapes a Hermes/LLM backend can surface as the "reply" text on an unrecoverable
# turn (provider HTTP errors, raw exception/stack-trace text, gateway/proxy
# failures). None of these is a thing a voice agent should ever read aloud.
_ERROR_TEXTS: tuple[str, ...] = (
    "API call failed: HTTP 502 Bad Gateway",
    "Error: 503 Service Unavailable",
    "anthropic.InternalServerError: overloaded_error: Overloaded",
    "Request failed with status code 500",
    "openai.APIError: The server had an error processing your request.",
    "Traceback (most recent call last):\n  File ...\nValueError: boom",
    "RuntimeError: connection reset by peer",
    "502 Bad Gateway",
    "An internal error occurred (rate_limit_error). Please try again later.",
    "upstream connect error or disconnect/reset before headers",
    # Rate-limit (HTTP 429) — a common provider error that must not be spoken
    # (codex review MAJOR: was undetected unless it also carried a token/phrase).
    "HTTP 429 Too Many Requests",
    "Error: 429 Too Many Requests",
    "openai.RateLimitError: 429",
    # codex round-2 MAJOR: a raw status LINE with the HTTP version prefix, and the
    # "Error code: NNN" framing common to SDK exceptions, were slipping through.
    "HTTP/1.1 502 Bad Gateway",
    "Error code: 502",
)


@pytest.mark.parametrize("text", _ERROR_TEXTS)
def test_is_provider_error_recognises_error_shapes(text: str) -> None:
    assert is_provider_error(text) is True


# ---- the detector is conservative: genuine replies pass through -------------

# Natural conversational replies that merely MENTION numbers, errors, or services
# as ordinary words must NOT be misclassified (that would silence the agent).
_GENUINE_REPLIES: tuple[str, ...] = (
    "Sure, I can help you with that.",
    "Your booking reference is 502, and the room is ready.",
    "There was an error on the form you submitted last week — shall we fix it?",
    "The server room is on the third floor, just past reception.",
    "I'll connect you to the service desk now.",
    "That status update sounds great, congratulations!",
    "It failed to rain all summer, so the garden is very dry.",
    "Please hold for just a moment.",
    "",
    "   ",
    # Genuine technical-SUPPORT speech mentioning error phrases as content, not as
    # the agent's own failure (codex review MAJOR: bare reason phrases over-matched).
    "Your website is currently returning 503 Service Unavailable to visitors.",
    "It sounds like your server is showing an internal server error to customers.",
    "The page says bad gateway when I open it, so let's check your hosting.",
    "There are 500 people on the waitlist and 429 of them have confirmed.",
    # codex round-2 MAJOR: a genuine reply LEADING with a status number (no reason
    # phrase) must not be classified as an error by the start-of-message branch.
    "503 people are waiting in the queue right now.",
    "429 confirmed guests will attend the gala on Saturday.",
)


@pytest.mark.parametrize("text", _GENUINE_REPLIES)
def test_is_provider_error_does_not_over_match_genuine_replies(text: str) -> None:
    assert is_provider_error(text) is False


# ---- the safe spoken reply --------------------------------------------------


def test_safe_error_reply_default_language_is_a_short_safe_apology() -> None:
    """The English safe line is a brief apology — no error detail, no leak."""
    line = safe_error_reply("en")
    assert line.strip() != ""
    # It must not echo any provider/HTTP-status detail.
    for token in ("502", "503", "500", "http", "error", "traceback", "exception"):
        assert token not in line.lower()
    # Short enough to be a quick spoken filler, not a paragraph.
    assert len(line) <= 120


def test_safe_error_reply_unknown_language_falls_back_to_english() -> None:
    """An unknown language code returns the English safe line, never raises."""
    assert safe_error_reply("zz-unknown") == safe_error_reply("en")


def test_safe_error_reply_is_not_itself_classified_as_an_error() -> None:
    """The safe line must pass the detector — it is a genuine reply, not an error.

    (A regression guard: if the safe line ever tripped ``is_provider_error`` the
    adapter could loop substituting it for itself.)
    """
    assert is_provider_error(safe_error_reply("en")) is False


# ---- operator-overridable apology + per-language fallback (spec A) ----------
#
# resolve_error_apology(language, override) is the single seam used by the adapter.
# • When override is non-empty, it is returned verbatim (operator-configured line).
# • When override is absent/empty, the per-language line is returned if one exists.
# • When no per-language line exists, English fallback is returned (never raises).
# • For any language with a registered line (non-'en'), that line is returned.


def test_resolve_error_apology_uses_override_when_set() -> None:
    """A non-empty operator override is returned verbatim, regardless of language."""
    custom = "Un instant, je vous prie."
    assert resolve_error_apology("en", override=custom) == custom


def test_resolve_error_apology_override_takes_priority_over_language_line() -> None:
    """Override wins even when the language has a registered per-language line."""
    custom = "Operator custom line."
    # "fr" should have a language line (or fall back to en), but override always wins.
    assert resolve_error_apology("fr", override=custom) == custom


def test_resolve_error_apology_uses_per_language_line_when_no_override() -> None:
    """A non-'en' language with a registered line returns that line, not English."""
    # French is the canonical non-English test language.
    fr_line = resolve_error_apology("fr", override=None)
    en_line = resolve_error_apology("en", override=None)
    # The French line must exist and differ from English.
    assert fr_line.strip() != ""
    assert fr_line != en_line


def test_resolve_error_apology_falls_back_to_english_for_unknown_language() -> None:
    """An unknown language with no line falls back to English, never raises."""
    result = resolve_error_apology("zz-unknown", override=None)
    assert result == resolve_error_apology("en", override=None)


def test_resolve_error_apology_empty_override_uses_language_line() -> None:
    """An empty-string override is treated as absent (uses language fallback)."""
    en_line = resolve_error_apology("en", override=None)
    assert resolve_error_apology("en", override="") == en_line


def test_resolve_error_apology_result_is_not_itself_a_provider_error() -> None:
    """The resolved apology line must never trigger is_provider_error (loop guard)."""
    for lang in ("en", "fr", "de", "es"):
        line = resolve_error_apology(lang, override=None)
        assert not is_provider_error(line), (
            f"resolve_error_apology({lang!r}) returned a line that triggers "
            f"is_provider_error: {line!r}"
        )
    custom = "Un moment s'il vous plait."
    assert not is_provider_error(resolve_error_apology("en", override=custom))


# ---- classify_provider_error: exact category tokens + precedence ----------------
#
# classify_provider_error() returns one of five stable log-pipeline tokens. Each
# test below pins a DIFFERENT representative input to its EXACT expected token so
# that a constant-return mutant fails at least four of the five category tests, and
# the precedence test ensures a precedence-swap mutant fails.


from hermes_voip.provider_error import classify_provider_error  # noqa: E402


def test_classify_provider_error_traceback_token() -> None:
    """A Python stack-trace header returns exactly "traceback"."""
    text = "Traceback (most recent call last):\n  File 'x.py', line 1\nValueError: boom"
    assert classify_provider_error(text) == "traceback"


def test_classify_provider_error_provider_token_token() -> None:
    """A provider SDK error-class repr returns exactly "provider_token"."""
    # "RuntimeError:" matches _PROVIDER_TOKEN_RE (\b\w*error\b\s*:) and does NOT
    # match _TRACEBACK_RE, so the second detector fires.
    text = "RuntimeError: connection reset by peer"
    assert classify_provider_error(text) == "provider_token"


def test_classify_provider_error_http_error_token() -> None:
    """An HTTP 5xx/429 status in error context returns exactly "http_error"."""
    # "HTTP 502" matches _HTTP_ERROR_RE keyword branch; none of the higher detectors
    # fire on this plain status string (no traceback, no provider token, no phrase).
    text = "HTTP 502"
    assert classify_provider_error(text) == "http_error"


def test_classify_provider_error_failure_phrase_token() -> None:
    """An explicit failure phrase returns exactly "failure_phrase"."""
    # "API call failed" matches _FAILURE_PHRASE_RE; no higher detector fires.
    text = "API call failed"
    assert classify_provider_error(text) == "failure_phrase"


def test_classify_provider_error_fallback_token() -> None:
    """A string with no sub-detector match returns "provider_error" (fallback)."""
    # The docstring says "should not occur" in practice, but the fallback branch is
    # real code. Call directly with a string that clears all four detectors to pin the
    # final return value. The function guarantees never returning an empty string.
    # (The caller is expected to have confirmed is_provider_error first; this call
    # exercises the fallback regardless of that precondition.)
    result = classify_provider_error("some unmatched string with no error markers")
    assert result == "provider_error"


def test_classify_provider_error_traceback_wins_over_provider_token() -> None:
    """When input matches both traceback AND provider_token, "traceback" is returned.

    Pins the detector ORDER: traceback > provider_token. A mutant that swaps these
    two detectors would return "provider_token" instead and fail this test.
    """
    # "Traceback (most recent call last):" matches _TRACEBACK_RE.
    # "ValueError:" matches _PROVIDER_TOKEN_RE (\b\w*error\b\s*:).
    # Both detectors fire; the higher-priority one (traceback) must win.
    text = "Traceback (most recent call last):\nValueError: something went wrong"
    assert classify_provider_error(text) == "traceback"


def test_classify_provider_error_provider_token_wins_over_http_error() -> None:
    """When input matches both provider_token AND http_error, "provider_token" wins.

    Pins the detector ORDER: provider_token > http_error. A mutant that swaps these
    two detectors would return "http_error" instead and fail this test.
    """
    # "InternalServerError: HTTP 502" — "InternalServerError:" fires _PROVIDER_TOKEN_RE
    # and "HTTP 502" fires _HTTP_ERROR_RE; provider_token must take priority.
    text = "InternalServerError: HTTP 502 Bad Gateway"
    assert classify_provider_error(text) == "provider_token"


def test_classify_provider_error_http_error_wins_over_failure_phrase() -> None:
    """When input matches both http_error AND failure_phrase, "http_error" is returned.

    Pins the detector ORDER: http_error > failure_phrase. A mutant that swaps these
    two detectors would return "failure_phrase" instead and fail this test.
    """
    # "API call failed: HTTP 502" — "API call failed" matches _FAILURE_PHRASE_RE and
    # "HTTP 502" matches _HTTP_ERROR_RE; _PROVIDER_TOKEN_RE does NOT fire (no
    # "error:" token, no structured token, no "status code" substring); http_error wins.
    text = "API call failed: HTTP 502"
    assert classify_provider_error(text) == "http_error"
