"""Real actuation tests for the multi-intercom WEBHOOK opening (ADR-0045, #38).

These tests exercise the ACTUAL HTTP request path
(:func:`hermes_voip.multi_intercom.fire_webhook_opening` ->
``_fire_webhook_blocking``) against a real local ``http.server`` bound to
``127.0.0.1`` — NOT a fake of the function. They prove:

* a 2xx response succeeds (no raise);
* a non-2xx response (500) raises :class:`WebhookError` carrying only the status;
* a network error (connection refused) raises :class:`WebhookError`;
* a ``GET`` opening sends NO request body (the body is suppressed on the wire);
* a 3xx redirect (e.g. https->http downgrade) is REFUSED, not followed — the
  Authorization header / body must never travel to a redirected (possibly
  cleartext) endpoint (ADR-0045 https-only guarantee).

PUBLIC repo: a loopback ``http.server`` on ``127.0.0.1`` is an obvious local fake,
not a real endpoint. The webhook opening's https-at-load guard is bypassed here by
constructing the :class:`Opening` directly (the load-time validation is covered in
``test_multi_intercom.py``); these tests target the SEND path.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hermes_voip.multi_intercom import (
    Opening,
    OpeningType,
    WebhookError,
    fire_webhook_opening,
)

_BODY = '{"action":"open"}'


class _RecordingHandler(BaseHTTPRequestHandler):
    """A loopback handler that records the last request + replies per-path.

    Paths:
    * ``/ok``        -> 200 (success)
    * ``/created``   -> 201 (a non-200 2xx still succeeds)
    * ``/fail``      -> 500 (a non-2xx -> WebhookError)
    * ``/redirect``  -> 302 to ``/ok`` (a redirect that, if FOLLOWED, would 200 —
      proving refusal, not an unreachable target)
    """

    # Class-level capture (a single server thread, one request per test).
    last_method: str | None = None
    last_body: bytes = b""
    request_count: int = 0

    def _capture(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        type(self).last_method = self.command
        type(self).last_body = self.rfile.read(length) if length else b""
        type(self).request_count += 1

    def _respond(self) -> None:
        self._capture()
        if self.path == "/ok":
            self.send_response(200)
            self.end_headers()
        elif self.path == "/created":
            self.send_response(201)
            self.end_headers()
        elif self.path == "/fail":
            self.send_response(500)
            self.end_headers()
        elif self.path == "/redirect":
            # A 302 to /ok on this SAME server: if urlopen FOLLOWS it the second
            # request lands on /ok (200) and the call would wrongly succeed. The
            # send path must refuse the redirect instead (proving https-only is not
            # silently downgraded via a Location header).
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def do_PUT(self) -> None:
        self._respond()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Silence the per-request stderr log line during the test run."""


@pytest.fixture
def server() -> Iterator[str]:
    """A loopback HTTP server; yields its ``http://127.0.0.1:<port>`` base URL."""
    _RecordingHandler.last_method = None
    _RecordingHandler.last_body = b""
    _RecordingHandler.request_count = 0
    httpd = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[0], httpd.server_address[1]
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _webhook(url: str, *, method: str = "POST", body: str = _BODY) -> Opening:
    return Opening(
        name="gate",
        type=OpeningType.WEBHOOK,
        method=method,
        url=url,
        headers={"Authorization": "Bearer fake-token-0000"},
        body=body,
    )


@pytest.mark.asyncio
async def test_webhook_2xx_succeeds(server: str) -> None:
    """A 2xx response opens the entry (no raise)."""
    await fire_webhook_opening(_webhook(f"{server}/ok"))
    assert _RecordingHandler.last_method == "POST"


@pytest.mark.asyncio
async def test_webhook_non_200_2xx_succeeds(server: str) -> None:
    """Any 2xx (e.g. 201 Created) succeeds — not just 200."""
    await fire_webhook_opening(_webhook(f"{server}/created", method="PUT"))
    assert _RecordingHandler.last_method == "PUT"


@pytest.mark.asyncio
async def test_webhook_non_2xx_raises_webhook_error(server: str) -> None:
    """A non-2xx (500) raises WebhookError reporting only the status."""
    with pytest.raises(WebhookError, match="500"):
        await fire_webhook_opening(_webhook(f"{server}/fail"))


@pytest.mark.asyncio
async def test_webhook_network_error_raises_webhook_error() -> None:
    """A connection refused (port 1, closed) raises WebhookError."""
    opening = _webhook("https://127.0.0.1:1/open")
    with pytest.raises(WebhookError):
        await fire_webhook_opening(opening)


@pytest.mark.asyncio
async def test_webhook_get_sends_no_body(server: str) -> None:
    """A GET opening sends NO request body on the wire (body suppressed)."""
    await fire_webhook_opening(_webhook(f"{server}/ok", method="GET", body=_BODY))
    assert _RecordingHandler.last_method == "GET"
    assert _RecordingHandler.last_body == b""


@pytest.mark.asyncio
async def test_webhook_redirect_is_refused_not_followed(server: str) -> None:
    """A 3xx redirect (https->http downgrade) is REFUSED, never followed.

    urllib.request.urlopen follows redirects by default; a 302 to an http:// URL
    would re-send the Authorization header / body in cleartext, defeating the
    https-only guarantee (ADR-0045). The send path must refuse a redirect.
    """
    with pytest.raises(WebhookError):
        await fire_webhook_opening(_webhook(f"{server}/redirect"))
    # EXACTLY ONE request reached the server: the redirect was NOT followed to /ok.
    # If urlopen had followed the Location header, request_count would be 2.
    assert _RecordingHandler.request_count == 1
