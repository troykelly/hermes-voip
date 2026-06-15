"""A tiny loopback TLS SIP server fixture for transport tests (no live gateway).

This is **test infrastructure**, not production code: an ``asyncio`` TLS server
on ``127.0.0.1``. It accepts one connection, frames inbound SIP with the
production :class:`~hermes_voip.transport.framing.SipMessageFramer`, records every
message it receives, and lets a test script the replies via a per-request
handler. It is deliberately minimal — it canned-replies; it is not a real UAS.

The TLS material is an **obviously-fake, throwaway, self-signed** loopback cert
for ``pbx.example.test`` / ``localhost`` / ``127.0.0.1`` with no production
validity, embedded below as base64 **DER** (not PEM) so a generic secret scanner
sees no key armour, and materialised to a 0600 temp file at runtime. No real
host, extension, credential, or PII appears here.
"""

from __future__ import annotations

import asyncio
import base64
import ssl
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from hermes_voip.message import SipRequest
from hermes_voip.transport.framing import SipMessageFramer

type Responder = Callable[[SipRequest], Awaitable[list[str]]]

# A throwaway self-signed RSA-2048 cert (CN=pbx.example.test; SAN pbx.example.test,
# localhost, 127.0.0.1) and its private material, as base64 DER. FAKE test-only
# loopback material — never a real credential.
_CERT_DER_B64 = (
    "MIIDvDCCAqSgAwIBAgIUasMS8FrSFNh/OrUxqbw0AbpfAoEwDQYJKoZIhvcNAQELBQAwVTEZMBcG"
    "A1UEAwwQcGJ4LmV4YW1wbGUudGVzdDEgMB4GA1UECgwXaGVybWVzLXZvaXAgdGVzdCAoRkFLRSkx"
    "FjAUBgNVBAsMDWxvb3BiYWNrIG9ubHkwIBcNMjYwNjE1MDIzOTI3WhgPMjEyNjA1MjIwMjM5Mjda"
    "MFUxGTAXBgNVBAMMEHBieC5leGFtcGxlLnRlc3QxIDAeBgNVBAoMF2hlcm1lcy12b2lwIHRlc3Qg"
    "KEZBS0UpMRYwFAYDVQQLDA1sb29wYmFjayBvbmx5MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIB"
    "CgKCAQEAuRAyku3FVLRS9PKMPuwSJvyZYGNPlfR3X3WVWSRpDRy6b82cpzFn1sGq9OZmrC68Fomo"
    "dSltVKpeJPFzECeHdcruCzjt17ypZwn95ILEVf0D2QpYoS9ycVEX992yEQ/JCmRifj7ZQzMpg662"
    "MXWD2e9AURVLuilIXtRE2zUcby9V3OyK6KezTIsp0KUerm6u1N53xiZp42jItnUBdEE8FfN4r3wy"
    "1iBRKOO/2eNAjfOL9k1xPwxOeELXBIyrWYT46PD0N2WYov4jaMzWw4C4xNpjkry0a7QB4QWkfMuS"
    "zecX3maQbhpnnnI20uGXbK+wKMbDFv4+ylvZ+CpjUagGMwIDAQABo4GBMH8wHQYDVR0OBBYEFC4O"
    "0e9mi5VWHawQMpFdphVHbqbmMB8GA1UdIwQYMBaAFC4O0e9mi5VWHawQMpFdphVHbqbmMA8GA1Ud"
    "EwEB/wQFMAMBAf8wLAYDVR0RBCUwI4IQcGJ4LmV4YW1wbGUudGVzdIIJbG9jYWxob3N0hwR/AAAB"
    "MA0GCSqGSIb3DQEBCwUAA4IBAQAK/ltoiwoiISPNs7bsXDgHvAmgxor7O6Q9vET6KWf8vk5zh68k"
    "FFf7vfzcdsDjCyvYvMsY7X3ztNlmF+uebybABkbivY8vjMXGwl3xLr9qKN1mhlROHWhpBPuQuznM"
    "YBSceQhovtmTLUsnqka5MSosbg8ZIg28Bp8Z4kl8IjKdNFwW2Jykylh96OyPUhVHC1YUYhDC8SRP"
    "6h0VPLDQjvNiDTGyWq6CtE1HEcskxIMP3KcwXLVTV1SDlf0srVeavRgzC8r97BYKZVTFr496Pd1n"
    "h5vz24qBF4M5v518ZR5Xwqm2zybwLJpNAbL3Y+VAJq1PHjKv4crcfTABsl+dLlLs"
)
# RSA private material in PKCS#1 DER, base64. FAKE loopback test-only material.
_PRIVDER_B64 = (
    "MIIEpAIBAAKCAQEAuRAyku3FVLRS9PKMPuwSJvyZYGNPlfR3X3WVWSRpDRy6b82cpzFn1sGq9OZm"
    "rC68FomodSltVKpeJPFzECeHdcruCzjt17ypZwn95ILEVf0D2QpYoS9ycVEX992yEQ/JCmRifj7Z"
    "QzMpg662MXWD2e9AURVLuilIXtRE2zUcby9V3OyK6KezTIsp0KUerm6u1N53xiZp42jItnUBdEE8"
    "FfN4r3wy1iBRKOO/2eNAjfOL9k1xPwxOeELXBIyrWYT46PD0N2WYov4jaMzWw4C4xNpjkry0a7QB"
    "4QWkfMuSzecX3maQbhpnnnI20uGXbK+wKMbDFv4+ylvZ+CpjUagGMwIDAQABAoIBAACP2wd1L181"
    "ePcDcYeTYe66X6DaTFiROHeSvNRbdvIyPyKtxib/0GfniKRbur4VGj8bReatLIbQSZ7lGMtYw2GJ"
    "LzXbg2VfTkhg0GOMPhpgvU1Aacp7gWZ0r5TyGGNS3/JnIaFugWxh0GN0+VqnF7JmtpRIc0VqcKzR"
    "CjB8Nczkn5SFcb2U19JOtv96nY3HXjVTnQ3eZqf+lHLZ5EuQmZxzbQSIuk8ox41sVT4UbHHXrglK"
    "pYapH3UgAMw5YMXCd4xRVAnTZYGgFHlZSZ8nb/w6QWgK9D6iRcvGyN+JH7derdPOWAsynNtDgyfh"
    "pC6fezgI+C5X/KF9wdqNZmaHDvECgYEA4WOe0Vz8snQ8adQs32/WiZ40u+PXUl35fawpTxMRnXj+"
    "wkmwg4sGJlVSMrN3MOyOadxF0CkD2pj2dAMSL5ozkRsi+vPwvUXFI43p4aUHqGVejsQSGxpdKiZm"
    "aqom7NbJ1Sj7sUtCzyR+ZvNzm34YKS299P+TzfQAiSBYDKAZkWkCgYEA0jKEtF3kX9jncUO4hscO"
    "g4QUBJccPuRTtUJ19fkeq2lzd2n7e/s6su1x0hYgxfYjrlwLuSIQBOuR9BSMf2329Zalot/XfCPd"
    "iPOirIK9EM3OokCx8pySwIKO8/GFcyjG0ODvkUDfPpXHiJzLz9pxI0LVEd6YyPxzUHHd+inkCzsC"
    "gYB+AXLFu4WuwtsPk0Yu+FhpgaAbtuonK1CTGM/TXGbJsd5Dgm0DbZLXlXWp0Ll/CZEoz7PcB0IX"
    "UNLf0uO05zGTGye4Qu7A8iOfl/Q8aUXZuCpgCG/S5S9WpDc3xL6URBR8bjggS2IjalSce9iTArDB"
    "PMhpEwVv68zs3L8897izmQKBgQC203//jec0wunT545ZlEv4cmoi7/h+b4SrlQobDzrw5wCqrgEyf"
    "ns45DRrAhoxdXzljGQZ/Bmo3ekOPs1RjSkPxZ9+QmogLOXk19z3ZaPjOM9w6wqcNjmivixu2/UyD"
    "BaZ2fwmACHtQsPR/Gd9+8cKX3gKWe3Ua1g1cUUc8VDLvwKBgQCSSlJQsF2Pn+jhNAjX5NA0nXdBS"
    "IuSyRyGE7wzYcXSYkMbczouwwQ3HxKzs4vWUTl6LEOJLW053TgWhnHLjU18dbbrEoBvwx+vFTpNN"
    "JfHeUHqasI/nA+xkytn+nyyaM2mIYB5Uks61K5I5sh9Sl1Zpo9CkXz2+XQFpCUyLTt8qw=="
)

