"""Recognise a provider/runtime *error* reply so it is never read aloud (ADR-0063).

The Hermes runtime hands the agent's turn text to a platform adapter's ``send()``.
On an unrecoverable backend failure the *reply* the adapter receives can be the
raw error itself — an HTTP ``502``/``503``, a provider error class
(``overloaded_error``), or a Python traceback. On a text platform that renders as
a chat line; on ``voip`` it is SYNTHESISED AS TTS and **spoken to the caller**:
unprofessional, and an information leak about the backend.

This module is the principled stand-in (mirroring :mod:`hermes_voip.notice_filter`).
The gateway's own ``_sanitize_gateway_final_response`` maps provider errors to a
safe reply only for ``platform == "telegram"`` and returns raw text unchanged for
every other platform, so the voip adapter must detect and replace the error at its
``send()``/speak seam itself. :func:`is_provider_error` recognises the error by
strong, structural error hallmarks; :func:`safe_error_reply` returns a short,
language-appropriate apology to speak instead. The real error is still logged
(redacted) by the caller and propagates — this module only decides what the caller
*hears*.

Conservative by design (rule 27 / rule 19): a genuine conversational reply that
merely *mentions* a number, the word "error", or a service as ordinary words
("Your reference is 502.", "There was an error on your form.") is NOT an error and
is spoken unchanged — over-matching would silence the agent. Pure, dependency-free,
and covered by the default ``mypy --strict`` + pytest gate.
"""

from __future__ import annotations

import re

# --- error detectors --------------------------------------------------------
#
# Each pattern is a STRONG error signal — an HTTP server-error status *in an error
# context*, a provider/library error-class token, an explicit failure phrase, or a
# Python traceback header. A bare number or the lone word "error" deliberately does
# NOT match (a genuine reply uses those innocently).

# HTTP error STATUS codes (5xx server errors + 429 rate-limit) — only in an error
# context, never a bare number. A real raw error frames a status one of two ways,
# neither of which a genuine conversational reply produces. (1) An http/status/error
# keyword sits next to the code, tolerating an "HTTP/1.1 " version prefix and a
# "status code" / "status=" / "error code:" framing — HTTP 502, HTTP/1.1 502, status
# code 500, status: 429, error code: 502. (2) A raw status LINE leads the message:
# the code immediately followed by its canonical reason phrase — 502 Bad Gateway,
# 429 Too Many Requests. A genuine reply matches neither: one that merely mentions a
# code mid-sentence (a caller naming a 503 Service Unavailable page), that LEADS with
# a code which is NOT a reason phrase ("503 people are waiting", "429 confirmed
# guests"), or that names a bare reference number (reference 502, room 500).
_HTTP_STATUS = r"(?:5\d{2}|429)"
# Canonical HTTP reason phrases for the 5xx/429 codes — what distinguishes a raw
# status line ("502 Bad Gateway") from a reply that merely LEADS with the number.
_HTTP_REASON = (
    r"(?:internal\s+server\s+error|not\s+implemented|bad\s+gateway"
    r"|service\s+unavailable|gateway\s+time-?out|http\s+version\s+not\s+supported"
    r"|variant\s+also\s+negotiates|insufficient\s+storage|loop\s+detected"
    r"|not\s+extended|network\s+authentication\s+required|too\s+many\s+requests)"
)
_HTTP_ERROR_RE = re.compile(
    # 1. an http/status/error keyword next to the code (digit-free separators so the
    #    status digits are never eaten by the gap matcher).
    rf"\b(?:https?(?:/\d(?:\.\d)?)?|status(?:\s+code)?|error(?:\s+code)?)\b"
    rf"[\s:=]*{_HTTP_STATUS}\b"
    # 2. a raw status line leading the message: code + its canonical reason phrase.
    rf"|^\s*{_HTTP_STATUS}\s+{_HTTP_REASON}\b",
    re.IGNORECASE,
)

# Provider / SDK error-class + structured error-code tokens. These appear in raw
# provider responses and exception reprs; natural speech never produces them.
_PROVIDER_TOKEN_RE = re.compile(
    r"\b\w*error\b\s*:"  # "APIError:", "InternalServerError:", "RuntimeError:"
    r"|\b(?:overloaded_error|rate_limit_error|api_error|internal_error"
    r"|server_error|invalid_request_error|authentication_error"
    r"|permission_error|not_found_error|overloaded)\b"
    r"|\bstatus[\s_]?code\b"
    r"|\bupstream\s+connect\s+error\b",
    re.IGNORECASE,
)

# Explicit failure phrasing a backend/gateway emits as the turn text.
_FAILURE_PHRASE_RE = re.compile(
    r"\b(?:api\s+call\s+failed|request\s+failed|call\s+failed"
    r"|failed\s+with\s+status|the\s+server\s+had\s+an\s+error"
    r"|an\s+(?:internal\s+)?error\s+occurred)\b",
    re.IGNORECASE,
)

# A Python traceback header — the unmistakable shape of a leaked stack trace.
_TRACEBACK_RE = re.compile(
    r"traceback\s*\(most\s+recent\s+call\s+last\)", re.IGNORECASE
)


def is_provider_error(content: str) -> bool:
    """Return ``True`` when ``content`` is a provider/runtime error, not a reply.

    Recognises the shapes an unrecoverable backend failure surfaces as the agent's
    "reply" text — an HTTP 5xx in an error context, a provider/SDK error-class or
    error-code token, an explicit failure phrase, or a Python traceback header — so
    the voip adapter speaks a safe line (:func:`safe_error_reply`) instead of
    reading the raw error aloud to the caller.

    Intentionally conservative: a genuine reply that merely mentions a number, the
    word "error", or a service in passing is not an error and is spoken unchanged.
    """
    return bool(
        _TRACEBACK_RE.search(content)
        or _FAILURE_PHRASE_RE.search(content)
        or _PROVIDER_TOKEN_RE.search(content)
        or _HTTP_ERROR_RE.search(content)
    )


# --- the safe spoken reply --------------------------------------------------
#
# Short, natural apologies keyed by language code (ADR-0054 mechanism). Each reads
# cleanly on every TTS model (no bracket tag) and reveals nothing about the
# backend. English-only for now; add a language by adding an entry (data-only),
# mirroring _COMFORT_FILLER_PHRASES_BY_LANGUAGE in config.py.
_SAFE_ERROR_REPLY_BY_LANGUAGE: dict[str, str] = {
    "en": "Sorry, I'm having trouble right now. Please bear with me.",
}

#: The English line is the back-compatible default for an unknown language code.
_DEFAULT_LANGUAGE = "en"


def safe_error_reply(language: str) -> str:
    """Return a short, safe spoken apology for ``language`` (ADR-0063).

    The line is what the caller HEARS in place of a provider/runtime error: a brief
    apology with no backend detail. An unknown ``language`` falls back to English
    (never raises) — a missing translation must never break the call.
    """
    return _SAFE_ERROR_REPLY_BY_LANGUAGE.get(
        language, _SAFE_ERROR_REPLY_BY_LANGUAGE[_DEFAULT_LANGUAGE]
    )
