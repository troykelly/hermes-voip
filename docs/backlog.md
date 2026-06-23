# hermes-voip backlog

Exhaustive register of every polish / improvement / robustness / efficiency / test / docs
opportunity found across all merged modules, grouped by module. The operator asked for **ALL**
opportunities regardless of priority — nothing here is pre-prioritised away. Each item is a
checklist entry with a severity tag: **[high]** / **[medium]** / **[low]**, plus a kind tag
(correctness / robustness / efficiency / api / test / docs / polish).

This backlog now spans the **full current codebase**: the original sans-IO SIP/RTP signalling +
provider-interface + media-codec foundation, PLUS the subsystems that were once "unbuilt" but have
since shipped — `stt/`, `tts/`, `media/vad.py`, `media/engine.py`, `media/call_loop.py`, `guard/`,
`adapter.py`, `plugin.py`, `call.py`, `incall.py`, `dialog.py`, `transport/`, and the end-to-end
test suite. Findings in those shipped subsystems were added by the autonomous Wave-1 `/orchestrate`
gap-review (ADR-0072; 2026-06-23; 11 of 12 dimensions — see Gap-review provenance below).

`docs/plan/IMPLEMENTATION-PLAN.md` predates the above subsystems and is now significantly stale
(tracked as a docs-drift item below).

Severity reflects impact on the codebase as it stands: a `[high]` is a correctness/contract
defect or a load-bearing test gap.

---

## src/hermes_voip/digest.py

- [x] **[high] correctness** (done: f2e10de) — Parser ignores RFC 2617 quoted-pair escapes inside quoted-strings.
  `_PARAM` (line 24) uses `"([^"]*)"`, stopping at the first inner `"` and never honouring `\"`/`\\`.
  Verified: `Digest realm="a\"b", nonce="xyz"` yields `realm == 'a\\'` (a backslash, `b"` dropped)
  instead of `a"b`. Since `realm`/`nonce` feed HA1/HA2 verbatim, a server whose realm legitimately
  contains an escaped quote/backslash produces a wrong `response` and registration fails silently.
  The build side (`_quoted`, line 53) *does* emit quoted-pair escapes, so parse/emit are asymmetric.
  Fix: escape-aware pattern `"((?:[^"\\]|\\.)*)"` + unescape; add a KAT + round-trip test.
