# ADR-0081: Skip one unparseable SIP message, but still propagate a framing failure

- **Date:** 2026-06-26
- **Status:** Accepted — amended by ADR-0098 (the parse-only decision below stands; a
  scoped reader dispatch-boundary fail-safe backstop now also contains a *post-parse*
  handler exception to the one message, closing the same class as a class)
- **Deciders:** operator (backlog item bk646) — agent session (transport robustness lane)

## Context

Both signalling transports drive a single sequential reader task that frames the
inbound stream into whole SIP messages and dispatches each one:

- `src/hermes_voip/transport/connection.py` (SIP-over-TLS) reads bytes, feeds them to
  `SipMessageFramer`, and calls `_dispatch(raw)` per yielded message;
- `src/hermes_voip/transport/ws_connection.py` (SIP-over-WSS) receives one text frame
  per message (RFC 7118 §5 — no stream framing) and calls `_dispatch(raw)`.

Before this ADR, both `_dispatch` implementations parsed the message with a **bare**
`SipResponse.parse` / `SipRequest.parse`:

```python
async def _dispatch(self, raw: str) -> None:
    if raw.startswith(_RESPONSE_PREFIX):
        await self._dispatch_response(SipResponse.parse(raw))
    else:
        await self._dispatch_request(SipRequest.parse(raw))
```

`SipRequest.parse` / `SipResponse.parse` raise a `ValueError` on a malformed
message (a first line that is neither a request-line nor a status-line, a header with
no colon, a continuation with no preceding header). That `ValueError` propagated out
of `_dispatch` → out of the `for raw in framer` loop → out of `_read_loop`, ending the
reader task; `_on_reader_done` then fired `on_connection_lost`. On the TLS transport
that connection is **shared by every active call** (it is the one signalling hub), so
**one peer sending one malformed message dropped ALL active calls** — a denial of
service against unrelated, established calls. On the WSS transport the same fault
dropped the registration (inbound calls then divert to voicemail).

