# ADR-0098: A scoped reader dispatch-boundary fail-safe backstop (amends ADR-0081)

- **Date:** 2026-07-02
- **Status:** Accepted
- **Deciders:** operator (task #59) — agent session (reader-backstop lane)

## Context

ADR-0081 established the principle that **one malformed SIP message must not DoS
unrelated active calls** on the shared signalling connection. It fixed the then-known
instance by scoping `_dispatch`'s `try/except ValueError` to the **parse call only** —
dispatching the parsed message downstream *outside* the `try` — and it **explicitly
rejected two broader alternatives**, for reasons that were and remain sound:

- **Catching in `_read_loop`.** The framer iteration and the parse both live in
  `_read_loop`'s `for` body, so a single `try` there could not distinguish a
  `FramingError` (the stream can no longer be delimited — **must** propagate and end the
  connection) from a parse `ValueError` (one framed message — **must** be skipped)
  without `isinstance` gymnastics, and risked swallowing the framing failure.
- **Catching `Exception` rather than `ValueError`** at the parse step. Only `parse`'s
  documented `ValueError` is a recoverable per-message fault; a broader catch there could
  mask programming errors or `asyncio.CancelledError`.

That reasoning is correct **for the parse step**, and this ADR leaves it untouched.

What ADR-0081 did not close is a **second exposure on the same reader path**. The reader
awaits `_dispatch_response` / `_dispatch_request` — and every handler they reach
(`_auto_ack_non_2xx`, the auto-response builders, `manager.on_response`,
`CallSession.handle_request`, the in-dialog re-INVITE path, …) — **outside** the
parse-only `try`. A message that **parses cleanly but whose downstream handling raises**
tears the reader down exactly as a parse failure once did:

```
handler raises → _read_loop → _on_reader_done → on_connection_lost → whole-connection teardown
```

i.e. every active call **and** the registration drop, from a single crafted-but-parseable
inbound message.

### The evidence: the per-site approach is not converging

Since ADR-0081, a **#57 catch-completeness audit** plus a wave of per-site fixes have
found **10+ distinct escapes of this one class** — each a different site, each reached by
a single parseable inbound message, each individually a whole-connection DoS:

- auto-response builders `build_response` / `build_options_ok` / `build_keepalive_ok`
  inline in `_handle_cancel` / `_answer_keepalive` (transport), and
  `_AnsweredDialogGuard.handle_request` in-dialog (adapter) — PR #388;
- `RegistrationFlow._check_cseq` `int()` on a non-decimal CSeq — PR #390 (task #56);
- `CallSession.handle_request` in-dialog `build_response` — PR #391 (task #55);
- transport `_txn_key` `int()` (×2 transports) + `_build_ack`'s `To`-require in
  `_auto_ack_non_2xx` — PR #392 (task #58);
- `call.py` `_cseq_number` `int()` via `CallSession.on_response` — PR #393 (task #60);
- the in-dialog re-INVITE `_on_reinvite` SDP parse — identified, task #61 (in flight).

On top of the *named* sites there is a **latent residual the per-site guards do not fully
close**: at **every** `int(x)`-after-`x.isascii() and x.isdecimal()` site, an
all-ASCII-decimal token longer than CPython's `sys.get_int_max_str_digits()` limit
(default 4300) **still** makes `int()` raise `ValueError` ("Exceeds the limit for integer
string conversion"). Every such site (`registration._min_expires` / `_granted_expires` /
`_check_cseq`, `sdp` crypto-tag / rport, `transaction._cseq_number`, `call._cseq_number`,
transport `_txn_key`) carries the same over-long-decimal escape.

The conclusion the evidence forces: the per-site parse-only mechanism, though each fix is
individually correct, **does not on its own deliver ADR-0081's stated intent**. Each
newly-reachable handler that can raise is a fresh whole-connection DoS until separately
discovered and patched — and the discoveries keep coming. A **fail-soft backstop at the
dispatch boundary is more aligned with ADR-0081's intent** ("one malformed message must
not DoS unrelated calls") than the per-site mechanism alone.

## Decision

Add a **scoped reader dispatch-boundary fail-safe backstop** in both transports
(`connection.py` and `ws_connection.py`), and **keep every per-site fix in place** as
defense-in-depth. This ADR **amends** ADR-0081 (whose status line is set to *Amended by
ADR-0098*); ADR-0081's parse-only decision stands unchanged and remains the first line.

In each `_dispatch`, **after** the unchanged parse-only `try/except ValueError`, the two
dispatch calls are wrapped:

```python
try:
    if isinstance(message, SipResponse):
        await self._dispatch_response(message)
    else:
        await self._dispatch_request(message)
except Exception as exc:  # noqa: BLE001 — ADR-0098 reader fail-safe backstop
    _log.warning(
        "a SIP message handler raised (%s) — dropping the one message,"
        " connection kept (ADR-0098 reader fail-safe backstop)",
        type(exc).__name__,
    )
```

Each design choice answers a **specific ADR-0081 concern**:

- **Scoped to the dispatch boundary, NOT `_read_loop`.** The backstop wraps only the
  `_dispatch_response` / `_dispatch_request` calls *inside* `_dispatch`; it never wraps
  the framer iteration in `_read_loop`. A `FramingError` — raised by the framer, in
  `_read_loop`, and never entering `_dispatch` — therefore **still propagates and ends the
  connection**, exactly as ADR-0081 requires for an unrecoverable stream. This is the
  point ADR-0081 made against a `_read_loop` catch-all, honoured by construction.
- **`except Exception`, NOT `except BaseException`.** `asyncio.CancelledError` (and
  `SystemExit` / `KeyboardInterrupt`) subclass `BaseException`, not `Exception`, so
  cooperative cancellation of the reader task **still propagates**. This is precisely the
  thing ADR-0081 worried about with a broad catch, addressed head-on — and locked by a
  test asserting a handler `CancelledError` propagates out of `_dispatch`.
- **Loud WARNING, exception type name only.** On catch, a WARNING is logged carrying
  `type(exc).__name__` **only** — never the wire content (no headers / Call-ID / host /
  body / SDP; the repo is PUBLIC, rule 34). The WARNING keeps any masked logic bug
  **surfaced** for an operator, so this is fail-soft, not silent suppression (rule 37) —
  the same discipline ADR-0081 used for its parse skip.
- **Per-site fixes retained.** The parse-only `except ValueError` and every per-site guard
  remain the first line; this backstop is the **last** line for the same class. Because
  the per-site guards stop the common faults ever reaching the backstop, the backstop is a
  genuine backstop (rarely hit in normal operation), **not** a load-bearing catch-all that
  would mask sloppy handlers.

The `# noqa: BLE001` (flake8-blind-except) carries the rule-20 justification inline; the
fuller rationale sits in the comment above the `try` in each transport.

## Consequences

- A parseable message whose downstream handling raises **any** exception is now contained
  to that one message; the connection, every other active call, and the registration
  survive. This closes the reader-escape class **as a class** — including escapes not yet
  individually found and the over-long-`int()` residual — rather than one site at a time.
- `asyncio.CancelledError` still cancels the reader; a `FramingError` still ends the
  connection (recovery via the ADR-0055 reconnect / re-register path). Neither is swallowed
  (rule 37).
- A handler bug that previously crashed the whole connection now surfaces as a per-message
  WARNING instead — the deliberate trade of **availability over fail-fast**. It is not
  hidden: the WARNING names the exception type and fires per occurrence, so a real logic
  bug remains visible in logs and CI, and the per-site fixes + their RED→GREEN tests remain
  the mechanism that catches such bugs precisely.
- Operators see a WARNING per offending message. A peer flooding such messages produces one
  WARNING each and no other harm (same posture as ADR-0081's parse skip); rate-limiting the
  WARNING remains a future refinement, not built now (rule 27).
- The dispatch path gains one `try` per message. In CPython ≥3.11 a `try` that does not
  raise is zero-cost, so the reader hot path is unaffected.

## Reversibility

Fully reversible and low-risk. The change is two localized `try/except` blocks (one per
transport) plus doc/comment updates; removing them restores the exact ADR-0081 behaviour.
Nothing depends on the backstop's presence — the per-site guards stand alone — and no data
model, wire format, or public API changes.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Keep per-site only (status quo, ADR-0081) | 10+ escapes of the same class found post-ADR-0081 and still counting (incl. the over-long-`int()` residual at every `isascii()+isdecimal()` site); the mechanism does not converge on ADR-0081's own intent. |
| Broaden the catch into `_read_loop` | Would also swallow a `FramingError` that MUST end the connection; ADR-0081 rejected this and it remains correct — hence scoping to the dispatch boundary instead. |
| `except BaseException` at the boundary | Swallows `asyncio.CancelledError` / `SystemExit`; the reader could not be cancelled or stopped (rule 37). `except Exception` lets them propagate. |
| Replace the per-site guards with only the backstop | Would make the backstop load-bearing and mask common, precisely-fixable faults; a bug should be caught at its site with a real test. The backstop is defense-in-depth, not a substitute. |
| Answer the offending message with an error response | The fault is often *in* the handler/builder, so we cannot reliably build a routable response; fabricating one risks mis-routing. Dropping with a WARNING is the safe choice (as in ADR-0081). |
