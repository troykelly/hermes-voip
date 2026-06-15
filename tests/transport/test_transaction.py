"""Tests for the INVITE client transaction (RFC 3261 §17.1).

This layer owns the ACK for a **non-2xx final** response to an INVITE: that ACK
is generated in the client transaction, on the **same branch** as the INVITE,
and is *not* the transaction-user's job (the TU emits only the §13.2.2.4 2xx
ACK). The state machine is modelled (Calling → Proceeding → Completed →
Terminated); over a reliable transport (TLS) request retransmission (Timer A) is
disabled, so these tests focus on the response side and the ACK construction.

Fakes only — ``pbx.example.test``, ``127.0.0.1``/``198.51.100.x``.
"""

from __future__ import annotations

import pytest

from hermes_voip.message import SipResponse
from hermes_voip.transport.transaction import (
    InviteClientTransaction,
    TransactionState,
)

_INVITE = (
    "INVITE sip:2000@198.51.100.99:5061;transport=tls SIP/2.0\r\n"
    "Via: SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bKtxn1;rport\r\n"
    "Max-Forwards: 70\r\n"
    "From: <sip:1000@pbx.example.test>;tag=ours\r\n"
    "To: <sip:2000@pbx.example.test>\r\n"
    "Call-ID: call-xyz\r\n"
    "CSeq: 314 INVITE\r\n"
    "Contact: <sip:1000@198.51.100.7:5061;transport=tls>\r\n"
    "Content-Length: 0\r\n\r\n"
)


def _response(status: int, reason: str, *, to_tag: str | None = "peer") -> SipResponse:
    to = "<sip:2000@pbx.example.test>"
    if to_tag is not None:
        to = f"{to};tag={to_tag}"
    return SipResponse.parse(
        f"SIP/2.0 {status} {reason}\r\n"
        "Via: SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bKtxn1;rport\r\n"
        "From: <sip:1000@pbx.example.test>;tag=ours\r\n"
        f"To: {to}\r\n"
        "Call-ID: call-xyz\r\n"
        "CSeq: 314 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def test_initial_state_is_calling() -> None:
    txn = InviteClientTransaction(_INVITE)
    assert txn.state is TransactionState.CALLING


def test_provisional_moves_to_proceeding_and_is_not_acked() -> None:
    txn = InviteClientTransaction(_INVITE)
    ack = txn.ack_for_response(_response(180, "Ringing", to_tag=None))
    assert ack is None
    assert txn.state is TransactionState.PROCEEDING


@pytest.mark.parametrize(
    ("status", "reason"),
    [(486, "Busy Here"), (404, "Not Found"), (500, "Server Error"), (603, "Decline")],
)
def test_non_2xx_final_is_acked_and_transaction_completes(
    status: int, reason: str
) -> None:
    txn = InviteClientTransaction(_INVITE)
    ack = txn.ack_for_response(_response(status, reason))
    assert ack is not None
    assert txn.state is TransactionState.COMPLETED
    assert ack.startswith("ACK sip:2000@198.51.100.99:5061;transport=tls SIP/2.0")


def test_non_2xx_ack_uses_the_invite_branch_call_id_and_cseq() -> None:
    # RFC 3261 §17.1.1.3: the ACK uses the SAME branch as the INVITE (it belongs
    # to the same client transaction), same Call-ID, and the INVITE's CSeq number
    # with method ACK.
    from hermes_voip.message import SipRequest  # noqa: PLC0415 — local to this test

    txn = InviteClientTransaction(_INVITE)
    ack_text = txn.ack_for_response(_response(404, "Not Found"))
    assert ack_text is not None
    ack = SipRequest.parse(ack_text)
    assert ack.method == "ACK"
    via = ack.header("Via")
    assert via is not None
    assert "branch=z9hG4bKtxn1" in via
    assert ack.header("Call-ID") == "call-xyz"
    assert ack.header("CSeq") == "314 ACK"
    assert ack.header("Max-Forwards") == "70"


def test_non_2xx_ack_to_uses_the_response_to_tag_and_from_keeps_our_tag() -> None:
    # RFC 3261 §17.1.1.3: the ACK To (incl. tag) is copied from the response being
    # acknowledged; the From is the INVITE's From (with our tag), unchanged.
    from hermes_voip.message import SipRequest  # noqa: PLC0415 — local to this test

    txn = InviteClientTransaction(_INVITE)
    ack_text = txn.ack_for_response(_response(486, "Busy Here", to_tag="server-tag"))
    assert ack_text is not None
    ack = SipRequest.parse(ack_text)
    to = ack.header("To")
    from_ = ack.header("From")
    assert to is not None
    assert from_ is not None
    assert "tag=server-tag" in to
    assert "tag=ours" in from_


def test_2xx_final_is_not_acked_by_this_layer() -> None:
    # The 2xx ACK is the transaction user's job (a fresh transaction, §13.2.2.4),
    # so the client transaction returns None and terminates.
    txn = InviteClientTransaction(_INVITE)
    ack = txn.ack_for_response(_response(200, "OK"))
    assert ack is None
    assert txn.state is TransactionState.TERMINATED


def test_retransmitted_non_2xx_final_is_re_acked_idempotently() -> None:
    # In the Completed state a retransmitted final response must be answered with
    # the SAME ACK again (RFC 3261 §17.1.1.2), absorbing the retransmission.
    txn = InviteClientTransaction(_INVITE)
    first = txn.ack_for_response(_response(486, "Busy Here"))
    second = txn.ack_for_response(_response(486, "Busy Here"))
    assert first is not None
    assert first == second
    assert txn.state is TransactionState.COMPLETED


def test_provisional_after_provisional_stays_in_proceeding() -> None:
    txn = InviteClientTransaction(_INVITE)
    txn.ack_for_response(_response(100, "Trying", to_tag=None))
    txn.ack_for_response(_response(180, "Ringing", to_tag=None))
    assert txn.state is TransactionState.PROCEEDING


def test_final_after_provisional_acks_when_non_2xx() -> None:
    txn = InviteClientTransaction(_INVITE)
    assert txn.ack_for_response(_response(180, "Ringing", to_tag=None)) is None
    ack = txn.ack_for_response(_response(487, "Request Terminated"))
    assert ack is not None
    assert txn.state is TransactionState.COMPLETED
