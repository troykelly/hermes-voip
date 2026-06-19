"""Tests for the provider/runtime-error detector + safe spoken reply (ADR-0063).

Pure, dependency-free module — covered by the default ``mypy --strict`` + pytest
gate (no hermes-runtime import). The detector recognises a backend/provider error
turn so the voip adapter speaks a short safe apology instead of reading a raw
``HTTP 502`` / stack-trace / provider message aloud to the caller (LAUNCH #4).
"""

from __future__ import annotations

import pytest

from hermes_voip.provider_error import is_provider_error, safe_error_reply

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
