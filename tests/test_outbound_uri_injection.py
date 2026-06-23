"""Outbound Request-URI injection hardening (RELEASE BLOCKER #4, security).

The outbound dial Request-URI is built by interpolating an AGENT-SUPPLIED
number/extension into ``sip:{extension}@{host}`` (``adapter.py`` ~line 1601).
Before this guard, the only protection was the C0/DEL control-char reject in
``message.build_request`` — which catches CR/LF/NUL injection but NOT ``@``,
whitespace, or SIP URI metacharacters (semicolon, question-mark, angle brackets,
comma, quote, slash, backslash). An agent (or a caller influencing the value)
could therefore inject into
the Request-URI to redirect the call to an arbitrary destination
(``1001@evil.com``) or smuggle URI parameters/headers (``1001;foo=bar``,
``1001?Header=x``).

This complements ``HERMES_VOIP_OUTBOUND_ALLOW`` (which gates WHICH destinations are
permitted): this gates the SHAPE of the user-part so a permitted-looking value can
never carry injection metacharacters into the URI.
"""

from __future__ import annotations

import pytest

from hermes_voip.adapter import _validate_dialable_target

# Targets that must be REJECTED — each carries an injection metacharacter that would
# corrupt the ``sip:{target}@{host}`` Request-URI (host hijack, param/header smuggle,
# or CRLF/control injection).
_MALICIOUS_TARGETS: tuple[str, ...] = (
    "1001@evil.com",  # host hijack — dial an arbitrary destination
    "1001 ",  # trailing whitespace
    " 1001",  # leading whitespace
    "10 01",  # interior whitespace
    "1001;transport=tcp",  # URI parameter smuggle
    "1001?Replaces=abc",  # header smuggle
    "1001,2002",  # multi-target / list injection
    'sip:"x"@h',  # quoted string + scheme
    "1001<2002",  # angle bracket
    "1001>2002",  # angle bracket
    "1001/2002",  # slash
    "1001\\2002",  # backslash
    "1001\r\nINVITE",  # CRLF injection
    "1001\nfoo",  # bare LF
    "1001\x00",  # NUL control char
    "1001\x7f",  # DEL control char
    "",  # empty
    "+",  # bare plus with no digits
    "abc",  # non-dialable letters
)

# Targets that must be ACCEPTED — ordinary dialable numbers/extensions.
_VALID_TARGETS: tuple[str, ...] = (
    "1000",  # plain extension
    "1001",
    "+14155550123",  # E.164-ish with leading '+'
    "0011441234567890",  # international dialled digits
    "*99",  # DTMF-style feature code
    "#1",
    "100*",
    "100#",
    "+1",
)


@pytest.mark.parametrize("target", _MALICIOUS_TARGETS)
def test_validate_dialable_target_rejects_injection(target: str) -> None:
    """Every injection-bearing target raises ValueError (no URI interpolation)."""
    with pytest.raises(ValueError, match="dial target"):
        _validate_dialable_target(target)


@pytest.mark.parametrize("target", _VALID_TARGETS)
def test_validate_dialable_target_accepts_dialable(target: str) -> None:
    """Ordinary dialable numbers/extensions pass the grammar unchanged."""
    _validate_dialable_target(target)  # must not raise