- [x] **[high] correctness** (done: f2e10de) — Quoted-param values escaped for the wire are NOT escaped before
  hashing — HA1/response. `build_authorization` hashes `challenge.realm` verbatim (line 154) but
  renders through `_quoted` (lines 184-187). This is correct per spec, but there is **no test** proving
  the hash uses the unescaped form, so a future "simplification" (escape-before-hash, or stop escaping)
  would pass the suite. Mutation-weak around the most security-sensitive computation. Add a KAT with
  `"`/`\` in realm/username proving the header is escaped AND the response equals the digest of the
  *unescaped* inputs (compute the vector independently).
- [x] **[medium] correctness** (#176) — Bare (unquoted) auth-param values swallow `;`-delimited trailing
  content. Bare alternative `([^,\s]+)` (line 24) terminates only on comma/whitespace, not `;`.
  Verified: `Digest realm=r;x=y, nonce=n` parses realm as `r;x=y`. A permissive/garbled gateway could
  poison realm (and thus HA1). Tighten the bare class or post-validate realm/nonce against the
  token/quoted-string grammar; add a boundary test.
- [ ] **[medium] robustness** — `nc` accepts negative and `>0xffffffff` values, producing malformed
  nonce-count. `f"{nc:08x}"` renders `nc=-1` as `-0000001`, `nc=2**40` as 9+ hex digits (both verified).
  RFC 2617 nc is exactly 8 hex digits. Validate `0 < nc <= 0xFFFFFFFF`; tests for nc=0/-1/2**32.
- [ ] **[medium] robustness** — No control-char-rejection tests for each caller/challenge-sourced
  quoted param. `_quoted`'s guard is shared, but only `nonce` is covered (`test_rejects_crlf_in_auth_param_value`).
  `uri` is both hashed and rendered; `username`, `opaque`, `realm`, `cnonce` are rendered too. Add
  per-field CRLF-rejection tests — the per-field coverage is what mutation testing rewards.
- [ ] **[low] robustness** — Empty `cnonce` (`cnonce=""`) is accepted and emitted (`is not None` check,
  line 167). Defeats the client-nonce purpose; some registrars reject it. Reject empty (or treat falsy
  as "generate") and document; add a test.
- [ ] **[low] correctness** (partial — residual: no test asserting unknown/hyphenated params are silently ignored (no test with an unknown x-custom-param= in tests/test_digest.py).) — `_PARAM` key pattern `\w+` (line 24) cannot match hyphenated extension
  param names. No test asserts unknown/hyphenated params are ignored gracefully. Broaden to `[\w-]+`
  for forward-compat or pin the silent-skip behaviour with a test.
- [ ] **[low] robustness** — `algorithm` token rendered unquoted (line 163) after only a lowercase
  membership check (line 147). Safe today (only `md5` passes) but the render path trusts the gate.
  Route through the control-char guard or assert a token grammar; note the coupling in a comment.
- [ ] **[low] robustness** — `realm` defaults to empty string silently when the challenge omits it
  (line 99), hashed into HA1 (line 154). Asymmetric with the missing-`nonce` case which *raises*
  (lines 93-95). Either raise on missing/empty realm, or document why empty realm is tolerated; add a test.
- [ ] **[low] api** — Caller cannot supply a precomputed HA1; `DigestCredentials` holds plaintext
  password (line 112) and recomputes HA1 each call. RFC 2617 allows storing A1. Offer an HA1-based
  credential variant so security-conscious callers need not keep plaintext resident; record in the ADR.
- [ ] **[low] api** (partial — residual: the build_authorization docstring does not explicitly state that auth-int-only is the rejection path or scope this as SIP REGISTER / empty b) — `qop` selection silently drops `auth-int`; auth-int unimplemented. Correct/in-scope
  for SIP REGISTER (empty body) but the docstring doesn't call out that auth-int-only is the rejection
  path. State the scope in the docstring/ADR; keep the auth-int-only rejection test.
- [ ] **[low] api** — `build_authorization` returns a bare value string; `registration.py` (lines 161-164)
  separately maps 401→Authorization / 407→Proxy-Authorization, duplicating knowledge the challenge origin
  already has. Carry `proxy: bool` on `DigestChallenge` (set by parse) or return a `(name, value)` pair;
  record in the digest/registration ADR.
- [ ] **[low] test** — No assertion that `algorithm`/`qop`/`nc` render UNQUOTED (`algorithm=MD5`, not
  `="MD5"`). `_param` helper strips quotes, so a quote-everything mutation survives. Assert raw header
  substrings (`'algorithm=MD5,' in header`, `'qop=auth,' in header`).
- [ ] **[low] test** — No test that `opaque` is ABSENT when the challenge omits it (negative case,
  line 181). A mutation always emitting `opaque=""` survives. Assert `_param(header,"opaque") is None`.
- [ ] **[low] test** — No test pinning the quoting table (username/realm/nonce/uri/cnonce/response/opaque
  quoted vs algorithm/qop/nc unquoted, lines 158-182). Add raw-substring assertions for at least one
  quoted and one unquoted param to lock the boolean-flip mutants.
- [ ] **[low] docs** — `DigestCredentials.password` is repr-suppressed (`field(repr=False)`, line 112)
  but the docstring doesn't say it is still plaintext in memory and must never be logged/interpolated.
  Add the note (public-repo / AGENTS 34); optionally a test that the secret isn't in `repr(creds)`.
- [ ] **[low] docs** — `_md5_hex` always encodes UTF-8 (line 37); a `charset` param (RFC 7616) is
  ignored. Latin-1 gateways with accented credentials would mismatch HA1. Document the UTF-8 assumption;
  add a non-ASCII-realm test pinning the behaviour.
- [ ] **[low] docs** — Algorithm round-trip subtlety: case-insensitive gate (line 147), case-preserving
  echo (line 163). Correct and intended but unexplained. Add one docstring sentence; add an uppercase
  `algorithm=MD5` accepted+echoed test to complete coverage.
- [ ] **[low] efficiency / docs** — `parse` materialises a full param dict even though only ~5 keys are
  read. Cold path (per-registration, not per-RTP-packet) — **no action on perf grounds**; note in review
  that digest parsing is cold-path so a future agent doesn't "optimise" clarity away.

## src/hermes_voip/message.py

- [x] **[high] correctness** (done on main; verified Wave 2) — `parse()` raises `AttributeError` (not `ValueError`) on a reason-less
  status-line. Status-line regex (line 23) makes the reason optional; `SIP/2.0 200\r\n\r\n` matches but
  `group(2)` is `None`, and line 162 calls `.strip()` → `AttributeError` (verified). Contradicts the
  docstring (lines 130-133) and rule 37. Make the second SP mandatory: `r"SIP/2\.0 (\d{3}) (.*)"`; add
  red tests for the reason-less form (must `ValueError`) and the empty-reason form (`reason==""`).
- [x] **[high] correctness** (done on main; verified Wave 2) — `build_request` emits a DUPLICATE `Content-Length` when the caller
  supplies one. Computed value is unconditionally appended (line 87) after every caller header; passing
  `("Content-Length","999")` yields two headers (verified) → malformed framing per RFC 7230, possible
  mis-framing. Reject (or drop) a caller-supplied Content-Length with `ValueError`; red test for the raise.
- [x] **[high] test** (done on main; verified Wave 2) — No test for the reason-less status-line (the `AttributeError` bug). Existing
  `test_parse_rejects_malformed_status_line` only covers `SIP/2.0 200OK`. Add the two cases above.
- [ ] **[medium] polish** — Control-character rejection logic is duplicated verbatim between
  `message.py` (lines 27-36) and `digest.py` (lines 30-32, 50). Two copies of an injection-guard
  invariant will drift. Hoist a shared `contains_control`/`reject_controls` into an internal
  `_text.py`/`_chars.py`; keep per-call-site wording; add a focused test (NUL/CR/LF/HTAB/DEL + a high
  code point that must be allowed).
- [ ] **[medium] robustness** — Header-block lines without a colon are silently dropped (`if sep:`,
  lines 156-159). Verified: a `garbageline` between headers vanishes with no error — inconsistent with
  the module's otherwise-strict stance. Decide and document: prefer raising `ValueError('malformed header
  line: …')`; if leniency is wanted, comment it and pin with a test.
- [x] **[medium] robustness** (#184) — `build_request` does not validate request-line method/URI shape. Only
  `_reject_controls` runs (lines 78-79); an empty method, embedded-space method, or empty/space URI
  passes. Verified: `build_request("", "sip:x", [])` → start-line `' sip:x SIP/2.0'`. Validate method
  against the token rule + reject empty; reject empty/space URI. Red tests for each.
- [ ] **[medium] test** (partial — residual: the multibyte-UTF-8 body test pinning byte-length Content-Length is still absent (test at line 150 uses ASCII-only body).) — Duplicate Content-Length and caller-owned-header collisions are untested.
  Add a test after deciding the policy; also a multibyte-UTF-8 body test pinning the byte-length
  Content-Length (a `len(body)` mutant survives today).
- [ ] **[medium] test** — Token-generator tests under-constrain format/length (mutation-weak).
  `test_new_branch_…` checks only `startswith('z9hG4bK')` and inequality; `new_call_id()` format/length
  unchecked. A `token_hex(8)→token_hex(4)` mutant survives. Assert
  `re.fullmatch(r'z9hG4bK[0-9a-f]{16}', new_branch())`, `[0-9a-f]{12}` for `new_tag()`, `[0-9a-f]{24}`
  for `new_call_id()`.
- [ ] **[low] efficiency** — `header()`/`headers_all()` re-lowercase every stored field name on every
  lookup (O(n) scan, no cache); `registration` does ~4-5 lookups/response. Off the RTP hot path, so
  cost is negligible TODAY. Either accept with a one-line comment, or build a lazily-computed
  lowercased index at `parse()` time. Document the decision either way.
- [ ] **[low] robustness** — `header()` returns the first match for list-valued/repeatable headers
  (Via/Contact/WWW-Authenticate). No docstring warning that callers of list-valued headers should use
  `headers_all()`. A latent trap for the future INVITE/dialog code. Add the caveat; optionally a
  quoting-aware `header_values(name)`.
- [ ] **[low] api** — No `Content-Length` cross-check / no int-header accessor. `parse()` trusts framing
  (documented). Consider an optional `validate_content_length` / `check_framing()` and a `header_int(name)`
  helper so `registration._granted_expires`'s hand-rolled int parsing (lines 188-200) is shared.
- [ ] **[low] test** — Continuation-line edge cases partially covered. The `'\t'` fold arm (line 148),
  the orphan-continuation `ValueError` path (lines 149-151), and the exact single-space join (line 152)
  are untested. Add tab-fold, first-line-starts-with-space (`ValueError(match='continuation')`), and
  exact-unfolded-string tests.
- [ ] **[low] docs** — `parse()` splits the body on the FIRST `CRLFCRLF` via `partition` (line 139),
  correctly preserving bodies with embedded blank lines, but it's untested and a `split()` refactor would
  corrupt them. Add a comment + a body-with-embedded-blank-line round-trip test.
- [ ] **[low] api** — Module exposes no `__all__`. Add
  `__all__ = ['SipResponse','build_request','new_branch','new_call_id','new_tag']`.

## src/hermes_voip/sdp.py

- [x] **[high] api** — `build_audio_offer` / `build_audio_answer` can now produce an SRTP/SAVP
  answer (RESOLVED, ADR-0053 Stage 1). Both take keyword-only `crypto` and emit `m=audio … RTP/SAVP`
  + an `a=crypto` line; `build_audio_answer` (`sdp.py`) mints our own answer key via
  `generate_answer_crypto` / `_negotiate_answer_crypto` and is wired into `adapter.py` for an
  `RTP/SAVP` offer (a plain `RTP/AVP` offer is still answered plain). Round-trip + adapter tests
  cover `is_srtp` / `crypto` survival.
- [ ] **[medium] correctness** — Opus / preference-ordered offer is unreachable through the builder.
  `negotiate_audio` selects in *offer* order (lines 220-240) with no notion of *our* preference, so a
  gateway offering PCMU-then-Opus keeps PCMU first, violating ADR-0005's "prefer Opus when offered".
  Order the result by the caller's `supported` order, or add `prefer: Sequence[str]`; document which wins
  (RFC 3264 lets the answerer reorder).
- [x] **[medium] api** — Companion `build_audio_answer` now exists (RESOLVED, ADR-0053 Stage 1):
  `build_audio_answer` in `sdp.py` parses the offer → negotiates → builds the answer with reciprocal
  direction + keyed crypto, so callers no longer re-derive RFC 3264 reciprocity by hand. Wired in
  `adapter.py`.
- [x] **[medium] robustness** (#185) — Parser accepts negative/zero ports and negative ptime silently.
  Verified: `m=audio -5 RTP/AVP 0` → port `-5`; `a=ptime:-3` → ptime `-3`. The *builder* validates these
  but the *parser* (hostile inbound data) does not. Validate port range and `ptime>0` on parse → `SdpError`;
  decide/document `port==0` (held/declined media) explicitly.
- [x] **[medium] test** (done — Wave 3 audit) — No negative-control on negotiate ordering / TE-only rejection. No test where
  offer order differs from `supported` (an order-by-supported mutant survives); no test for the
  telephone-event-shared-but-no-voice branch (`has_voice` guard, lines 236-239). Add both.
- [ ] **[low] robustness** — Direction attribute matching is exact-case only; `a=SENDONLY` is silently
  ignored (defaults to `sendrecv`, lines 146-147) → wrong media direction (e.g. a hold treated as
  sendrecv). Lower-case before the membership test; add a mixed-case test.
- [ ] **[low] robustness** — Duplicate rtpmap (last-wins) and duplicate payload-type in `m=` (dup
  `Codec`) resolved silently. Decide a policy (dedupe PTs preserving first; document last-wins for rtpmap)
  and pin with a test.
- [ ] **[low] correctness** — telephone-event clock-rate vs voice clock-rate consistency not validated
  (RFC 4733). `telephone-event/16000` alongside `PCMU/8000` is accepted and would mis-time DTMF
  (ADR-0010). After negotiation, validate the TE clock_rate equals the selected voice rate; test the
  mismatch.
- [ ] **[low] efficiency** — `SessionDescription.parse` does `text.replace(_CRLF,'\n').split('\n')`
  (line 189), allocating two transient buffers. SDP is NOT the 50pkt/s path. Use `splitlines()` (drops
  the copy, handles bare CR) — micro-optimisation; correctness of bare-CR handling is the better reason.
- [ ] **[low] polish** (partial — residual: addrtype is still hardcoded to IP4 at sdp.py:1330, 1332, 1464, 1689, 1691 — "derive addrtype from the address family" is unimplemented.) — `o=` line collapses three distinct RFC 4566 fields. `o=- {session_id}
  {session_id} IN IP4 {local_address}` (line 285) forces sess-id == sess-version (a re-INVITE must keep
  sess-id and increment only sess-version — the docstring's re-INVITE claim at 260-261 is aspirational,
  rule 27), hard-codes username `-`, and hard-codes `IP4` so an IPv6 local_address emits a wrong addrtype.
  Split `session_id`/`session_version`; derive addrtype from the address family.
- [ ] **[low] polish** — `add_attribute` exception funnel double-wraps; the `isinstance(exc, SdpError)`
  re-raise branch (lines 116-129) is dead (nothing in `_add_attribute` raises `SdpError`). Simplify to
  `except SdpError: raise` then `except ValueError: …`, or remove the dead branch.
- [ ] **[low] polish** — Malformed integer payload types / rtpmap fields surface as a generic
  `malformed a=…` (line 128) with no field detail. Raise per-field messages ("malformed rtpmap clock
  rate") so gateway-interop failures are diagnosable.
- [ ] **[low] api** — `SessionDescription.connection_address` duplicates `audio.connection_address`
  (the resolved media-level-or-session-fallback). A caller reaching for the session field when they meant
  the media's effective address gets the wrong value. Document that `audio.connection_address` is the
  effective address.
- [ ] **[low] docs** (partial — residual: No "Scope and leniency" paragraph was added to the module docstring (sdp.py:1-20); it still only describes the two keying paths. DONE (reque) — No module-level statement of scope/leniency (video ignored is stated; 2nd+ audio
  sections ignored, unknown `a=` dropped, dup rtpmap last-wins, no DTLS fingerprint, port 0 not special
  are NOT). Add a "Scope and leniency" paragraph; consider modelling `a=fingerprint`/`a=setup` so the
  ADR-0005 DTLS-SRTP profile is representable.
- [ ] **[low] test** — Build-side under-asserted (mutation-weak round-trip): no literal-line assertions
  (`v=0` first, single `m=audio … RTP/AVP`, `a=ptime:20`, `a=sendrecv`, trailing CRLF). A dropped `v=0`
  round-trips through our own lenient parser. Assert key literal lines.
- [ ] **[low] test** (partial — residual: test_build_rejects_bad_direction (specifically named in the item) does not exist — no test asserts that build_audio_offer/build_audio_answer) — Direction round-trip untested for sendonly/recvonly/inactive (every assertion is
  `sendrecv`); build-side direction validation (lines 270-272) and `session_id`-into-`o=` are untested.
  Parameterise over all four directions for parse+build; add `test_build_rejects_bad_direction` and a
  session-id-in-o= test.
- [ ] **[low] test** — Parser robustness paths untested (negative/zero port, negative ptime, blank lines,
  lone CR, trailing whitespace, duplicate rtpmap). Add once the parser is hardened (assert `SdpError`).

## src/hermes_voip/registration.py

- [x] **[high] robustness** (done — Wave 3 audit) — `423 Interval Too Brief` is treated as a hard failure. `handle()` maps
  everything not 200/401/407 to `Failed` (lines 145-151); a registrar enforcing a minimum interval
  (`Min-Expires`) ends the flow with no retry → silent total outage. Read `Min-Expires`, re-issue with
  `expires = max(requested, min_expires)`; surface a new outcome or transparently re-build. Red-then-green
  regression test.
- [x] **[high] api** (done — Wave 3 audit) — Registration API is not exported from `__init__.py` (only `sip_address_of_record`).
  `RegistrationFlow`/`RegistrationConfig`/`Challenged`/`Registered`/`Failed` are reachable only via the
  deep path. For a plugin whose reason to exist is registration, export them and add a public-import test.
- [x] **[high] robustness** (done — Wave 3 audit) — `_registrar_uri` (lines 93-97) silently produces malformed URIs.
  Verified: `1000@pbx.example.test` → `1000@pbx.example.test:` (trailing colon), `''` → `':'`. A malformed
  AOR corrupts the request-URI and digest uri and surfaces much later as a confusing gateway rejection.
  Validate scheme + non-empty host in `RegistrationConfig.__post_init__`/`_registrar_uri`, raise
  `ValueError`; tests for empty/no-scheme/no-user.
- [ ] **[medium] correctness** — AOR scheme not constrained to `sips`, contradicting ADR-0005's
  SIP-over-TLS mandate. `_registrar_uri` echoes whatever scheme the AOR carries; a `sip:` AOR over a TLS
  transport is internally inconsistent with no signal. Either document scheme as the transport's
  responsibility, or add a `transport in {TLS,WSS} ⇒ sips:` consistency check; record the stance.
- [ ] **[medium] robustness** (attempted Wave 4 — BLOCKED: a `transport` Literal on RegistrationConfig conflicts with `GatewayConfig.via_transport` dict typing at config.py:684; reconcile both files in one lane) — `RegistrationConfig.transport` and `expires` are unvalidated. `transport`
  is a free str injected into the Via line (line 201); `expires` accepts negatives (`Expires: -1`). Make
  `transport` a `Literal['TLS','WSS','UDP','TCP']`, reject negative `expires` in `__post_init__`
  (AGENTS 17 prefer types). Test an unknown-transport rejection.
- [ ] **[medium] correctness** — Auth re-send does not increment `nc` on a refresh reusing the same
  nonce. `_reauthenticate` (lines 158-177) always uses `nc=1` with a fresh cnonce; a registrar expecting
  monotonic nc within a nonce lifetime is unsupported. Document the fresh-nonce assumption OR thread an nc
  counter; at minimum assert `nc=00000001` appears so the assumption is pinned.
- [ ] **[medium] test** — No test that the Via branch changes between the initial and authed REGISTER
  (RFC 3261 §8.1.1.7 requires a new branch). `_build` does call `new_branch()` each time, but nothing
  pins it; a hoist-to-`__init__` refactor would silently break transaction matching. Extract and assert
  the branches differ (both `z9hG4bK…`); assert Call-ID/From-tag stay equal.
- [ ] **[medium] test** — No registration-level test for the qop-less (RFC 2069) digest path or the
  opaque echo. Every challenge fixture offers `qop="auth"` and no opaque. Add a qop-less challenge test
  (no nc/cnonce, 32-hex response) and an opaque-echo test — guards the registration↔digest seam.
- [ ] **[medium] test** — No test that a fresh nonce in a second 401 (`stale=true`) is used. The flow
  treats any second 401 in a transaction as `Failed` (lines 146-148), so it cannot honour stale-nonce
  rotation; there is no `DigestChallenge.stale` field. Decide scope: parse `stale` + retry, or pin
  "we fail on the second 401 even if stale" with a test.
- [ ] **[low] correctness** — A 2xx other than 200 (e.g. 202) is mishandled — `status == _OK` exact
  compare (line 141) falls through to `Failed`; 1xx provisionals also treated as `Failed`. Treat
  `200 ≤ status < 300` as success (or document 200-only) and ignore 1xx; tests for 1xx-then-200 and
  non-200 2xx.
- [ ] **[low] efficiency** — `_check_cseq` calls `cseq.split()` twice (line 183) and never validates the
  CSeq *method*; a response whose number coincides but whose method is `INVITE` is accepted. Split once,
  validate `parts[1].upper() == 'REGISTER'`; test a method mismatch.
- [ ] **[low] robustness** — Missing/garbled CSeq is silently accepted (lines 181-182), bypassing the
  only correlation check. Reconsider raising; if leniency is deliberate, pin it with a test.
- [ ] **[low] robustness** (partial — residual: registration.py:358-384 — multi-Contact parsing via `_split_contacts`/`_binding_uri` is implemented and our-binding matching exists. Tests f) — `_granted_expires` falls back to the *requested* Expires when the 200 omits
  expiry (lines 188-197), masking a shorter grant → silent registration drop. It also doesn't match the
  Contact against ours. Log/flag the missing-expiry case; tests for multi-Contact/Expires-only/neither.
- [x] **[low] correctness** (done — Wave 3 audit) — `_EXPIRES_PARAM` matches the first `;expires=` across a folded multi-Contact
  header, risking the wrong binding's lifetime. Iterate `headers_all('Contact')`, parse each into
  URI+params, read `;expires` from the binding whose URI matches ours; multi-binding fixture.
- [ ] **[low] polish** — Via `rport`/`branch` assembly is hand-rolled (line 201) and the response Via's
  `received`/`rport` are never read back, so a NATed registration advertises an unreachable Contact.
  Document NAT reflection as the transport's seam (ADR-0005), OR expose the response Via to the transport;
  consider a typed Via builder shared with future INVITE code.
- [ ] **[low] docs** — `build_request` can raise `ValueError` mid-flow (control char in a config field)
  but `start()`/`handle()`/`deregister()` docstrings mention only `RuntimeError`. Document the
  `ValueError` path or validate config in `__post_init__`; test a control char in a config field.
- [ ] **[low] test** — `test_403_yields_failed` asserts only status, not `reason` (mutation-weak); ditto
  the second-challenge test. Assert `outcome.reason`.
- [ ] **[low] test** — No test that `deregister()` resets registered state / cannot be called twice. The
  `_registered = txn.requested_expires > 0` logic is exercised only incidentally; a `> 0`→`>= 0` mutant
  survives. Add register → deregister → 200 → assert a second `deregister()` raises.
- [ ] **[low] api** — `RegistrationOutcome` union has no exhaustiveness guard at call sites and no
  documented dispatch contract (send `Challenged.request`; schedule on `Registered.expires`; surface
  `Failed`). Add a typed `assert_never` example to the module docstring for the future transport author.
- [ ] **[low] polish** — `_Transaction.cseq` duplicates the flow's `_cseq` (lines 88, 154-155, 172-173) —
  two sources of truth invite drift. Comment why the snapshot exists, or derive correlation from one field;
  test that a response with the pre-auth CSeq (after re-auth bumped it) is rejected.
- [ ] **[low] docs** — Module/class docstrings don't state thread-safety / single-flight assumptions
  (mutable `_cseq`/`_txn`/`_registered`, no locking; ADR-0005 = one asyncio loop). Add one line; consider
  a duplicate/retransmitted-response idempotency test.

## src/hermes_voip/rtp.py

- [x] **[high] correctness** (done — Wave 3 audit) — `JitterBuffer` anchors playout on the FIRST arrival, dropping an earlier
  reordered packet at stream start. `push()` sets `_next = seq` on the first packet (lines 188-189);
  `push(12)` then `push(10)` drops 10 (verified). At call start the first packets are the most
  reordering-prone — this permanently loses the greeting / first words feeding STT. Defer anchoring until
  the first `pop()`, or allow `_next` to revise downward while nothing has been emitted; reorder-pair-first
  test.
- [ ] **[medium] correctness** — `target_depth` counts ALL buffered packets, so a far-future cluster
  triggers premature loss for the immediate gap. `pop()` declares `Lost` when `len(_packets) >= _depth`
  (line 200) measuring total occupancy, not contiguous backlog behind the gap (verified with
  depth=3, push 10/100/101/102). Gate loss on backlog ahead of the next contiguous run; at minimum
  document total-occupancy semantics and pin with a test.
- [ ] **[medium] efficiency** — A single far-ahead packet emits a long run of one-`Lost`-per-`pop()`
  (verified: depth=1, push 10 then 50 → 39 separate `Lost`), each allocating a frozen dataclass, spinning
  the media loop. Coalesce into `Lost(sequence, count)` / a run-length scanned in one step; even if the
  per-packet API is kept, add a fast-path and document the per-pop cost.
- [ ] **[medium] robustness** — Malformed RTP padding silently accepted. When the pad byte exceeds the
  payload, the guard (lines 114-117) fails and the raw payload (incl. the bogus pad byte) is returned with
  no error — inconsistent with the other parse paths that raise. Verified: P=1, payload `b'\x05'` →
  `b'\x05'`. Raise `ValueError` when `pad > len(payload)`; decide/document `pad==0`-with-P. Tests both.
- [ ] **[medium] api** (partial — residual: `__len__`, `flush()`, `reset()` are absent; no SSRC-awareness or auto-reset on SSRC change.) — `JitterBuffer` exposes no `__len__`/peek/flush/reset. The media loop needs depth
  (metrics/adaptive), drain-on-BYE (trailing audio), and reset-on-SSRC-change (re-INVITE/source resync) —
  none exist, and an SSRC change mis-classifies the new stream against the stale anchor. Add
  `__len__`/peek/`flush()`/`reset()`; have the buffer know the SSRC and auto-reset on change.
- [ ] **[low] api** — `pack()` cannot re-emit CSRC/extension/padding it can parse (lines 67-79 vs
  100-117), so `RtpPacket` is lossy. Documented as intentional but not a named/tested invariant. State the
  skip-only contract in the docstring; test that parse-of-extension then pack yields a bare header.
- [ ] **[low] robustness** — No length cap on payload/packet size; `pack()` builds multi-kB datagrams and
  the buffer stores them; per-buffered-packet byte size is unbounded. Consider an optional generous
  telephony ceiling, or document the deliberate absence (transport's responsibility).
- [ ] **[low] test** — `max_ahead` window boundary is inclusive but untested at the exact edge (line 185;
  verified next=10/max_ahead=256 keeps 266, drops 267). A `>`→`>=` mutant survives. Document inclusive;
  add `next+max_ahead` (kept) / `+1` (dropped) tests.
- [ ] **[low] docs** — The `_seq_before` (32768) vs `max_ahead` (256) interaction creates an undocumented
  effective window `[next .. next+256]` and a wraparound ambiguity if `max_ahead` nears 32768. Add a
  comment; assert `max_ahead < _SEQ_HALF` in `__init__`.
- [ ] **[low] test** — No test for a duplicate of a packet still buffered (only an already-popped
  duplicate is tested). A `setdefault`→assignment (last-wins) mutant survives. Push two copies of the same
  buffered seq with distinct payloads; assert first-arrival payload wins.
- [ ] **[low] test** — Jitter-buffer tests assert only `.sequence_number`, never payload/timestamp/ssrc
  fidelity through the buffer (the property that matters for STT). Give packets unique payloads/timestamps
  and assert correct payload+timestamp per sequence.
- [ ] **[low] api** — `_seq_before` is underscore-private but imported by tests (blurring the public
  surface). Either promote the RFC 1982 helpers (`seq_before`/`seq_next`/`seq_distance`) to a shared public
  seqnum module (RTCP/DTMF/RTP all need them) or keep private and test only via the buffer.
- [ ] **[low] docs** — Module/class docstrings describe a clean concealment workflow but there is no PLC
  helper and no documented drive protocol; a naive caller could busy-loop `pop()`. Add a "Usage" note
  (pop on the playout tick; on `Lost` run PLC; on `None` wait) and clarify PLC lives in the media loop.
- [ ] **[low] polish** — Header struct format `'!BBHII'` is duplicated between `pack()` (line 72) and
  `parse()` (line 95); `_HEADER_LEN = 12` is maintained independently. Introduce a module-level
  `struct.Struct('!BBHII')`; derive the length from `.size`.
- [ ] **[low] efficiency / docs** — `payload = data[offset:]` copies (correct: jitter-buffered packets
  don't pin the transport's receive buffers). Add a comment so a future zero-copy/memoryview refactor
  doesn't silently reintroduce retention.

## src/hermes_voip/dtmf.py

- [x] **[high] robustness** (#180) — `DtmfReceiver` does not validate `history >= 1`. `history=0` leaks `_seen`
  unbounded (eviction guard at line 175 short-circuits; `_order` is a no-op `maxlen=0` deque) — verified
  growing `_seen`, an unbounded leak on a long call, silently breaking the bounded-window contract.
  `history<0` raises a raw `ValueError` from deque. Validate `if history < 1: raise ValueError(...)`
  mirroring `JitterBuffer`; document the minimum.
- [ ] **[medium] polish** — `_order`/`_seen` are two structures kept in sync by hand (lines 175-178) — the
  source of the `history=0` desync and a mutation-fragile pattern. Replace with a single insertion-ordered
  `dict`/`OrderedDict` (membership + FIFO eviction via `popitem(last=False)`), or add a
  `len(_order)==len(_seen)` invariant assertion/test.
- [ ] **[medium] docs** — `dtmf.py` is a flat module; ADR-0010 prescribes a `dtmf/` package
  (`detector.py`/`rfc4733.py`/`sip_info.py`/`inband.py`/`generate.py`) with `tests/dtmf/`, and the module
  is absent from `__init__` `__all__`. Track the migration so later detector/sip_info/inband/generate work
  lands correctly; add a NOTE acknowledging this is the `rfc4733` codec slice.
- [ ] **[medium] api** — The module does not provide the ADR-0010 `DtmfDigit`/`DtmfMode`/`DtmfDetector`
  seam (rich digit with symbol/duration_ms/source/received_at; async `digits()`/`mode()`); `feed` returns
  a bare `str|None` and requires the caller to pass the RTP timestamp by hand. Wrap `DtmfReceiver` behind
  the detector protocol when that layer is built; track explicitly so primitive and seam don't drift.
- [ ] **[medium] api** — `feed()` conflates "still pressing" / "duplicate end" / "non-digit event" into a
  single `None` (lines 170-172). A controller may need to distinguish a completed flash/non-digit from
  "nothing happened". Return a small result type (enum-tagged / `DtmfPress | None`) or expose whether a new
  end-timestamp was recorded.
- [ ] **[medium] test** — `encode()`'s end-bit/volume byte construction is mutation-weak — one pinned
  vector (`end=True, volume=10` → `0x8A`); no `end=False` (`0x0A`), no `volume=0`/`0x3F`. A `|`→`&` or
  dropped-`_END_BIT` mutant partly survives. Add end=False, volume=0 (`0x80`), volume=0x3F assertions.
- [ ] **[medium] test** — Bounded-window eviction (an evicted timestamp re-emits) is a deliberate tradeoff
  with no test (verified with `history=2`). Fill the window, confirm the oldest is evicted, assert a
  re-feed of the evicted timestamp re-emits — pins the window size semantics.
- [ ] **[low] robustness** — `event_payloads` has no minimum-packet / step-vs-total guard; `step >=
  total_duration` yields only the final packet, losing RFC 4733 incremental updates (verified
  total=160/step=1000). Document the degenerate single-update (and test) or clamp `step <= total_duration`.
- [ ] **[low] api** — Default `volume=10` is an undocumented magic value, and since `event_payloads` is a
  generator, an out-of-range volume raises lazily on first iteration. Add `_DEFAULT_VOLUME = 10` with a
  -dBm0 comment; validate eagerly at call time alongside the step/total checks.
- [ ] **[low] polish** — `digit_to_event` uses `str.find` + a `len==1` guard (line 87) to avoid the
  empty-string match — indirect. Use a precomputed `dict[str,int]`; clearer, O(1), rejects ''/multi-char
  naturally, symmetric with `event_to_digit`.
- [ ] **[low] api** — Module constants lack `typing.Final` (vs `media/audio.py`'s convention) and there is
  no `__all__`. Annotate with `Final`; declare `__all__`.
- [ ] **[low] correctness** — RTP timestamp dedup uses exact-equality on an unbounded Python int while
  RTP timestamps are 32-bit wrapping (unlike `rtp.py`'s serial arithmetic). Correct here (a keypress shares
  one timestamp; the bounded window makes a 2^32 collision a non-issue) but undocumented — invites a wrong
  "fix". Add a one-line comment; optionally assert the timestamp fits 0..2^32-1.
- [ ] **[low] polish** — `0 <= event < len(_DIGITS)` is written three times (`event_to_digit` line 100,
  `feed` line 179, inverse of `digit_to_event`) — mutation-fragile. Extract one
  `_digit_for_event(event) -> str | None`.
- [ ] **[low] test** — `DtmfEvent.__post_init__` branches untested for the flash (16) accepted path and
  the bounds (255 ok / 256 reject; volume 63 ok / 64 reject; duration 65535 ok / 65536 reject). Add them.
- [ ] **[low] test** — `feed()` non-digit (flash=16) path (lines 179-181) untested. Add: `feed(event=16,
  end=True)` returns `None`, and a same-timestamp re-feed also returns `None` (recorded but not surfaced).
- [ ] **[low] test** — Lost-end / start-only-press surfacing (docstring, lines 156-157) untested. Add:
  only non-end packets never emit (end bit is the sole trigger); a lone end packet emits once then dedups.
- [ ] **[low] robustness** — `decode()` ignores the reserved R bit (0x40) without a note (liberal accept;
  not preserved on re-encode). Add a one-line comment; optionally a test feeding R=1.
- [ ] **[low] docs** — Module docstring says the receiver "yields each pressed digit"; `feed()` is a
  push-style `str|None` method, not a generator (rule 27). Reword to "surfaces each pressed digit exactly
  once (one return per completed key-press)".
- [ ] **[low] api** — `_REDUNDANT_END_COUNT=3` is hardcoded with no override, and `event_payloads` has no
  marker-bit / first-packet semantics (RFC 4733 §2.5.1.2 wants marker on the first packet; `rtp.py`'s
  `RtpPacket.marker` exists). Document that marker handling is the transport's job + which index is "first";
  consider making the end-count a keyword arg.

## src/hermes_voip/media/audio.py

- [x] **[high] robustness** (#177) — Decode/resample paths raise `audioop.error` (NOT `ValueError`) for inputs
  the module's own validators would reject — verified `audioop.error` is not a `ValueError` subclass, so
  `except ValueError` (the module's advertised contract) won't catch them. e.g. `Resampler(0, 16000)
  .resample(b'\x00\x00')` raises `audioop.error: sampling rate not > 0`. Validate positive rates (below)
  and/or wrap `audioop.error` into a coherent module error type; document which exceptions propagate.
- [x] **[high] robustness** (done: f219dea) — `Resampler.__init__` does not validate positive rates; the failure is
  deferred to the first `resample()` deep in the C backend (verified `Resampler(0, …)`/`Resampler(-8000,
  …)` construct fine). A config-derived rate of 0 lies dormant until mid-call. Add
  `if from_rate <= 0 or to_rate <= 0: raise ValueError(...)`; tests for each non-positive rate.
- [x] **[high] api** (done: f219dea) — A-law frame-bridge gap: `ulaw_to_frame`/`frame_to_ulaw` exist but there is no
  `alaw_to_frame`/`frame_to_alaw`, despite ADR-0005 advertising PCMA. When a gateway answers PCMA the
  transport must hand-roll frame construction + the 8kHz guard that `frame_to_ulaw` centralises. Add the
  a-law pair, OR better a codec-parameterised `frame_to_g711(frame, codec)` / `g711_to_frame(...)`; mirror
  a-law frame-bridge tests.
- [ ] **[medium] api** — Resample path is byte-oriented; nothing returns/consumes `PcmFrame`, forcing the
  hot-path loop to unwrap frames and re-stamp `sample_rate`/`monotonic_ts_ns` by hand (the error-prone
  part — a forgotten rate update makes the frame lie). Add `resample_frame(frame) -> PcmFrame` that sets
  the new rate and propagates ts; document the ts policy. Keep the byte-level primitives.
- [ ] **[medium] robustness** — `decode_ulaw`/`decode_alaw` validate nothing (asymmetric with the
  validating encode path), so a zero-length/truncated payload becomes a silent empty/short frame. Add
  tests pinning empty-input behaviour; decide whether zero-length payloads should be rejected (here or in
  the transport/jitter layer) and document the deliberate no-validation choice for decode.
- [ ] **[medium] test** — Continuity test only covers 8k↔16k; the 24k↔8k TTS path (ADR-0004/0005) and
  24k↔16k are untested — exactly the non-integer-ratio cases where boundary clicks appear. Extend the
  parametrize to include 24000↔8000/16000↔24000/24000↔16000; add sample-count sanity for 24k↔8k.
- [ ] **[medium] test** (partial — residual: wide resample tolerance `300 <= out_samples <= 340` (line 63) unchanged; no a-law near-lossless value test; no golden-vector assertion (e.g.) — Mutation-weak assertions: wide resample tolerances (`300 <= … <= 340`); the
  ulaw/alaw one-byte-per-sample tests assert no decoded values (a `lin2ulaw`→`lin2alaw` swap survives);
  a-law has NO round-trip value assertion at all (a `decode_alaw`→`ulaw2lin` mutant survives). Add an
  a-law near-lossless value test; tighten counts; add an `encode_ulaw != encode_alaw` discrimination test;
  add golden-vector assertions (e.g. mu-law of PCM 0 is 0xFF).
- [ ] **[low] efficiency** — Every encode/decode/resample returns a fresh `bytes`; ~200 short-lived
  allocations/sec/call with no buffer reuse and no measurement recorded (AGENTS 22 wants a number).
  audioop has no in-place variant, but add a microbenchmark of per-frame encode+resample+decode latency
  and record it against the ADR-0005 budget; consider batching frames per `ratecv` call (state-carrying,
  correctness-preserving).
- [ ] **[low] docs** — `ratecv` low-pass weights are left at defaults `(1, 0)` with no comment — a real
  audio-quality decision for the STT path. Add a one-line comment (defaults adequate for narrowband
  speech) or expose weights as params; record in ADR-0005.
- [ ] **[low] test** (partial — residual: no assertion against a known-good G.711 reference value (e.g. `encode_ulaw(pcm16_of_0) == b'\xff'`); bare `decode_ulaw`/`decode_alaw` distin) — No test pins that `decode_ulaw`/`decode_alaw` are distinct codecs / that
  `ulaw_to_frame` uses mu-law specifically (the frame round-trip uses mu-law both ways, so a global
  mu→a swap still passes). Assert against a known-good G.711 reference value.
- [ ] **[low] polish** — `Resampler` forbids equal rates but there's no symmetric guard catching
  resample-into-an-already-correct-rate, and the rate-label invariant is enforced only by `frame_to_ulaw`.
  Consider an `expect_rate(frame, hz)` validator centralised in this rate-authority module.
- [ ] **[low] docs** — `media/__init__.py` docstring claims the media layer owns RTP/SRTP/jitter/DTMF, but
  `rtp.py`/`dtmf.py` live at the top level (only `audio.py` is under `media/`) — aspirational (rule 27).
  Either move them under `media/` or correct the docstring to what the package owns today + cross-reference
  the siblings.
- [ ] **[low] docs** — `audio.py` docstring calls audioop "typed" — strict-clean status depends on
  `audioop-lts`'s bundled stubs at the pinned version. Drop "typed" or note the dependency so a future bump
  that drops stubs is a deliberate decision, not a surprise `# type: ignore`.
- [ ] **[low] polish** — No `__all__` (vs the root package's convention). Add one listing the public
  codec/resampler/frame-bridge symbols + `G711_SAMPLE_RATE`.
- [ ] **[low] polish** — `_MONO` is private while `G711_SAMPLE_RATE` is public; the mono-only assumption
  is silent (a stereo buffer has even length so `_validate_pcm16` passes and produces garbage). Document
  the mono-only contract in the function docstrings (raw-bytes callers carry no channel metadata).
- [ ] **[low] docs** — `Resampler` carries mutable `_state` but the docstring doesn't state the
  concurrency contract; two coroutines sharing one would silently corrupt both streams. Add: not safe for
  concurrent use; one coroutine/thread per stream-direction-per-call.

## src/hermes_voip/providers/ (audio, asr, tts, guard, policy, transport, registry)

- [x] **[high] test** (done — Wave 3 audit) — Async Protocol methods are never awaited/executed in any test. The conformance
  suite touches only sync members (`input_sample_rate`, `isinstance`); `screen`/`connect`/`send_audio`/
  `inbound_audio`/`stream`/`synthesize`/`flush`/`cancel`/`__anext__` are never run (`runtime_checkable`
  only checks attribute presence). A `def`-instead-of-`async def` or non-iterator fake passes. Add async
  tests that drive each fake (await `screen`, iterate `inbound_audio`/`stream`/the TtsStream, await
  `connect`/`flush`/`cancel`).
- [x] **[high] test** (done — Wave 3 audit) — No async test runner dependency. `pyproject.toml` lists `pytest==9.1.0` but no
  `pytest-asyncio`/`anyio` and no `asyncio_mode`; plain pytest can't run `async def test_…`, which is why
  the async contract is untested. Add and pin `pytest-asyncio` (or `anyio`), set `asyncio_mode = "auto"`,
  regenerate `uv.lock`, convert the conformance tests to await the async members.
- [ ] **[medium] robustness** — `PcmFrame` performs no validation (no `__post_init__`). An odd-length
  `samples` truncates silently (`len // 2`, line 36); `sample_rate=0`/negative passes downstream. The
  canonical 50pkt/s currency type bypasses `media/audio._validate_pcm16`. Add `__post_init__` asserting
  whole-sample length and `sample_rate > 0` (propagate per rule 37); if the per-frame cost is a concern,
  use a `PcmFrame.validated(...)` factory and document the choice.
- [ ] **[medium] robustness** — `Transcript.confidence` and `GuardResult.score` advertise 0.0..1.0 but
  never enforce it (asr.py:20, guard.py:35). For `score` this is security-adjacent (ADR-0009 thresholds
  on it). Validate in `__post_init__` (reject out-of-range/NaN) or introduce a branded `Probability`
  newtype; tests for the range.
- [x] **[medium] correctness** (done — Wave 3 audit) — `GuardSessionState.flagged_turns` is declared but never populated by
  `record()` (policy.py:41, 43-45 fold only `degraded`); `GuardResult` carries no turn id, so it *cannot*
  be populated (aspirational field, rule 27). Either wire it up (give `screen`/`GuardResult` a turn id and
  append on warranting verdicts, with a test) or remove it until implemented (rule 6).
- [x] **[medium] correctness** (done — Wave 3 audit) — `record()` ignores `GuardResult.verdict` entirely — only `degraded` is
  folded (policy.py:45), so REFUSE/RESTRICT produce no session-state change and cumulative risk (ADR-0009)
  is unrepresentable. Implement the cumulative-risk model (at minimum count suspicious turns) with tests,
  or correct the docstring to stop implying cumulative risk.
- [ ] **[medium] robustness** — `gate_tool_call` returns a bare bool — a hard-block leaves no audit trace
  of *why* (unconfirmed vs degraded), exactly what an operator needs. Return a typed `GateDecision(allowed,
  reason)` with a discriminated `GateReason`, or accept an audit sink; strengthen tests beyond `is
  True/False` to assert the reason.
- [ ] **[low] api** — `policy.py` imports `GuardResult` only to read one bool in `record()` — more
  coupling than needed. Use `record(self, *, degraded: bool, …)` or a narrow `Protocol`; note the decision
  if guard/policy are intentionally one ADR-0009 unit.
- [ ] **[low] api** — `ProviderRegistry` exposes no `__contains__`/`has`/`unregister`/get-factory, no
  memoisation (`make` always instantiates despite the "resolve at startup" intent), and `T` is unbounded.
  Add `__contains__`/`has`, consider a memoising `make` for the singleton case, optionally bound `T`;
  document that `make()` instantiates fresh.
- [ ] **[low] test** — Registry tests don't assert the factory is called lazily/exactly once; the
  duplicate-message assertion is a loose substring. Add a counter fake (0 after register, 1/2 after
  make(s)); tighten the duplicate assertion to include kind + quoted name.
- [ ] **[low] efficiency** — `names()` re-sorts the dict each call (registry.py:49) — fine (startup/
  diagnostic), but add a one-line comment so a future caller doesn't reach for it per-turn.
- [ ] **[low] api** (partial — residual: the six individual modules (asr.py, tts.py, guard.py, policy.py, transport.py, registry.py) still define no __all__, so deep-path imports ar) — `providers/__init__.py` exports nothing (no `__all__`, no re-exports); none of the
  seven modules define `__all__`. The most-imported contract (ADR-0004) forces deep-path imports. Re-export
  the canonical public names from the package under one `__all__`.
- [ ] **[low] api** — `GuardVerdict`/`ToolRisk` are documented "ascending severity" but derive from plain
  `Enum` (uncomparable) — the claim implies a comparability that doesn't exist (rule 27). Make them
  `IntEnum`/add a `severity` property if thresholding is needed, else soften the docstrings.
- [ ] **[low] api** — `GuardVerdict`'s non-ALLOW members have no enforcement linkage to `ToolRisk` —
  `gate_tool_call` never consults the verdict, so a RESTRICT verdict doesn't itself clamp the toolset
  (the documented clamp is absent). Implement a verdict-aware clamp (CLARIFY blocks all tools this turn;
  RESTRICT blocks ELEVATED+IRREVERSIBLE) with tests, or move/reference the mapping to its documented home.
- [ ] **[low] api** — ASR `stream()` has no symmetric teardown vs `TtsStream.flush()/cancel()` (no
  `aclose()`); barge-in/early-hangup teardown relies on GC. Document the ASR teardown contract (require
  `aclose()`/`AsyncGenerator`, or state closing the input iterator is the sole signal) to align both seams.
- [ ] **[low] efficiency** — `MediaTransport.send_audio` is one-frame-at-a-time (50 awaits/s/direction)
  with no batched send and no backpressure signal (returns `None`). Record the per-frame choice as
  deliberate in the efficiency pass; consider a backpressure return or internal pacing (AGENTS 22).
- [ ] **[low] test** — No property-based tests for the audio invariants (no `hypothesis`); equality/hash
  tested for one equal pair but never inequality (frames differing only in ts/rate never asserted `!=`).
  Add inequality assertions; consider a hypothesis `sample_count` test over arbitrary bytes.
- [x] **[low] test** (done — Wave 3 audit) — The conformance fakes' empty async generators (`return; yield`) only exercise the
  empty-stream path, so non-empty iteration is never validated. Add fakes that yield a scripted sequence
  and drain them asserting exact order.
- [x] **[low] test** (done — Wave 3 audit) — `TtsStream` flush/cancel barge-in (the single most important TTS behaviour) has no
  behavioural test — `isinstance` ignores signatures, flush/cancel are never awaited. Add a fake whose
  `cancel()` flips a flag and whose `__anext__` then raises `StopAsyncIteration`; assert post-cancel
  iteration is empty and `flush()` emits buffered frames.
- [ ] **[low] docs** — Several Protocol docstrings reference behaviour the seam can't enforce / name
  vendors (asr.py "VAD signal" with no VAD type; tts.py cancel maps to Deepgram/Cartesia/ElevenLabs/
  sherpa-onnx — lock-in-by-documentation in the vendor-neutral core; transport.py "Hides … DTMF" but the
  Protocol exposes no DTMF method). Trim vendor names (point to ADR-0007); clarify whether DTMF surfaces
  here or via ADR-0010; audit each cross-ADR claim (rule 27).
- [ ] **[low] robustness** — `PcmFrame` carries no channel-count/encoding marker — "mono PCM16-LE" is
  convention-only; `sample_count` assumes mono. Keep it strictly mono16 + add the length check, or encode
  channels explicitly; document mono16-LE as an invariant, not a hint.
- [ ] **[low] robustness** — `registry.make` calls `factory()` with no context on a construction failure
  (the unknown-name path *is* contextualised). Wrap with exception chaining (`raise ProviderInitError(...)
  from exc`) while still propagating (rule 37); test a raising factory surfaces a contextual error.

## Cross-cutting

These span multiple modules or the repo as a whole.

- [x] **[high] test/infra** (done — Wave 3 audit) — **No async test runner.** The entire provider seam is async yet untestable
  as written (no `pytest-asyncio`/`anyio`, no `asyncio_mode`). This blocks behavioural coverage of every
  streaming/transport/guard contract. Add + pin the runner, set `asyncio_mode`, regenerate `uv.lock`.
  (Listed per-module under providers too; called out here because it gates the whole async surface.)
- [ ] **[medium] polish/DRY** — **Duplicated control-character injection guard.** Identical constants +
  predicate in `message.py` and `digest.py`. A single shared helper prevents drift of a security
  invariant. (See message.py item.)
- [ ] **[medium] polish/DRY** — **RFC 1982 serial-number arithmetic** is implemented in `rtp.py`
  (`_seq_before`/`_seq_next`) and conceptually needed by `dtmf.py` (timestamp dedup) and future RTCP/RTP.
  Decide: promote to a shared public seqnum module, or keep private and document why each consumer's
  approach differs (rtp uses serial arithmetic; dtmf uses exact-equality + bounded window — both correct
  for their case).
- [ ] **[medium] api/consistency** — **`__all__` discipline is inconsistent.** The root `__init__`
  declares `__all__`; `media/audio.py`, `dtmf.py`, `message.py`, and every `providers/*` module do not.
  Adopt `__all__` uniformly so the public surface is explicit and private helpers stay out of
  star-imports/autodoc.
- [ ] **[medium] api/consistency** — **`typing.Final` on module constants is inconsistent.**
  `media/audio.py` annotates public constants with `Final`; `dtmf.py`/`rtp.py` do not. Adopt `Final`
  uniformly for mutation-resistance and consistency.
- [ ] **[medium] correctness/consistency** (partial — residual: audioop.error not wrapped/documented as propagating.) — **Exception-type contract is inconsistent across the
  foundation.** Some guards raise `ValueError`/`SdpError`, others leak `AttributeError` (message.py
  reason-less status) or `audioop.error` (media/audio decode/resample). Define and document one coherent
  exception contract per layer (parse layers raise `ValueError`/domain errors on hostile input; never leak
  a foreign type a caller wouldn't catch). Audit each public function's docstring against what it actually
  raises (rule 27, rule 37).
- [ ] **[medium] robustness/consistency** (partial — residual: Resampler rate validation IS now implemented (src/hermes_voip/media/audio.py:201-215: validates positive rates and plain int). However: src/) — **Constructor-time validation is uneven.** `JitterBuffer`
  and `DtmfEvent` validate in `__init__`/`__post_init__`; `Resampler` (rates), `DtmfReceiver` (history),
  `RegistrationConfig` (aor/transport/expires), and `PcmFrame` (length/rate) do not — deferring failures
  to deep, mid-call sites. Standardise fail-fast construction across the foundation.
- [ ] **[medium] api/consistency** (partial — residual: provider Protocols/types (PcmFrame, MediaTransport, AsrProvider, TtsProvider, GuardProvider) and media codecs are still deep-import only; no) — **Public-import discoverability.** Only `sip_address_of_record` is
  exported from `hermes_voip/__init__.py`; `RegistrationFlow`, the provider Protocols/types, and the media
  codecs are reachable only via deep paths and are untested at the package boundary. Decide a deliberate
  public surface and pin it with import tests.
- [ ] **[medium] test/quality** — **Mutation-resistance is a standing gap.** Multiple modules have
  assertion-weak or negative-control-missing tests (digest quoting table, message token generators,
  rtp payload fidelity, dtmf encode byte, media/audio codec identity, registration reason/branch/stale,
  sdp ordering). AGENTS 19 names mutation score as the target; run a mutation pass (e.g. `mutmut`/
  `cosmic-ray` as dev tooling) over the foundation and close the surviving-mutant gaps.
- [x] **[low] docs** (done — Wave 3 audit) — **No `docs/plan/` until now.** The phased plan lives in
  `docs/plan/IMPLEMENTATION-PLAN.md` (this change). Keep it current as units land (rule 42 spirit for
  plans).
- [ ] **[low] docs/consistency** — **Module docstrings vs reality (rule 27).** `media/__init__.py`
  (claims RTP/SRTP/DTMF ownership), `dtmf.py` ("yields"), `sdp.py` `o=` re-INVITE claim, several provider
  Protocol docstrings (vendor names, DTMF, VAD) all describe behaviour the code doesn't have or own. Sweep
  for aspirational docs whenever a module is next touched.
- [ ] **[low] efficiency/process** — **No recorded efficiency budget numbers (AGENTS 22).** The hot-path
  modules (`media/audio.py`, `rtp.py`, the provider send/recv seam) have no measured per-frame latency/
  allocation numbers against the ADR-0005 budget. Add microbenchmarks and record the numbers (not "done")
  when the media plane is wired; until then, the cold-path-vs-hot-path framing should be stated in comments
  so future agents don't mis-optimise cold paths (digest/message/sdp/registration are all cold).
- [ ] **[low] supply-chain/process** — **Model-artifact licence/checksum gate is a separate axis from the
  package licence gate.** ADRs 0006/0007/0009 each require a model-specific licence+checksum CI gate
  (repo + pinned revision + filenames + sha256 + SPDX allow-list) distinct from `pip-licenses`. Track that
  the existing `supply-chain.yml` covers only Python packages; the model gates land with each provider.

## src/hermes_voip/dialog.py

- [ ] **[low] correctness** — `build_in_dialog_request` can emit a CSeq >= 2**31, the exact value its own
  parser rejects (RFC 3261 §8.1.1.5). `dialog.py:230` computes `next_cseq = dialog.local_cseq + 1` with no
  upper-bound guard, while the parse path (`_cseq`, lines ~339-345) explicitly raises `DialogError` when
  `sequence >= _MAX_CSEQ (2**31)`. A dialog at `local_cseq = 2**31 - 1` produces `CSeq: 2147483648 BYE`
  (verified) — a value the same module would reject on parse. Fix: guard `next_cseq` against `_MAX_CSEQ` in
  `build_in_dialog_request`; TDD red test asserting the dialog at `2**31 - 1` raises rather than emitting
  the out-of-range value.

## src/hermes_voip/incall.py

- [ ] **[medium] correctness** — An offer-carrying re-INVITE with no `m=audio` line is misclassified as
  `OfferlessReinvite` and answered with a fresh OFFER, violating RFC 3264 offer/answer. `classify_inbound_reinvite`
  returns `OfferlessReinvite()` whenever `offer.audio is None` (`incall.py`) — conflating "no SDP body at all"
  with "an SDP body that parses but carries no m=audio". `call.py:729-732` (`_on_reinvite`) then treats
  `OfferlessReinvite` by re-OFFERING, but a re-INVITE that carried any SDP body is an offer per RFC 3264;
  the 2xx must carry an ANSWER, never a fresh offer. Fix: distinguish "empty body" (`OfferlessReinvite`) from
  "non-empty body without usable audio" (should yield 488 or an explicit reject-audio answer). TDD: red test
  feeding a non-empty SDP body with no `m=audio` and asserting no fresh offer in the 2xx (`incall.py`,
  `call.py`).

## src/hermes_voip/stt/deepgram.py

- [x] **[high] robustness** (#174) — `_map_event` (line 284): `event = json.loads(raw)` has no `try/except`. A
  single malformed text frame from the Deepgram Flux WebSocket raises `json.JSONDecodeError`, which propagates
  through `_receive()` → `_generate()` → `asyncio.TaskGroup` → cancels `_pump` and `_delivery` → the call
  ends. A one-line `try/except json.JSONDecodeError` that logs and returns `None` makes bad frames survivable.
  Not tracked previously.

## src/hermes_voip/transport/

- [ ] **[medium] robustness / test** — SIP parse error in TLS/WSS `_dispatch` kills the entire connection and
  drops all active calls with no regression test. `transport/connection.py:601-605` and
  `transport/ws_connection.py:364-368` call `SipRequest.parse()` / `SipResponse.parse()` with no
  `try/except`; a single malformed message tears down all calls simultaneously. The behaviour is documented as
  intentional but has zero regression tests for: (1) parse error → connection loss (not silent drop);
  (2) reconnect supervisor fires; (3) active calls terminate cleanly. Needs tests in `tests/test_adapter_reconnect.py`.

## src/hermes_voip/guard/_onnx_runtime.py

- [ ] **[low] robustness** — `_injection_label_index` (line 146): `json.loads(config_path.read_text(...))` has
  no `try/except`. A corrupt `config.json` (partial write, truncated model download) raises a bare
  `json.JSONDecodeError` with no context about which file or why. Wrap with
  `ValueError(f"corrupt config.json at {config_path}: {e}") from e` for actionable diagnostics.

## src/hermes_voip/registration.py (Wave-1 additions)

- [x] **[high] security** (#173) — SHA-256→MD5 digest downgrade: `registration.py:285` picks only the FIRST
  `WWW-Authenticate` challenge (`SipResponse.header()` returns first-match-only, confirmed `message.py:78`).
  RFC 8760 §2.4 requires choosing the most-preferred supported algorithm when a registrar sends multiple
  challenges (e.g. SHA-256 AND MD5). The strongest-algorithm machinery in `digest.py` (`_ALGORITHM_PREFERENCE`
  lines 46-50) exists but is never used to select among offered challenges — only to validate a single token.
  Fix: read ALL challenge headers via `headers_all`, parse each, select the strongest via the existing
  preference order. TDD: red test feeding two `WWW-Authenticate` headers (MD5 first, SHA-256 second) asserting
  the Authorization echoes `algorithm=SHA-256` (`registration.py`, `digest.py`, `message.py`).

## src/hermes_voip/adapter.py (Wave-1 additions)

- [x] **[medium] security** (#190) — Outbound dial Request-URI built by unvalidated interpolation
  `sip:{extension}@{host}` (`adapter.py:1601`) from the agent-supplied `number`/`extension`. The only
  downstream guard is `message.py _reject_controls` which rejects only C0/DEL — not `@`, spaces, or SIP URI
  metacharacters. Additionally, `outbound_allow.py`'s documented URI-form allowlist entries (e.g.
  `sip:1000@pbx.example.test`) can never match the dial path because `extension="sip:1000@pbx.example.test"`
  interpolates to `sip:sip:1000@pbx.example.test@<host>` — the documented URI-form feature is
  broken/aspirational (rule 27). Two-part fix: (a) validate `extension` against RFC 3261 user/token grammar
  before interpolation; (b) make outbound_allow + dial consistent on URI-form entries. TDD: red test that an
  allowlisted `extension` containing `@evil.com` is rejected before any INVITE is built
  (`adapter.py`, `outbound_allow.py`, `message.py`, `tests/test_outbound_allow.py`).
- [ ] **[high] observability** — Wire RTCP `CallQuality` snapshot to structured log fields. `adapter.py:5181-5190`
  logs RTCP teardown quality as a plain `%`-formatted string with no `extra={}` kwargs (runbook-0014 §Packet
  loss & jitter marks emission as "SOURCE EXISTS, EMISSION TBD"). No test asserts the teardown log line is
  emitted when `_rtcp_active` is True — every adapter test that reaches `_teardown_call` explicitly sets
  `engine._rtcp_active=False` to skip the path (`tests/test_adapter.py:680,746,1628,2086,2148,2247`). Emit
  structured log dict with `event='rtcp_call_quality'`; add a test with a fake engine where
  `_rtcp_active=True` using `pytest caplog`.
- [ ] **[high] observability** — Add structured per-call lifecycle log events with machine-parseable fields for
  SLO counting. Current log lines at `adapter.py:2819-2822` (INVITE received), `:2852-2856` and `:2944-2950`
  (REJECTED), `:4722` (200 OK), `:4861` (CallLoop started) use positional `printf`-style with no `extra=`
  dict. Runbook-0014 §Call setup success marks these events as NOT YET INSTRUMENTED. Emit
  `logging.info(..., extra={'event':'invite_received','call_id':...,'outcome':'rejected','sip_code':488})`
  — no new infra, stdlib `logging` supports `extra=` natively.
- [ ] **[medium] observability** — Emit per-call duration and concurrent-call gauge to structured log at
  teardown. `adapter.py:902` maintains `_admitted_calls` (live set) whose `len()` is the real-time concurrency
  count but is never logged at admission (`:5248`) or release (`:5258`). Call duration is never logged: the
  INVITE-received timestamp is a log string only (`adapter.py:2819`) with no `time.monotonic()` anchor and
  `_teardown_call` (`:5173`) has no start-time to subtract. Runbook-0014 §Concurrent calls marks
  `voip.calls.active_count`/`started`/`ended` as NOT YET INSTRUMENTED. Add `time.monotonic()` at admission;
  log `duration_s` and `len(_admitted_calls)` at release.
- [ ] **[medium] observability** — Log RTCP dormant-path reason per call so operators know which calls lack
  quality data. When RTCP stays dormant (secured call without SRTCP keys, kill-switch off, or PT conflict),
  `adapter.py:5179` silently skips the call-quality log — no record that a given call produced no quality data
  or why. On the live test gateway (SDES-SRTP, runbook-0014 §Secured paths: RTCP is dormant), every
  production call is silent at teardown. Emit a single `DEBUG/INFO` "INVITE %s: RTCP dormant — no call
  quality data (secured=%s, enabled=%s)" at teardown (`adapter.py`, `media/engine.py`).
- [ ] **[medium] observability** — Add test asserting RTCP call-quality INFO log is emitted at teardown when
  `_rtcp_active` is True. No existing test covers `adapter.py:5179-5190`; every adapter test sets
  `engine._rtcp_active=False` to skip it. A parameterised test with a fake engine where `_rtcp_active=True`
  and `call_quality` returns a known `CallQuality`, using `pytest caplog`, pins this mutation-invisible path
  (`tests/test_adapter.py`).

## src/hermes_voip/media/call_loop.py

- [ ] **[medium] test** — Add REFUSE-verdict caller-activity tests to `CallLoop` (two mutation-surviving
  paths). In `_screen_and_deliver` (`call_loop.py:2015-2027`): (1) line 2017 sets `_caller_active_in_window
  = True` even on REFUSE — a mutant omitting this assignment would silently trigger spurious reprompts; (2)
  the `if result.verdict is not GuardVerdict.REFUSE:` guard (line 2020) prevents `_schedule_comfort_filler`
  from firing — a mutant that always schedules the filler on refused turns would survive because
  `test_refuse_verdict_blocks_deliver_turn` (`tests/test_call_loop.py:329`) only asserts `delivered == []`
  without inspecting filler scheduling or the caller-active flag. Not tracked previously.
- [ ] **[medium] observability** — Instrument per-turn latency (STT-finalize → first TTS frame) with a
  structured log event. Runbook-0014 §Per-turn latency defines `voip.turn.latency_ms` as NOT YET INSTRUMENTED
  ("manual estimate from logs"). `call_loop.py` logs "asr: delivering turn" only at DEBUG (line 1294-1299);
  `speak()`'s `on_first_frame` fires only for the greeting (line 1953). Add `time.monotonic()` at
  `transcript_q.put` (line 1300) and at each `on_first_frame` of the delivery path; log
  `event='turn_latency_ms'` at INFO. Zero new dependencies.
- [ ] **[medium] observability** — Instrument time-to-first-audio latency (INVITE received → first RTP tx)
  with a structured log event. Runbook-0014 §Time to first audio defines `voip.media.first_audio_latency_ms`
  as NOT YET INSTRUMENTED (target < 2 s). No `time.monotonic()` is stored at INVITE receipt (`adapter.py:2819`);
  the "greeting: first RTP sent" log (`call_loop.py:1953`) carries no `call_id` and no elapsed time. Thread
  the INVITE receipt timestamp into `CallLoop` construction; log `first_audio_latency_ms=...` at INFO at
  first-RTP-sent (`adapter.py`, `media/call_loop.py`).
- [x] **[high] ux** (#188) — Reprompt/goodbye phrases and no-input timing have no operator config surface
  (ADR-0057 follow-on never plumbed). `config.py` has zero `HERMES_VOIP_NO_INPUT` / `GOODBYE` / `REPROMPT`
  env parsing (verified: `grep -rn 'HERMES_VOIP_NO_INPUT\|HERMES_VOIP_GOODBYE\|HERMES_VOIP_REPROMPT'
  config.py` returns nothing). `MediaConfig` carries no such fields; the adapter's `CallLoop` construction
  (`adapter.py:4810-4852`) passes greeting/comfort-filler kwargs but NONE of the no-input/goodbye kwargs —
  so every call runs `CallLoop`'s hardcoded English defaults (`_DEFAULT_GOODBYE_PHRASE='Goodbye.'`,
  `_DEFAULT_NO_INPUT_REPROMPT_PHRASES` in `media/call_loop.py:162-183`). Fix: add
  `HERMES_VOIP_GOODBYE` / `HERMES_VOIP_GOODBYE_PHRASE` / `HERMES_VOIP_NO_INPUT_*` env parsing + `MediaConfig`
  fields mirroring comfort-filler; thread into adapter's `CallLoop` kwargs; update runbook-0015. TDD:
  red config-parse + adapter-wiring tests first (`config.py`, `adapter.py:4810`, `media/call_loop.py:162`,
  `docs/adr/0057-conversational-ux-silence-goodbye-streaming.md`, `docs/runbooks/0015-voip-silence-reprompt-and-goodbye.md`).
- [ ] **[medium] ux** — Multi-language UX is advertised everywhere but every non-`'en'` language is rejected
  at startup. `_SUPPORTED_LANGUAGES` is derived solely from `_COMFORT_FILLER_PHRASES_BY_LANGUAGE` which has
  only an `'en'` key (`config.py:311,330`), so `ConfigError` is raised for any other code. Even within 'en':
  `media_cfg.language` is consumed in exactly two places repo-wide (`adapter.py:1332` safe_error_reply, and
  comfort-filler selection); `DEFAULT_GREETING` (`config.py:56`), reprompt set, and goodbye line are bare
  English literals with no language dict. Data-mostly fix: add at least one real non-`'en'` phrase set across
  comfort-filler + reprompt + goodbye + greeting + `safe_error_reply`; key each by language dict; add the
  language to the supported set (`config.py:56,311`, `provider_error.py`, `media/call_loop.py:162`,
  `adapter.py:1332`).
- [ ] **[low] ux** — Inbound greeting is not language-keyed even within the existing language mechanism.
  `DEFAULT_GREETING` is a single English literal (`config.py:56`) and `_parse_greeting` returns it for any
  language (`config.py:1811-1821`) — unlike comfort filler which selects from a language-keyed dict. The
  greeting is the first thing every inbound caller hears (ADR-0002 NAT-latch). Making greeting a
  language-keyed default (a `_DEFAULT_GREETING_BY_LANGUAGE` dict consumed by `_parse_greeting`, mirroring
  comfort filler) closes an inconsistency. TDD: red test that a configured non-`'en'` language selects that
  language's built-in greeting when `HERMES_SIP_GREETING` is unset (`config.py:56,1811`).

## .github/workflows/

- [ ] **[medium] security** — Supply-chain advisory audit only fires on dependency-file changes — no scheduled
  run, so a newly-disclosed CVE against an unchanged pinned dep is invisible to CI. `.github/workflows/supply-chain.yml`
  triggers ONLY on `pull_request`/`push` filtered to paths `[pyproject.toml, uv.lock, supply-chain.yml]`;
  `grep -rn 'schedule|cron' .github/workflows/` returns nothing. pip-audit / OSV databases update
  continuously; the pyjwt PYSEC-2026-175..179 case (ADR-0062) is exactly this class. Fix: add a
  `schedule: - cron:` trigger (e.g. `7 6 * * *`) running the existing `uv run pip-audit` job. Update
  `docs/runbooks/0003-supply-chain-audit.md` in the same commit (rule 42).

## docs/runbooks/

- [x] **[high] docs** — Runbook 0012 numbering collision: two runbooks share the same prefix.
  `docs/runbooks/0012-voip-acoustic-echo-cancellation.md` (committed 2026-06-17) and
  `docs/runbooks/0012-voip-inbound-call-context.md` (committed 2026-06-18) both occupy the same number slot,
  breaking the monotone numbering convention. Fixed: renamed `0012-voip-acoustic-echo-cancellation.md` to
  `0018-voip-acoustic-echo-cancellation.md` (first free slot; 0017 was already taken by
  `0017-devcontainer-resources.md`).
- [x] **[high] docs** — AEC runbook default is stale: says 16 ms; code default is 64 ms since commit ff73953.
  `docs/runbooks/0018-voip-acoustic-echo-cancellation.md` had three stale references to the old 16 ms default
  for `HERMES_VOIP_AEC_FILTER_MS`: the knobs table (line 26), and the two verify-output examples (lines 99-100).
  `config.py:256` `_DEFAULT_AEC_FILTER_MS = 64` (verified). Fixed: updated table and both example outputs to 64 ms.
- [ ] **[medium] docs** — Add outbound SIP CANCEL coverage to runbook 0007. ADR-0069 (outbound SIP CANCEL,
  RFC 3261 §9.1) is Accepted and fully implemented (`transport/connection.py:418` `send_cancel`,
  `adapter.py:2309` `abort_call`, `adapter.py:2347` `_ring_timeout`). `docs/runbooks/0007-voip-outbound-calling.md`
  has zero CANCEL coverage: no mention of what happens when an outbound ringing call is aborted, no log line
  to watch for, no mention of the ring-timeout auto-CANCEL path, and no note that `ring_timeout_secs` on the
  WSS transport raises `NotImplementedError`. Rule 42: update runbook 0007 with CANCEL mechanics.
- [ ] **[low] docs** — Document `HERMES_VOIP_CALL_ON_CONNECT` in `plugin.yaml` `optional_env` and runbook
  0007. This live operator-facing env var (`adapter.py:483`, `:993`) bypasses the
  `HERMES_VOIP_OUTBOUND_ALLOW` allowlist (`adapter.py:920-921`) and fires a one-shot outbound dial on first
  registration. It has no entry in `plugin.yaml optional_env`, no "how to set / verify / disable" section in
  runbook 0007, and the allowlist bypass is undocumented. An operator who sets this var without knowing it
  bypasses the security gate may dial unintentionally.
- [ ] **[low] operability** — Document and validate `HERMES_VOIP_KEEPALIVE_INTERVAL`. Read from env at
  `adapter.py:992-993` via bare `float()` with no validation, no `ConfigError` on invalid input, not declared
  in `config.py`, not in `plugin.yaml optional_env`, not mentioned in any runbook. Controls RFC 5626 §5.4
  double-CRLF keepalive interval (ADR-0038, default 30 s). Setting it to 0 or a negative number suppresses
  keepalives; a non-numeric value crashes with an untyped `ValueError` at connect time rather than a readable
  `ConfigError`. Fix: add validation and declaration in `config.py`; add to `plugin.yaml`; document in a
  runbook (`adapter.py`, `config.py`, `plugin.yaml`).
- [x] **[high] docs** (#187, runbook 0019) — No runbook documenting the release / version-bump process. AGENTS.md rule 42 requires
  runbooks as you work for any operational process. No runbook documents how to release hermes-voip: which
  three version strings to update (`pyproject.toml:3`, `src/hermes_voip/__init__.py:49`,
  `packaging/hermes-plugins/hermes-voip/plugin.yaml:27`), in what order, how to verify sync, whether to
  build/test the wheel, and where to publish. The plugin-manifest tests guard against drift after the fact but
  do not serve as a runbook.

## README.md

- [x] **[high] docs** — README Security section falsely stated call transfer is unavailable. `README.md:587-588`
  said "call transfer stays unavailable until its spoof-resistant confirmation channel ships." This was factually
  wrong: `transfer_blind` and `transfer_attended` are both in `plugin.yaml provides_tools` (lines 49-50);
  `dtmf_confirm.py` implements `ArmedConfirmation` (the spoof-resistant DTMF confirmation channel, ADR-0010/0031);
  `refer.py` implements REFER/Replaces; `voip_tools.py` has `transfer_blind_handler` and
  `transfer_attended_handler`. Fixed: Security section now accurately describes the shipped trust model
  (transfer is available at operator privilege level, gated by DTMF confirmation for blind and outbound
  allow-list for attended).
- [ ] **[medium] docs** — README reports stale tool count: "9 tools, 1 hook" should be "10 tools, 1 hook".
  `README.md:210` says the plugin registers `9 tools, 1 hook`. `plugin.yaml provides_tools` (lines 41-50) has
  10 entries including `transfer_attended` (added in commit `d8097cc`). The discrepancy will mislead operators.

## docs/plan/IMPLEMENTATION-PLAN.md

- [ ] **[medium] docs** — `IMPLEMENTATION-PLAN.md` is massively stale: describes the entire plugin as
  not-started. Line 25 marks ADR-0002 as not-started and says "No `register(ctx)`, no `plugin.yaml`, no
  `VoipAdapter`". In reality: `plugin.py` has `register()`, `plugin.yaml` is shipped, `adapter.py` has
  `class VoipAdapter(BasePlatformAdapter)`, `media/vad.py` exists (the plan's ADR-0008 entry says "no
  `media/vad.py`"), and the STT/TTS/guard/media/manager/call modules are all built. Section 5 ("Smallest
  next shippable step") says to ship P0.1+P0.3 first, but those are already shipped. A future agent reading
  this plan will perform duplicate work or make wrong decisions. Update the plan to reflect current status or
  supersede it with a note pointing to the ADR index.

## docs/adr/

- [ ] **[low] docs** — ADR numbering has four unexplained gaps: 0039, 0040, 0041, 0051. The ADR sequence
  jumps from 0038 to 0042 (skipping 0039-0041) and from 0050 to 0052 (skipping 0051). `docs/adr/CLAUDE.md`
  states "Numbered NNNN-kebab-title.md" with no mention of intentional gaps or a reservation policy. The `adr`
  skill finds the next free slot by scanning the directory (producing numbers after 0073, not filling gaps),
  leaving the gaps permanently undocumented. Update `docs/adr/CLAUDE.md` to note whether gaps are intentional
  (reserved) or voided.
- [ ] **[low] docs** — Propose an ADR for a local-only structured-log metric protocol (no external sink).
  Runbook-0014 §Instrumentation roadmap says "wire metric emission (likely via StatsD, Prometheus, or
  structured logging)" but makes no decision. All current SLO signals are plain `%`-format log strings with no
  `extra=` fields. Without a decision on field names and format, the individual observability findings above
  will produce ad-hoc field names that diverge across lanes. An ADR specifying the local-only structured-log
  event schema (field names, log level, logger name) costs nothing to deploy (no infra, no paid service,
  satisfies rule 40) and gives lane authors a shared contract (`docs/adr/`, `docs/runbooks/0014-voip-slo-metrics.md`).

## src/hermes_voip/providers/ (Wave-1 additions)

- [ ] **[high] api** — `providers/__init__.py` re-exports only `Providers` and `build_providers`; the 12
  other public names forming the ADR-0004 provider seam — `StreamingASR`, `Transcript`, `StreamingTTS`,
  `TtsStream`, `InjectionGuard`, `GuardResult`, `GuardVerdict`, `MediaTransport`, `PcmFrame`, `AsrFactory`,
  `TtsFactory`, `GuardFactory` — all require deep submodule paths. There are ~178 such deep-path import sites
  across `src/` and `tests/` (confirmed: `rg -rn 'from hermes_voip.providers.(asr|tts|guard|audio|transport|policy)'`).
  The existing backlog entry (line 512) is now stale — `Providers`/`build_providers` ARE re-exported; the
  remaining gap is the Protocol+type surface. Fix: re-export all ADR-0004 public names from
  `providers/__init__.py` and add a package-boundary import test.
- [ ] **[medium] api** — `hermes_voip` top-level missing config and provider types; accidental submodule names
  leak into the namespace. `import hermes_voip; dir(hermes_voip)` shows sub-module objects (`caller_modes`,
  `config`, `digest`, `message`, `plugin`, `registration`, `sip`) leaking because those modules have no `__all__`
  and are imported transitively. Meanwhile `MediaConfig`, `GatewayConfig`, `ConfigError`, `Providers`,
  `build_providers`, `PcmFrame`, `StreamingASR`, `StreamingTTS`, `InjectionGuard` are absent and require deep
  paths. The existing backlog item (line 583) was written when only `sip_address_of_record` was in the
  top-level `__init__`; the `Registration` types have since been added (`hermes_voip/__init__.py:26-48`) but
  the config and provider surface remains absent. Fix: promote `MediaConfig`, `ConfigError`, and key provider
  Protocol types into `hermes_voip/__init__.__all__`; add `__all__` to transitively-imported modules
  (`src/hermes_voip/__init__.py`, `config.py`, `sip.py`, `digest.py`, `message.py`).
- [ ] **[low] api** — `stt` package promotes internal bridging utilities into its public `__all__`. `stt/__init__.py:16-29`
  re-exports `RECOGNISER_SAMPLE_RATE`, `FrameUpsampler`, `float32_to_pcm16`, `pcm16_to_float32` in `__all__`.
  These are intra-package implementation details: `RECOGNISER_SAMPLE_RATE` is only referenced inside
  `stt.resample` and `stt.sherpa_onnx`; the conversion functions are used by `stt.sherpa_onnx` and one test
  (`test_audio_content.py` imports `FrameUpsampler` directly from `hermes_voip.stt.resample`, not from
  `hermes_voip.stt`). Remove them from `stt/__init__.py/__all__` or prefix with `_`
  (`src/hermes_voip/stt/__init__.py`, `src/hermes_voip/stt/resample.py`).
- [ ] **[low] api** — `tts` package re-exports ElevenLabs-specific testability seams and ambiguously-named
  `Synthesizer` Protocol as package-level public API. `tts/__init__.py:13-18` re-exports `HttpByteStream`,
  `HttpCancellation`, and `ElevenLabsRequest` alongside proper provider implementations; no test imports them
  via `hermes_voip.tts` (all consumers import from `hermes_voip.tts.elevenlabs` directly). `Synthesizer` (the
  Kokoro backend injection Protocol) is exported with a generic name that does not signal "Kokoro-only seam".
  Fix: remove `HttpByteStream`/`HttpCancellation`/`ElevenLabsRequest` from `tts/__init__.__all__`; rename
  `Synthesizer` to `SherpaKokoroBackend` or keep it in `sherpa_kokoro.py` only
  (`src/hermes_voip/tts/__init__.py`, `tts/elevenlabs.py`, `tts/sherpa_kokoro.py`).
- [ ] **[medium] api** — `__all__` missing from modules added after the original backlog audit: `call_context.py`,
  `hermes_surface.py`, `notice_filter.py`, `provider_error.py`, `media/call_loop.py`, `media/dtls.py`,
  `media/srtp.py`, `media/srtcp.py`. All expose implementation-private symbols to star-imports and autodoc.
  The existing backlog item (line 566) enumerates the original modules; this covers the new ones.

## Packaging / release

- [ ] **[high] operability** (partial — manual release process documented in runbook 0019 / version single-sourced #187; automated CI-publish is PROPOSE-ONLY pending the operator's publish-target decision, rule 40/41) — No automated version bumping mechanism or release workflow. The version is
  hardcoded to `'0.0.0'` in three places (`pyproject.toml:3`, `src/hermes_voip/__init__.py:49`,
  `packaging/hermes-plugins/hermes-voip/plugin.yaml:27`) with no automation to keep them synchronized. A
  release requires manual synchronization of all three, inviting drift. The test at
  `tests/test_plugin_manifest.py:159-164` guards `pyproject.toml` ↔ `plugin.yaml` drift but there is no
  guard for `__init__.__version__`, and no documented release process.
- [x] **[high] test** (#181) — No test for `__version__` equality with `pyproject.toml`. `test_plugin_manifest.py`
  asserts `plugin.yaml` version matches `pyproject.toml` (line 159) but there is no corresponding test for
  `src/hermes_voip/__init__.py.__version__` (line 49) — a release bump could easily leave it out of sync
  (`src/hermes_voip/__init__.py:49`, `tests/test_plugin_manifest.py:159-164`).
- [x] **[medium] operability** (done — Wave 3 audit) — Wheel artifacts remain in the git-tracked `dist/` directory.
  `dist/hermes_voip-0.0.0-py3-none-any.whl` exists and is not in `.gitignore`. Build artifacts in the repo
  create a stale-artifact risk and violate deterministic-builds rule 33. Add `dist/` to `.gitignore` and
  remove the committed artifact.
- [x] **[medium] test** (#191) — No wheel build/test in CI or local gate. `.github/workflows/gate.yml` runs
  format/lint/mypy/pytest but never builds the wheel or tests its contents. `test_manifest_is_importable_package_data`
  (`test_plugin_manifest.py:405`) checks *source* package data, but there is no CI job verifying a built
  wheel can be installed and that `importlib.resources` works inside it. A broken wheel (missing `plugin.yaml`,
  wrong entry-point) would pass the current gate.
- [ ] **[low] docs** — No documentation on how to distribute / publish the plugin. README covers installation
  but contains no section on distributing hermes-voip to end users: no mention of PyPI, GitHub Releases, wheel
  publishing, or the two install models (pip entry-point vs directory plugin) from an operator's perspective
  (`README.md`, `docs/adr/0037-hermes-plugin-manifest-and-install-models.md`).

## src/hermes_voip/media/engine.py

- [ ] **[low] observability** — Add RTCP periodic quality poll (mid-call snapshot) at configurable interval
  for long calls. `engine.py` reads `engine.call_quality` only once at teardown via the adapter. For a 30-minute
  call a single end-of-call snapshot provides no insight into degradation that occurred and recovered mid-call.
  `rtcp.py:922-936` `ReceptionStats.snapshot()` is designed for non-disturbing quality polls. Add a periodic
  call (e.g. every 60 s) emitting a structured log event; `snapshot()` explicitly does not roll the loss-interval
  baseline (`media/engine.py`, `adapter.py`, `rtcp.py`).

## src/hermes_voip/keepalive.py

- [ ] **[low] feature** — Surface inbound MWI (message-summary NOTIFY, RFC 3842) to the agent instead of
  dropping the body. The gateway sends unsolicited `Event: message-summary` NOTIFY to report waiting voicemail;
  the plugin acknowledges with a bare 200 OK and parses nothing (`keepalive.py:21,92`;
  `transport/connection.py:752`; `ws_connection.py:433`). The MWI body (`Messages-Waiting: yes` + voicemail
  counts) is a real, observed-live telephony signal. Shippable, fully local, sans-IO: add a typed
  `MwiState` parser (RFC 3842 simple-message-summary grammar), have the transport's unsolicited-NOTIFY path
  hand it to an adapter hook that injects an `internal=True` notification turn. Needs a short ADR for scope
  before build (RFC 3842 grammar + existing not-processed comment in `keepalive.py:21`).

## Gap-review provenance

- **2026-06-23**: 44 new items above were discovered by the Wave-1 `/orchestrate` autonomous gap-review
  (ADR-0072). The review fanned out across 11 of 12 dimensions; the `performance` dimension's agent failed
  with an API error (HTTP 200, empty/malformed response) and its dimension still needs a re-run. Items already
  tracked in this backlog before this date (12 items, primarily DTMF encode/boundary vectors, jitter-buffer
  payload fidelity, resampler 24 kHz paths, and digest quoting assertions) were deduplicated and not
  re-appended. The `backlog.md preamble is stale` docs-drift finding was resolved in-place (preamble rewrite
  above) rather than tracked as a separate item.

## Future / deferred (nice-to-have, ADR-gated)

- [ ] **[low] feature/observability** — Durable call-events + recordings store via AWS S3 Tables
  (Iceberg). Researched + designed in ADR-0060 (Proposed/Deferred). Optional, dependency-free
  call-event sink in core; S3-Tables writer as an optional extra (default off); audio blobs in plain
  S3, queryable metadata in Iceberg. Requires explicit operator cost approval, an ADR flip to Accepted,
  and a runbook before any implementation (rule 40).
- [ ] **[medium] feature** — Agent-screened inbound answering (opt-in "ring the agent, do not
  auto-pick-up"). Today an inbound `INVITE` is auto-answered `200 OK` immediately (the
  `_send_answer_200` path in `adapter.py`), so the agent only learns of the call once media is already
  live and no `100 Trying` / `180 Ringing` provisional is sent. Proposal: add an opt-in mode via a new
  env flag `HERMES_VOIP_AUTO_ANSWER` (default `true` = today's behaviour; `false` = screen). When
  screening, *after* the automatic policy gates pass (drain `503` / capacity `486` / declined-caller
  `603` / secure-media `488` / admission reserve), the adapter sends `180 Ringing` (keeping the `INVITE`
  transaction alive and suppressing retransmits) and wakes the agent with the call metadata as an
  `internal=True` "incoming call" turn — caller identity (`From` / display-name / `P-Asserted-Identity`),
  called number (`To` / Request-URI), allow-listed custom headers, offered media (codecs,
  secured-vs-cleartext, video?), and arrival time. The agent then decides via new call-control tools
  `accept_call` / `decline_call(reason)` that mirror the existing `hang_up_call` / `transfer_*` tools in
  `voip_tools.py`: accept builds the SDP answer + `200 OK` and starts the conversational loop exactly as
  today; decline sends `603 Decline` (or `486` / `480` per the tool argument). A bounded screening
  timeout (new env `HERMES_VOIP_SCREEN_TIMEOUT`, e.g. `20s`, below the gateway `INVITE` timeout) with a
  configurable default-on-timeout action (`accept` | `decline`) guarantees the dialog never wedges.
  * Composes *after* the automatic gates — never ring the agent for a call we would reject anyway
    (capacity / declined caller / cleartext-under-mandate). Reserve the admission slot at ring time and
    release it on decline/timeout (reuse `_admit_inbound` / `_teardown_call`).
  * SIP mechanics: `180 Ringing` (optionally a leading `100 Trying`) stops `INVITE` retransmission
    during deliberation; long deliberation may need a periodic provisional. MVP carries no early media;
    the `200 OK` carries the secured SDP answer (ADR-0070); a decline sends a non-2xx final (`ACK`
    handled as today).
  * Agent wake-up reuses the half-duplex text surface: inject the incoming-call context as a system
    turn. The `From` display-name is attacker-controlled and MUST be defanged like other caller-sourced
    strings (injection guard). Document the decision contract so a non-responding agent hits the timeout
    fallback, not a wedged dialog.
  * Interactions: caller `CANCEL` during ring → `487`, abandon the screen; RFC 4028 session timers
    arm only after `200`; call-progress/AMD runs only post-answer; graceful shutdown declines in-flight
    screens.
  * Advanced / later (its own ADR): `183 Session Progress` + early-media SRTP so the agent can *hear*
    the caller and speak a screening prompt ("who is calling?") *before* formally answering — a
    two-phase-keying problem, out of MVP scope.
  * Needs a design ADR (the WHY + the ring/wake/decide SIP state-machine + the decision contract) before
    build, TDD across ring → wake → accept/decline/timeout, and a runbook note for the new env flags.

## Review follow-ups (Wave 4 release-blocker reviews, 2026-06-23)

- [ ] **[high] security** — `transfer_blind(target, ...)` passes an agent-supplied `target` (extension OR
  SIP URI) straight into a `Refer-To: <target>` REFER header via `build_blind_refer` (`refer.py`) with
  `urllib.quote/unquote` handling and NO dialable-grammar gate. A `target` like `1001@evil.com` or one
  bearing `?Replaces=` / `;`-params could redirect/smuggle into the REFER-To URI — the SAME injection class
  the outbound-INVITE guard (`_validate_dialable_target`, place_call) closes. The INVITE hole is fixed but
  the REFER hole is open; close it (validate/allow-list the transfer target, or strictly escape it for the
  Refer-To URI) so the injection class is fully mitigated. Surfaced by the outbound-URI security review.
- [ ] **[low] test** — `test_no_input_defaults_match_call_loop_constants` (#188) asserts HARDCODED literals
  rather than importing `media/call_loop.py`'s `_DEFAULT_*` constants. It catches a `config.py`-side drift
  but NOT a `call_loop.py`-side change to the same defaults. Strengthen it to assert
  `cfg.goodbye_phrase == call_loop._DEFAULT_GOODBYE_PHRASE` (etc.) so the must-match invariant is bulletproof.