`connection.py:35-38` previously documented this as intentional ("an unparseable /
unframable stream fails the reader task"). That conflates two genuinely different
failures, which is the design tension bk646 asks this ADR to resolve.

### The two failures are not the same

`SipMessageFramer` (`framing.py`) is responsible for **delimiting** messages on a byte
stream by `Content-Length` + CRLF framing. When it cannot (an absent / non-numeric /
out-of-range `Content-Length`, an unterminated oversized head, a continuation with no
header), it raises `FramingError` (a subclass of `ValueError`) **from its iteration**.
After a framing failure the next message boundary is unknown — the stream is
**unrecoverable**; nothing downstream can re-synchronise it.

But once framing **succeeds**, exactly one complete message has been extracted and the
stream is still synchronised at the next boundary. If only *that one message* fails to
*parse*, the rest of the stream — and every other active call on the connection — is
unaffected. Dropping the whole connection for it is disproportionate.

Crucially, the two failures arise at **different layers**, so they are cleanly
separable by where the exception is raised:

- a `FramingError` is raised by the framer, inside `_read_loop`'s `for raw in framer`
  (TLS); it never enters `_dispatch`;
- a parse `ValueError` is raised by `SipRequest.parse` / `SipResponse.parse`, inside
  `_dispatch`, on an already-framed `str`.

This was verified directly: a `GARBAGE\r\nContent-Length: 0\r\n\r\n` message frames
cleanly (the framer yields it) and then fails `parse` with a bare `ValueError` that is
**not** a `FramingError`; a head with no `Content-Length` raises `FramingError` from
the framer iteration, never reaching `_dispatch`.

## Decision

**Distinguish framing failure from post-framing parse failure.**

- **Framing failure → propagate (unchanged).** A `FramingError` from the framer
  (the stream can no longer be delimited) still ends the reader task and is surfaced
  via `on_connection_lost`. This is the pre-existing, correct behaviour and is left
  untouched — `_read_loop` does not catch it.
- **Post-framing parse failure → log loudly and skip the one message.** `_dispatch`
  now wraps only the `parse` call in `try/except ValueError`. On a parse failure it
  emits a **WARNING** and `return`s, skipping that single message; the connection and
  every other active call on it stay alive. The well-formed messages that follow on the
  same connection are still dispatched.

This is **not** swallowing an error (rule 37): the failure is surfaced via a structured
WARNING. It is the same robustness pattern already blessed in this codebase for the
Deepgram Flux frame reader (`stt/deepgram.py::_parse_flux_frame` — "a single bad frame
must not kill the live call"). One malformed message must not be a DoS against
unrelated calls.

The fix is applied identically to **both** transports.

### Log content — PUBLIC repo, rule 34

The WARNING carries a **non-PII summary only**: `type(exc).__name__` and `len(raw)`.
It deliberately does **not** log the raw message or the exception's `str` — a SIP
message carries `From` / `To` / `Call-ID` / `Contact` / SDP (PII and routing detail),
and the parse error's text echoes the offending wire line. The repo is public; the same
discipline (`type(exc).__name__`, never the body/line) is already used by the glare
ACK/BYE failure log and the adapter's structural error logs. Type + length is loud
enough to alert an operator that a peer is emitting malformed signalling without leaking
wire content.

### Implementation shape

```python
async def _dispatch(self, raw: str) -> None:
    is_response = raw.startswith(_RESPONSE_PREFIX)
    try:
        message: SipResponse | SipRequest = (
            SipResponse.parse(raw) if is_response else SipRequest.parse(raw)
        )
    except ValueError as exc:
        _log.warning(
            "dropping an unparseable SIP message (%s, len=%d) — connection kept",
            type(exc).__name__,
            len(raw),
        )
        return
    if isinstance(message, SipResponse):
        await self._dispatch_response(message)
    else:
        await self._dispatch_request(message)
```

Parsing happens inside the `try`; the parsed message is dispatched **outside** it, so
the `except` scopes to the parse alone and never accidentally swallows a downstream
dispatch/handler error (rule 37). The union-typed `message` plus the `isinstance`
discriminator keeps it clean under `mypy --strict` (no `Any`, no `cast`,
no possibly-unbound locals). `ws_connection.py` gains a module logger
(`logging.getLogger(__name__)`) to match `connection.py`.

## Consequences

- A malformed message from a buggy/compromised/hostile peer is now contained to that
  one message; the connection and all other active calls survive. This closes a real
  DoS vector on the shared TLS signalling connection.
- A genuine framing corruption is still fatal to the connection (correct — the stream
  is unrecoverable) and is still surfaced via `on_connection_lost`; the higher layer's
  reconnect/registration-refresh path (ADR-0055) handles recovery.
- The `connection.py` module docstring (rule 27) is updated from "an unparseable /
  unframable stream fails the reader task" to describe the framing-vs-parse split; the
  `ws_connection.py` docstring gains the same note.
- Operators see a WARNING per malformed message. A peer flooding malformed messages
  produces a WARNING per message but does no other harm; if log volume from such a
  flood ever became a concern, rate-limiting the WARNING is a future refinement (not
  built now — it would be aspirational, rule 27).

## Alternatives considered

- **Catch the parse failure in `_read_loop` instead of `_dispatch`.** Rejected: the
  framer iteration and the parse both live in `_read_loop`'s `for` body, so a single
  `try` there could not tell a `FramingError` (must propagate) from a parse
  `ValueError` (must be skipped) without `isinstance` gymnastics, and would risk
  swallowing the framing failure. Scoping the `try` to the `parse` call inside
  `_dispatch` keeps the two failures cleanly separated by construction.
- **Catch `Exception` rather than `ValueError`.** Rejected (rule 37): only `parse`'s
  documented `ValueError` is a recoverable per-message fault. A broader catch could
  mask programming errors or `asyncio.CancelledError`.
- **Answer the malformed message with a 400 Bad Request.** Rejected: the message did
  not parse, so we have no reliable `Via` / `Call-ID` / `CSeq` to build a routable
  response from; fabricating one risks mis-routing. Silently skipping (with a WARNING)
  is the safe, RFC-permissible choice for an unparseable datagram.
