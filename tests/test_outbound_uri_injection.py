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

from unittest.mock import MagicMock

import pytest

# The adapter imports the real Hermes base at module top (``gateway.platforms.base``
# / ``gateway.config``), an OPTIONAL extra absent from the default install. Skip the
# whole module when the runtime is absent — exactly like the sibling adapter tests —
# so the default gate yields a clean SKIP, not a collection error. The dedicated
# ``hermes-contract`` CI job installs the extra so these actually run there (rule 26).
pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_voip.adapter import VoipAdapter, _validate_dialable_target
from hermes_voip.outbound_allow import load_outbound_allowlist


@pytest.fixture
def _register_voip_platform() -> None:
    """Register a throwaway "voip" entry so ``Platform("voip")`` resolves.

    ``VoipAdapter.__init__`` resolves ``Platform(_PLATFORM_NAME)`` against the
    module-singleton ``platform_registry``; without a registered "voip" entry the
    enum lookup raises ``ValueError`` during construction (mirrors the autouse
    fixture in ``tests/test_adapter.py``).
    """
    if not platform_registry.is_registered("voip"):
        platform_registry.register(
            PlatformEntry(
                name="voip",
                label="VoIP",
                adapter_factory=lambda cfg: MagicMock(),
                check_fn=lambda: True,
                validate_config=lambda cfg: True,
                required_env=[],
                install_hint="",
                source="plugin",
            )
        )


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


# The injection metacharacters that most directly corrupt the outbound Request-URI:
# host hijack, URI-parameter smuggle, header smuggle, and CRLF request injection.
_PLACE_CALL_INJECTIONS: tuple[str, ...] = (
    "1001@evil.com",  # host hijack — redirect the call to an arbitrary destination
    "1001;transport=tcp",  # URI parameter smuggle
    "1001?Replaces=abc",  # header smuggle
    "1001\r\nINVITE sip:x@y SIP/2.0",  # CRLF request injection
)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", _PLACE_CALL_INJECTIONS)
@pytest.mark.usefixtures("_register_voip_platform")
async def test_place_call_rejects_injection_before_dialing(target: str) -> None:
    """``place_call`` itself refuses an injection-bearing target (the WIRED guard).

    This proves the guard is wired into the real dial chokepoint — not merely that
    the helper exists. ``_validate_dialable_target`` runs as the FIRST statement of
    ``place_call``, before any transport/manager/SDP work, so a malicious target
    raises ``ValueError`` here on an adapter that has not even ``connect()``-ed.
    Were the guard removed, an unprivileged value would instead fall through to the
    UAC body (which, with no transport, raises ``RuntimeError`` — a DIFFERENT type),
    so this ``ValueError`` expectation fails: the test has teeth against the call
    site, not just the helper.
    """
    adapter = VoipAdapter(PlatformConfig(enabled=True, extra={}))
    with pytest.raises(ValueError, match="dial target"):
        await adapter.place_call(target)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_register_voip_platform")
async def test_allowlisted_uri_target_still_fails_shape_guard() -> None:
    """An allowlisted URI-shaped target still fails the dial-chokepoint shape guard.

    The outbound allowlist answers WHETHER a configured target is permitted. It must
    not disable the independent Request-URI shape guard that rejects injection-bearing
    targets before the adapter interpolates them into ``sip:{target}@host``. Here the
    exact injection-shaped string is allowlisted, so it CLEARS the allowlist gate, yet
    ``place_call`` still refuses it on shape.

    (``OUTBOUND_ALLOW`` is exact for URI-shaped entries -- ``*`` is a literal dial
    character, ADR-0029 -- so the target is allowlisted verbatim, not via a glob.)
    """
    adapter = VoipAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._outbound_allow = load_outbound_allowlist(
        {"HERMES_VOIP_OUTBOUND_ALLOW": "1001@evil.example.test"}
    )

    with pytest.raises(ValueError, match="dial target"):
        await adapter.place_call_with_objective(
            "1001@evil.example.test",
            "fake objective",
        )
