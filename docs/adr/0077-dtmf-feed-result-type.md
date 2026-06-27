# ADR-0077 — DtmfReceiver.feed() result type: DtmfPress | DtmfNoPress

| Field     | Value                           |
|-----------|---------------------------------|
| Status    | Accepted                        |
| Date      | 2026-06-26                      |
| Deciders  | agent session (dtmf-feed-result-dedup lane) |
| Backlog   | bk354, bk366                    |

## Context

`DtmfReceiver.feed()` (dtmf.py) previously returned `str | None`, where:

- A `str` meant a completed key-press (the digit character).
- `None` conflated three distinct non-press outcomes:
  1. The end bit was not set — the tone is still in progress (`STILL_PRESSING`).
  2. The end bit was set but this RTP timestamp was already recorded — a redundant
     or reordered end packet (`DUPLICATE_END`).
  3. The end bit was set and the timestamp was new, but the telephone-event code is
     not a keypad digit (e.g. flash = event 16) (`NON_DIGIT_EVENT`).

The sole caller, `_handle_inbound_dtmf` in `media/engine.py`, tested `if digit is not None`
to decide whether to fire the `on_dtmf` callback.  The collapsed `None` was sufficient
for that single caller, but it prevented future callers from distinguishing the cases
without adding their own state.

A separate defect (bk354) kept two parallel structures — `_order: deque[int]` and
`_seen: set[int]` — to implement the bounded insertion-ordered dedup window, requiring
manual synchronisation on every write.

## Decision

### bk366 — `DtmfPress | DtmfNoPress` result type

Replace the bare `str | None` return with a discriminated result type:

```python
@dataclass(frozen=True, slots=True)
class DtmfPress:
    digit: str

class DtmfNoPress(enum.Enum):
    STILL_PRESSING  = "still_pressing"
    DUPLICATE_END   = "duplicate_end"
    NON_DIGIT_EVENT = "non_digit_event"
```

`feed()` now returns `DtmfPress | DtmfNoPress`.

**Why a dataclass + enum pair, not a single enum?**

- `DtmfPress` carries data (the digit string); enum variants cannot carry arbitrary
  instance data in Python's `enum.Enum` without workarounds.
- `DtmfNoPress` is a pure sentinel (no data); an enum is the right type for a fixed
  set of named singletons.
- The caller site `isinstance(result, DtmfPress)` is zero-overhead and unambiguous;
  `match`/`case` or `if isinstance` both work naturally.

**Caller update** (`engine.py::_handle_inbound_dtmf`):
```python
# before
digit = self._dtmf_receiver.feed(event, timestamp=packet.timestamp)
if digit is not None:
    self._on_dtmf(digit)

# after
result = self._dtmf_receiver.feed(event, timestamp=packet.timestamp)
if isinstance(result, DtmfPress):
    self._on_dtmf(result.digit)
```

This is the only production call site.  The test `test_send_dtmf_round_trips_through_receiver`
in `test_media_engine.py` is also updated.

### bk354 — Single `_window: dict[int, None]` structure

Replace `_order: deque[int]` + `_seen: set[int]` with a single
`_window: dict[int, None]`.

Python `dict` is insertion-ordered (guaranteed since Python 3.7, and the repo
requires >= 3.13).  A `dict[int, None]` acts as an ordered set: membership
testing is O(1) by key lookup; oldest-entry eviction is O(1) via
`next(iter(d))` + `del d[key]`; insertion is O(1).

The previous two-structure approach had a correctness obligation: every write to
`_order` had to be paired with a matching write to `_seen` (and vice versa).
A missed `discard` would cause `_seen` to grow unbounded.  The single-structure
approach makes that class of error impossible: there is only one structure to
write.

Eviction semantics are preserved exactly: when the window is full, the oldest
(first-inserted) entry is removed before adding the new timestamp — the same
LRU-ordered eviction the deque + set performed.

## Alternatives considered

1. **Return `str | None` with doc comments distinguishing the None cases** — rejected:
   documentation is not a type contract; future callers cannot branch on it without
   runtime state.

2. **Single `DtmfResult` enum with a `PRESS` variant carrying the digit as a
   string field** — rejected: Python `enum.Enum` with per-member instance data
   requires awkward `__new__` overrides and breaks `is`-comparison ergonomics.

3. **`Optional[str]` with a separate `last_reason` attribute on the receiver** —
   rejected: thread-safety is harder, the reason is stale after the next call, and
   it adds mutable state to a logically pure computation.

4. **Keep `deque + set`** — rejected: dual-write synchronisation is a latent bug
   surface, and a single dict is both simpler and equivalent in complexity.

## Consequences

- `feed()` is a breaking API change.  The module is internal to this package; the
  only production consumer is `media/engine.py`, which is updated in the same
  commit.  Test consumers are also updated.
- Future callers (e.g. a confirmation-flow router that wants to log the reason a
  digit was suppressed) can branch on `DtmfNoPress.DUPLICATE_END` vs
  `DtmfNoPress.NON_DIGIT_EVENT` without adding state.
- `DtmfPress` and `DtmfNoPress` are exported from `hermes_voip.dtmf`; importing
  code must add them to its imports.
- The `_window` consolidation removes two attributes (`_order`, `_seen`) from
  `DtmfReceiver`; code that introspects these private attrs breaks (by design —
  they are private, and the structural test in the test suite verifies the
  migration).
