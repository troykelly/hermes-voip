r"""RED tests: RFC 5626 double-CRLF keepalive ping written by SipOverTlsTransport.

These are the *writer-side* keepalive tests — the transport sends b"\r\n\r\n" to
the peer at a configured interval.  Contrast with the *framer-side* tests
(test_framing.py) and the OPTIONS-response keepalive tests (test_keepalive.py).

All fakes only; no real network, no real TLS.  Credentials are obvious fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_voip.transport.connection import SipOverTlsTransport

pytestmark = pytest.mark.asyncio


class _CapturingWriter:
    """Minimal fake asyncio.StreamWriter that records bytes written to it."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self._closed = False

    def write(self, data: bytes) -> None:
        """Capture the written bytes."""
        self.written.append(data)

    async def drain(self) -> None:
        """No-op drain."""

    def close(self) -> None:
        """Mark closed."""
        self._closed = True

    async def wait_closed(self) -> None:
        """No-op wait."""

    def get_extra_info(self, key: str, default: object = None) -> object:
        """Return a fake local address for sent-by derivation."""
        if key == "sockname":
            return ("127.0.0.1", 5061)
        return default


class _RaisingWriter(_CapturingWriter):
    """Writer that raises OSError on every write (simulates a broken connection)."""

    def write(self, data: bytes) -> None:
        """Always raise to simulate a connection reset."""
        raise OSError("connection reset")


async def _make_transport(
    writer: _CapturingWriter,
    *,
    keepalive_interval: float = 0.05,
    on_connection_lost: object = None,
) -> SipOverTlsTransport:
    """Build and connect a SipOverTlsTransport against the fake writer."""
    reader = asyncio.StreamReader()
    # Patch asyncio.open_connection to return our fake pair.
    with patch(
        "hermes_voip.transport.connection.asyncio.open_connection",
        new_callable=AsyncMock,
        return_value=(reader, writer),
    ):
        transport = SipOverTlsTransport(
            host="pbx.example.test",
            port=5061,
            ssl_context=MagicMock(),
            keepalive_interval=keepalive_interval,
            on_connection_lost=on_connection_lost,  # type: ignore[arg-type]
        )
        await transport.connect()
    return transport


@pytest.mark.asyncio
async def test_keepalive_ping_written_within_interval() -> None:
    """A double-CRLF ping must be written to the transport within keepalive_interval."""
    writer = _CapturingWriter()
    transport = await _make_transport(writer, keepalive_interval=0.05)
    try:
        # Wait up to 3x the interval for at least one ping to appear.
        deadline = asyncio.get_event_loop().time() + 0.15
        while b"\r\n\r\n" not in b"".join(writer.written):
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(0.01)
        all_written = b"".join(writer.written)
        assert b"\r\n\r\n" in all_written, (
            f"expected double-CRLF keepalive ping within 0.15s; got {all_written!r}"
        )
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_keepalive_write_failure_triggers_connection_lost() -> None:
    """An OSError during a keepalive write must invoke on_connection_lost."""
    lost_exc: list[BaseException | None] = []

    def _on_lost(exc: BaseException | None) -> None:
        lost_exc.append(exc)

    writer = _RaisingWriter()
    transport = await _make_transport(
        writer, keepalive_interval=0.05, on_connection_lost=_on_lost
    )
    try:
        # Wait for the keepalive loop to fire and trigger the lost callback.
        deadline = asyncio.get_event_loop().time() + 0.5
        while not lost_exc:
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(0.01)
        assert lost_exc, "on_connection_lost was not called after keepalive OSError"
    finally:
        # aclose may itself see a closed writer; suppress gracefully.
        with contextlib.suppress(Exception):
            await transport.aclose()


@pytest.mark.asyncio
async def test_aclose_cancels_keepalive_task() -> None:
    """After aclose(), no further pings must be written to the writer."""
    writer = _CapturingWriter()
    transport = await _make_transport(writer, keepalive_interval=0.05)

    # Let one ping land so we know the loop was running.
    deadline = asyncio.get_event_loop().time() + 0.2
    while b"\r\n\r\n" not in b"".join(writer.written):
        if asyncio.get_event_loop().time() >= deadline:
            break
        await asyncio.sleep(0.01)

    await transport.aclose()

    # Record bytes written up to this point.
    count_after_close = len(writer.written)

    # Sleep long enough for another interval to have passed if the task were
    # still alive.
    await asyncio.sleep(0.12)

    assert len(writer.written) == count_after_close, (
        "keepalive task continued writing after aclose(); "
        f"{len(writer.written) - count_after_close} extra write(s) detected"
    )
