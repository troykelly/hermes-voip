"""A tiny loopback TLS SIP server fixture for transport tests (no live gateway).

This is **test infrastructure**, not production code: an ``asyncio`` TLS server
on ``127.0.0.1``. It accepts one connection, frames inbound SIP with the
production :class:`~hermes_voip.transport.framing.SipMessageFramer`, records every
message it receives, and lets a test script the replies via a per-request
handler. It is deliberately minimal — it canned-replies; it is not a real UAS.

The TLS material is an **obviously-fake, throwaway, self-signed** loopback cert
for ``pbx.example.test`` / ``localhost`` / ``127.0.0.1`` with no production
validity. It is generated **ephemerally at test time** with the ``openssl`` CLI
into a per-session temp directory and is **never committed** — so no private-key
material lives in the repo (a committed key would also trip secret scanning). The
test is skipped if ``openssl`` is unavailable. No real host, extension,
credential, or PII appears here.
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import ssl
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from functools import cache
from pathlib import Path

import pytest

from hermes_voip.message import SipRequest
from hermes_voip.transport.framing import SipMessageFramer

type Responder = Callable[[SipRequest], Awaitable[list[str]]]


def _ensure_openssl() -> str:
    """Return the ``openssl`` executable path, or skip the test if it is absent."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl CLI not available; cannot mint the loopback test cert")
    return openssl


@cache
def _generate_chain() -> tuple[Path, Path]:
    """Mint a throwaway self-signed loopback cert+key with openssl (once/session).

    The directory is created with ``mkdtemp`` (0700) and removed at process exit;
    the key file is written by openssl. Nothing is committed. ``@cache`` runs the
    openssl call once so it does not repeat per test.
    """
    openssl = _ensure_openssl()
    directory = Path(tempfile.mkdtemp(prefix="hermes-voip-loopback-"))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    subprocess.run(  # noqa: S603 — fixed argv (no shell), openssl path from shutil.which
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=pbx.example.test/O=hermes-voip test (FAKE)",
            "-addext",
            "subjectAltName=DNS:pbx.example.test,DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return (cert, key)


def server_ssl_context() -> ssl.SSLContext:
    """A TLS server context using the ephemeral fake loopback cert/key."""
    cert, key = _generate_chain()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


def client_ssl_context() -> ssl.SSLContext:
    """A TLS client context that trusts the ephemeral fake loopback cert.

    Real deployments verify against the system trust store; the test pins the one
    throwaway cert so the loopback handshake verifies end-to-end (hostname
    ``pbx.example.test`` / ``127.0.0.1`` are SANs on the cert).
    """
    cert, _key = _generate_chain()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=str(cert))
    return ctx


class LoopbackSipServer:
    """An asyncio TLS server that frames inbound SIP and canned-replies."""

    def __init__(self, responder: Responder) -> None:
        """Bind nothing yet; ``responder`` maps each request to reply texts."""
        self._responder = responder
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = asyncio.Event()
        self.received: list[str] = []
        self._received_event = asyncio.Event()

    @property
    def port(self) -> int:
        """The ephemeral port the server bound to (valid after :meth:`start`)."""
        if self._server is None:
            msg = "server not started"
            raise RuntimeError(msg)
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        """Start listening on an ephemeral ``127.0.0.1`` TLS port."""
        self._server = await asyncio.start_server(
            self._handle, host="127.0.0.1", port=0, ssl=server_ssl_context()
        )

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        framer = SipMessageFramer()
        self._writer = writer
        self._connected.set()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                framer.feed(data)
                for raw in framer:
                    self.received.append(raw)
                    self._received_event.set()
                    if raw.startswith("SIP/2.0 "):
                        continue  # a response from the client; nothing to reply
                    request = SipRequest.parse(raw)
                    for reply in await self._responder(request):
                        writer.write(reply.encode())
                    await writer.drain()
        finally:
            writer.close()

    async def push(self, message: str, *, timeout: float = 3.0) -> None:
        """Send an unsolicited message to the connected client (e.g. an INVITE)."""
        await asyncio.wait_for(self._connected.wait(), timeout)
        if self._writer is None:  # pragma: no cover - guarded by the event
            msg = "no client connected"
            raise RuntimeError(msg)
        self._writer.write(message.encode())
        await self._writer.drain()

    async def push_bytes(self, data: bytes, *, timeout: float = 3.0) -> None:
        """Send RAW bytes to the connected client (may be non-UTF-8).

        :meth:`push` encodes a ``str``; a well-framed SIP message whose BODY is
        binary and not valid UTF-8 (an ``application/ISUP`` / ``octet-stream`` /
        Latin-1 payload) cannot be expressed as ``str``, so this writes the exact
        bytes onto the wire to exercise the framer's non-UTF-8-body handling.
        """
        await asyncio.wait_for(self._connected.wait(), timeout)
        if self._writer is None:  # pragma: no cover - guarded by the event
            msg = "no client connected"
            raise RuntimeError(msg)
        self._writer.write(data)
        await self._writer.drain()

    async def wait_for_received(
        self, predicate: Callable[[str], bool], *, timeout: float = 3.0
    ) -> str:
        """Wait until a received message matches ``predicate``; return it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for raw in self.received:
                if predicate(raw):
                    return raw
            remaining = deadline - loop.time()
            if remaining <= 0:
                msg = "no received message matched within the timeout"
                raise TimeoutError(msg)
            self._received_event.clear()
            try:
                await asyncio.wait_for(self._received_event.wait(), remaining)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        """Close the listening socket and wait for it to wind down."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