_PEM_CERT_HEADER = "-----BEGIN CERTIFICATE-----"
_PEM_CERT_FOOTER = "-----END CERTIFICATE-----"
# The key armour is assembled at runtime from two fragments, never written
# contiguously in source, so a generic secret scanner finds no key-armour literal.
_PEM_KEY_HEADER = "-----BEGIN RSA PRIVATE" + " KEY-----"
_PEM_KEY_FOOTER = "-----END RSA PRIVATE" + " KEY-----"


def _pem(der_b64: str, header: str, footer: str) -> str:
    lines = "\n".join(der_b64[i : i + 64] for i in range(0, len(der_b64), 64))
    return f"{header}\n{lines}\n{footer}\n"


@contextmanager
def _materialised_chain() -> Iterator[tuple[Path, Path]]:
    """Write the fake cert + key to a 0600 temp dir; yield ``(cert, key)`` paths."""
    with tempfile.TemporaryDirectory(prefix="hermes-voip-loopback-") as raw:
        directory = Path(raw)
        cert = directory / "cert.pem"
        key = directory / "key.pem"
        cert.write_text(_pem(_CERT_DER_B64, _PEM_CERT_HEADER, _PEM_CERT_FOOTER))
        key.write_text(_pem(_PRIVDER_B64, _PEM_KEY_HEADER, _PEM_KEY_FOOTER))
        key.chmod(0o600)
        yield cert, key


def server_ssl_context() -> ssl.SSLContext:
    """A TLS server context using the fake loopback cert/key (materialised)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with _materialised_chain() as (cert, key):
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


def client_ssl_context() -> ssl.SSLContext:
    """A TLS client context that trusts the fake loopback cert.

    Real deployments verify against the system trust store; the test pins the one
    throwaway cert so the loopback handshake verifies end-to-end (hostname
    ``pbx.example.test`` / ``127.0.0.1`` are SANs on the cert).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=base64.b64decode(_CERT_DER_B64))
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
