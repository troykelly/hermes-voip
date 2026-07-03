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
- [x] (done #214) **[medium] robustness** — `nc` accepts negative and `>0xffffffff` values, producing malformed
  nonce-count. `f"{nc:08x}"` renders `nc=-1` as `-0000001`, `nc=2**40` as 9+ hex digits (both verified).
  RFC 2617 nc is exactly 8 hex digits. Validate `0 < nc <= 0xFFFFFFFF`; tests for nc=0/-1/2**32.
- [x] (done #214) **[medium] robustness** — No control-char-rejection tests for each caller/challenge-sourced
  quoted param. `_quoted`'s guard is shared, but only `nonce` is covered (`test_rejects_crlf_in_auth_param_value`).
  `uri` is both hashed and rendered; `username`, `opaque`, `realm`, `cnonce` are rendered too. Add
  per-field CRLF-rejection tests — the per-field coverage is what mutation testing rewards.
- [x] **[low] robustness** — Empty `cnonce` (`cnonce=""`) is accepted and emitted (`is not None` check,
  line 167). Defeats the client-nonce purpose; some registrars reject it. Reject empty (or treat falsy
  as "generate") and document; add a test. — shipped #287
- [x] **[low] correctness** (partial — residual: no test asserting unknown/hyphenated params are silently ignored (no test with an unknown x-custom-param= in tests/test_digest.py).) — `_PARAM` key pattern `\w+` (line 24) cannot match hyphenated extension
  param names. No test asserts unknown/hyphenated params are ignored gracefully. Broaden to `[\w-]+`
  for forward-compat or pin the silent-skip behaviour with a test. — shipped #293
- [x] **[low] robustness** — `algorithm` token rendered unquoted (line 163) after only a lowercase
  membership check (line 147). Safe today (only `md5` passes) but the render path trusts the gate.
  Route through the control-char guard or assert a token grammar; note the coupling in a comment.
  — done: a comment at the `build_authorization` params list documents the coupling — the unquoted
  algorithm/qop/nc are safe because `algorithm` is validated to `_SUPPORTED_ALGORITHMS` (a fixed
  `[a-z0-9-]` set) before the render, `qop` is the literal `"auth"`, and `nc` is 8 hex digits — with a
  pointer to route through `_quoted` if that gate is ever loosened.
- [x] **[low] robustness** — `realm` defaults to empty string silently when the challenge omits it
  (line 99), hashed into HA1 (line 154). Asymmetric with the missing-`nonce` case which *raises*
  (lines 93-95). Either raise on missing/empty realm, or document why empty realm is tolerated; add a test. — shipped #293
- [ ] **[low] api** — Caller cannot supply a precomputed HA1; `DigestCredentials` holds plaintext
  password (line 112) and recomputes HA1 each call. RFC 2617 allows storing A1. Offer an HA1-based
  credential variant so security-conscious callers need not keep plaintext resident; record in the ADR.
- [x] **[low] api** — `qop` selection silently drops `auth-int`; auth-int unimplemented. Correct/in-scope
  for SIP REGISTER (empty body) but the docstring doesn't call out that auth-int-only is the rejection
  path. State the scope in the docstring/ADR; keep the auth-int-only rejection test. — done: the
  `build_authorization` docstring now states `qop=auth-int` is unimplemented (unnecessary for SIP
  REGISTER's empty body) and that a challenge offering only `auth-int` is the rejection path;
  `test_rejects_qop_present_without_auth` pins it.
- [ ] **[low] api** — `build_authorization` returns a bare value string; `registration.py` (lines 161-164)
  separately maps 401→Authorization / 407→Proxy-Authorization, duplicating knowledge the challenge origin
  already has. Carry `proxy: bool` on `DigestChallenge` (set by parse) or return a `(name, value)` pair;
  record in the digest/registration ADR.
- [x] **[low] test** — No assertion that `algorithm`/`qop`/`nc` render UNQUOTED (`algorithm=MD5`, not
  `="MD5"`). `_param` helper strips quotes, so a quote-everything mutation survives. Assert raw header
  substrings (`'algorithm=MD5,' in header`, `'qop=auth,' in header`). — shipped #287
- [x] **[low] test** — No test that `opaque` is ABSENT when the challenge omits it (negative case,
  line 181). A mutation always emitting `opaque=""` survives. Assert `_param(header,"opaque") is None`. — shipped #287
- [x] **[low] test** — No test pinning the quoting table (username/realm/nonce/uri/cnonce/response/opaque
  quoted vs algorithm/qop/nc unquoted, lines 158-182). Add raw-substring assertions for at least one
  quoted and one unquoted param to lock the boolean-flip mutants. — shipped #287
- [x] **[low] docs** — `DigestCredentials.password` is repr-suppressed (`field(repr=False)`, line 112)
  but the docstring doesn't say it is still plaintext in memory and must never be logged/interpolated.
  Add the note (public-repo / AGENTS 34); optionally a test that the secret isn't in `repr(creds)`.
  — done: the `DigestCredentials` docstring warns the password is still the plaintext secret in memory
  and must never be logged/interpolated (rule 34); `test_credentials_repr_does_not_leak_password` proves
  the secret (and the field name) are absent from `repr(creds)`.
- [x] **[low] docs** — `_md5_hex` always encodes UTF-8 (line 37); a `charset` param (RFC 7616) is
  ignored. Latin-1 gateways with accented credentials would mismatch HA1. Document the UTF-8 assumption;
  add a non-ASCII-realm test pinning the behaviour. — done: the module docstring documents the UTF-8
  encoding assumption (the RFC 7616 §4 `charset` param is not consulted);
  `test_non_ascii_realm_is_utf8_encoded_in_ha1` is a KAT proving HA1 uses UTF-8 (and differs from
  ISO-8859-1) for a realm containing U+00E9.
- [x] **[low] docs** — Algorithm round-trip subtlety: case-insensitive gate (line 147), case-preserving
  echo (line 163). Correct and intended but unexplained. Add one docstring sentence; add an uppercase
  `algorithm=MD5` accepted+echoed test to complete coverage. — done: the `DigestChallenge.algorithm`
  attribute doc explains the case-insensitive match + verbatim case-preserving echo;
  `test_uppercase_algorithm_accepted_and_echoed_verbatim` covers an uppercase `algorithm=MD5` accepted
  and echoed unquoted.
- [x] **[low] efficiency / docs** — `parse` materialises a full param dict even though only ~5 keys are
  read. Cold path (per-registration, not per-RTP-packet) — **no action on perf grounds**; note in review
  that digest parsing is cold-path so a future agent doesn't "optimise" clarity away. — done:
  `DigestChallenge.parse` carries a cold-path comment stating the full-dict materialisation is
  intentional (per-registration, never per-RTP-packet) and must not be micro-optimised into a
  single-pass early-exit scan.
- [ ] **[low] correctness / security** — `DigestChallenge.parse` / `_strongest_challenge` mis-parse
  two comma-separated `Digest …` challenges packed into a SINGLE `WWW-Authenticate` /
  `Proxy-Authenticate` line (RFC 7616 §3.7 permits the `1#challenge` comma list; real SIP registrars
  use separate header lines). `_strongest_challenge` (registration.py:603) separates challenges only
  by `headers_all` — i.e. per header LINE — then parses each with `DigestChallenge.parse`, whose
  `_PARAM.finditer` (digest.py:47/193) folds every param on the line into ONE dict with duplicate keys
  OVERWRITTEN last-wins (the bare `Digest` scheme token is not a `name=value` pair, so it is skipped,
  not treated as a delimiter). So a single comma-joined line collapses into ONE challenge built from
  whichever `realm`/`nonce`/`algorithm` appear LAST. No crash-escape — the result is still a valid
  `DigestChallenge`, so this is a wrong/mixed auth, never a bare exception on the shared reader loop.
  Two outcome sub-cases: (a) when the last-appearing params come from different challenges, the mixed
  realm/nonce yields a `response` the registrar rejects → second challenge → fail CLOSED to `Failed`
  (registration.py:526-528), or an unsupported mixed algorithm raises `ValueError` caught in
  `_handle_challenge` (registration.py:531) → `Failed`; BUT (b) when a stronger challenge is followed
  by a COMPLETE MD5 `Digest` challenge with the SAME realm, last-wins folds to a COHERENT MD5
  challenge (realm+nonce+algorithm all from the MD5 half), `pick_best_challenge` returns it, and the
  client SILENTLY authenticates with MD5 — a downgrade that hides the stronger challenge. Sub-case (b)
  is why this is not purely cosmetic; it stays LOW because it affects ONLY the non-standard
  comma-joined encoding an on-path attacker would coalesce (two separate lines merged with MD5 ordered
  last) or an unusual gateway emits — separate-line challenges (SIP practice, and the header-reorder
  threat model in `_strongest_challenge`'s docstring) stay fully downgrade-protected. Fix (if ever):
  split a comma-joined challenge value into its constituent `Digest` challenges (respecting quoted
  commas) before `DigestChallenge.parse`, so `pick_best_challenge` sees each challenge whole.

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
- [x] **[medium] polish** — Control-character rejection logic is duplicated verbatim between — shipped #277 (extracted to _chars.contains_control)
  `message.py` (lines 27-36) and `digest.py` (lines 30-32, 50). Two copies of an injection-guard
  invariant will drift. Hoist a shared `contains_control`/`reject_controls` into an internal
  `_text.py`/`_chars.py`; keep per-call-site wording; add a focused test (NUL/CR/LF/HTAB/DEL + a high
  code point that must be allowed).
- [x] (done #208) **[medium] robustness** — Header-block lines without a colon are silently dropped (`if sep:`,
  lines 156-159). Verified: a `garbageline` between headers vanishes with no error — inconsistent with
  the module's otherwise-strict stance. Decide and document: prefer raising `ValueError('malformed header
  line: …')`; if leniency is wanted, comment it and pin with a test.
- [x] **[medium] robustness** (#184) — `build_request` does not validate request-line method/URI shape. Only
  `_reject_controls` runs (lines 78-79); an empty method, embedded-space method, or empty/space URI
  passes. Verified: `build_request("", "sip:x", [])` → start-line `' sip:x SIP/2.0'`. Validate method
  against the token rule + reject empty; reject empty/space URI. Red tests for each.
- [x] **[medium] test** (partial — residual: the multibyte-UTF-8 body test pinning byte-length Content-Length is still absent (test at line 150 uses ASCII-only body).) — Duplicate Content-Length and caller-owned-header collisions are untested. — verified shipped on main 2026-06-27 (tests/test_message.py byte-length Content-Length tests)
  Add a test after deciding the policy; also a multibyte-UTF-8 body test pinning the byte-length
  Content-Length (a `len(body)` mutant survives today).
- [x] **[medium] test** — Token-generator tests under-constrain format/length (mutation-weak). — verified shipped on main 2026-06-27 (tests/test_message.py rfc3261 hex-length regex tests)
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
- [x] **[low] test** — Continuation-line edge cases partially covered. The `'\t'` fold arm (line 148), — shipped #283
  the orphan-continuation `ValueError` path (lines 149-151), and the exact single-space join (line 152)
  are untested. Add tab-fold, first-line-starts-with-space (`ValueError(match='continuation')`), and
  exact-unfolded-string tests.
- [x] **[low] docs** — `parse()` splits the body on the FIRST `CRLFCRLF` via `partition` (line 139), — shipped #283 (HTAB-fold + embedded-blank-line body tests)
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
- [x] (#234) **[medium] correctness** — Opus / preference-ordered offer is unreachable through the builder.
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
- [x] (done #209) **[low] robustness** — Direction attribute matching is exact-case only; `a=SENDONLY` is silently
  ignored (defaults to `sendrecv`, lines 146-147) → wrong media direction (e.g. a hold treated as
  sendrecv). Lower-case before the membership test; add a mixed-case test.
- [x] (#320) **[low] robustness** — Duplicate rtpmap (last-wins) and duplicate payload-type in `m=` (dup
  `Codec`) resolved silently. Decide a policy (dedupe PTs preserving first; document last-wins for rtpmap)
  and pin with a test.
- [x] (#311) **[low] correctness** — telephone-event clock-rate vs voice clock-rate consistency not validated
  (RFC 4733). `telephone-event/16000` alongside `PCMU/8000` is accepted and would mis-time DTMF
  (ADR-0010). After negotiation, validate the TE clock_rate equals the selected voice rate; test the
  mismatch.
- [ ] **[low] efficiency** — `SessionDescription.parse` does `text.replace(_CRLF,'\n').split('\n')`
  (line 189), allocating two transient buffers. SDP is NOT the 50pkt/s path. Use `splitlines()` (drops
  the copy, handles bare CR) — micro-optimisation; correctness of bare-CR handling is the better reason.
- [x] **[low] polish** — `o=` line collapses three distinct RFC 4566 fields. `o=- {session_id}
  {session_id} IN IP4 {local_address}` (line 285) forces sess-id == sess-version (a re-INVITE must keep
  sess-id and increment only sess-version — the docstring's re-INVITE claim at 260-261 is aspirational,
  rule 27), hard-codes username `-`, and hard-codes `IP4` so an IPv6 local_address emits a wrong addrtype.
  Split `session_id`/`session_version`; derive addrtype from the address family. — shipped #291
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
- [x] **[low] docs** — No module-level statement of scope/leniency (video ignored is stated; 2nd+ audio
  sections ignored, unknown `a=` dropped, dup rtpmap last-wins, no DTLS fingerprint, port 0 not special
  are NOT). Add a "Scope and leniency" paragraph; consider modelling `a=fingerprint`/`a=setup` so the
  ADR-0005 DTLS-SRTP profile is representable. — done: a "Scope and leniency" paragraph is added to the
  `sdp.py` module docstring (tolerated cosmetic quirks vs fail-closed semantic errors, and the CRLF/LF
  line-splitting boundary); `a=fingerprint`/`a=setup` are already modelled (`Fingerprint` dataclass + the
  DTLS-SRTP build path).
- [x] **[low] test** — Build-side under-asserted (mutation-weak round-trip): no literal-line assertions
  (`v=0` first, single `m=audio … RTP/AVP`, `a=ptime:20`, `a=sendrecv`, trailing CRLF). A dropped `v=0`
  round-trips through our own lenient parser. Assert key literal lines. — done: the build round-trip test
  asserts `lines[0] == "v=0"`, a single `m=audio …` line, `a=ptime:20`, `a=sendrecv`, and the trailing
  CRLF (`text.endswith("\r\n")` and `lines[-1] == ""`).
- [x] **[low] test** — Direction round-trip untested for sendonly/recvonly/inactive (every assertion is
  `sendrecv`); build-side direction validation (lines 270-272) and `session_id`-into-`o=` are untested.
  Parameterise over all four directions for parse+build; add `test_build_rejects_bad_direction` and a
  session-id-in-o= test. — done: `test_direction_round_trips_for_valid_build_values` parametrises all four
  directions (parse + build + RFC 3264 answer-mirror); `test_build_rejects_bad_direction` rejects bad
  directions on both offer and answer; `test_build_audio_offer_distinct_session_id_and_version` covers
  `session_id`→`o=`.
- [x] **[low] test** — Parser robustness paths untested (negative/zero port, negative ptime, blank lines,
  lone CR, trailing whitespace, duplicate rtpmap). Add once the parser is hardened (assert `SdpError`).
  — done: semantic errors are rejected (`test_parse_rejects_negative_port` / `_port_above_65535` /
  `_accepts_port_zero` / `_rejects_negative_ptime` / `_rejects_ptime_zero` /
  `_rejects_duplicate_rtpmap_payload_type`); the cosmetic-leniency contract and the bare-CR
  line-splitting security boundary are pinned this PR (`test_parse_tolerates_trailing_whitespace_without_phantom_payload`
  / `_lf_only_line_endings` / `_embedded_blank_line` / `test_parse_lone_cr_cannot_inject_attribute` /
  `test_parse_rejects_lone_cr_splicing_the_m_line`).
- [ ] **[low] robustness/security** (surfaced by codex on the leniency PR) — A media-direction
  attribute corrupted by an embedded bare CR (`a=recvonly\ra=inactive`) is currently dropped as
  unrecognised, so the direction silently falls back to the default `sendrecv` — broadening media flow
  versus the `recvonly`/`inactive` the gateway intended (a hold/one-way case becomes two-way). This is
  NOT a line-injection hole (the forged value never wins; pinned by `test_parse_lone_cr_cannot_inject_attribute`),
  and transport TLS integrity is the primary control against byte tampering, so it is defence-in-depth,
  not a live vulnerability. Decide the policy: keep lenient-default, or fail closed (reject a known
  direction attribute whose value is present-but-corrupt, distinct from a genuinely unknown `a=`). Needs
  a small design note before changing parser behaviour (the current "unknown `a=` ignored" leniency must
  not regress).

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
- [x] **[medium] correctness** (done — ADR-0080) — AOR scheme not constrained to `sips`, contradicting ADR-0005's
  SIP-over-TLS mandate. `_registrar_uri` echoes whatever scheme the AOR carries; a `sip:` AOR over a TLS
  transport is internally inconsistent with no signal. ENFORCED: `RegistrationConfig.__post_init__` rejects
  a `sip:` AOR on a TLS/WSS transport (`_require_secure_scheme`, transport-gated, case-insensitive); UDP/TCP
  leave the scheme to the deployer; `sips:` is accepted on any transport.
- [x] **[medium] robustness** (attempted Wave 4 — BLOCKED: a `transport` Literal on RegistrationConfig conflicts with `GatewayConfig.via_transport` dict typing at config.py:684; reconcile both files in one lane) — `RegistrationConfig.transport` and `expires` are unvalidated. `transport` — shipped #282
  is a free str injected into the Via line (line 201); `expires` accepts negatives (`Expires: -1`). Make
  `transport` a `Literal['TLS','WSS','UDP','TCP']`, reject negative `expires` in `__post_init__`
  (AGENTS 17 prefer types). Test an unknown-transport rejection.
- [x] **[medium] correctness** (done — ADR-0080: PINNED, fresh-nonce assumption documented; no nc counter added — rule 6) — Auth re-send does not increment `nc` on a refresh reusing the same
  nonce. `_reauthenticate` always uses `nc=1` with a fresh cnonce. This is CORRECT (RFC 7616 §3.4) for a
  purely-reactive flow: each REGISTER is a fresh transaction answering a freshly-received nonce, so there is
  no reused nonce to count against; a persistent monotonic nc would be state with no reachable consumer.
  Pinned with a test asserting the authed REGISTER carries `nc=00000001`.
- [x] **[medium] test** (done — ADR-0080) — No test that the Via branch changes between the initial and authed REGISTER
  (RFC 3261 §8.1.1.7 requires a new branch). `_build` does call `new_branch()` each time, but nothing
  pins it; a hoist-to-`__init__` refactor would silently break transaction matching. PINNED: a test asserts
  the branches differ (both `z9hG4bK…`) while Call-ID/From-tag stay equal.
- [x] **[medium] test** (done — ADR-0080) — No registration-level test for the qop-less (RFC 2069) digest path or the
  opaque echo. Every challenge fixture offers `qop="auth"` and no opaque. ADDED: a qop-less challenge test
  (no nc/cnonce, 32-hex response, independently recomputed) and an opaque-echo test — guards the registration↔digest seam.
- [x] **[medium] test** (done — ADR-0080: PINNED the "fail on second 401 even if stale" limitation) — No test that a fresh nonce in a second 401 (`stale=true`) is used. The flow
  treats any second 401 in a transaction as `Failed`, so it cannot honour in-transaction stale-nonce
  rotation; there is no `DigestChallenge.stale` field. PINNED as an intentional, recorded limitation:
  recovery is the RegistrationManager's next refresh (a brand-new transaction with the fresh nonce). A test
  asserts a second 401 with `stale=true` is still `Failed` and that the next refresh re-authenticates.
- [ ] **[low] robustness** (follow-up from ADR-0080 / bk250) — Optional in-transaction `stale=true` retry.
  Today recovery from a stale-nonce 401 is the next refresh (a new transaction). If a gateway is ever
  observed to rely on in-transaction recovery, add a `DigestChallenge.stale` field + a single re-answer
  within the same transaction when the second 401 carries `stale=true` (bounded, reversible). Deferred now
  (rule 6 — no scaffolding for an unproven need).
- [x] **[low] correctness** — A 2xx other than 200 (e.g. 202) is mishandled — `status == _OK` exact
  compare (line 141) falls through to `Failed`; 1xx provisionals also treated as `Failed`. Treat
  `200 ≤ status < 300` as success (or document 200-only) and ignore 1xx; tests for 1xx-then-200 and
  non-200 2xx. — shipped #288
- [x] **[low] efficiency** — `_check_cseq` calls `cseq.split()` twice (line 183) and never validates the
  CSeq *method*; a response whose number coincides but whose method is `INVITE` is accepted. Split once,
  validate `parts[1].upper() == 'REGISTER'`; test a method mismatch. — shipped #288
- [x] **[low] robustness** — Missing/garbled CSeq is silently accepted (lines 181-182), bypassing the
  only correlation check. Reconsider raising; if leniency is deliberate, pin it with a test. — shipped #288
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
- [x] (#314) **[low] test** — No test that `deregister()` resets registered state / cannot be called twice. The
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
- [x] (done #215; partial: total-occupancy semantics documented + pinned; contiguous-backlog correctness fix deferred to the JitterBuffer __len__/flush/reset/SSRC API redesign) **[medium] correctness** — `target_depth` counts ALL buffered packets, so a far-future cluster
  triggers premature loss for the immediate gap. `pop()` declares `Lost` when `len(_packets) >= _depth`
  (line 200) measuring total occupancy, not contiguous backlog behind the gap (verified with
  depth=3, push 10/100/101/102). Gate loss on backlog ahead of the next contiguous run; at minimum
  document total-occupancy semantics and pin with a test.
- [x] **[medium] efficiency** — A single far-ahead packet emits a long run of one-`Lost`-per-`pop()` — verified shipped on main 2026-06-27 (rtp.py Lost.count run-length coalescing)
  (verified: depth=1, push 10 then 50 → 39 separate `Lost`), each allocating a frozen dataclass, spinning
  the media loop. Coalesce into `Lost(sequence, count)` / a run-length scanned in one step; even if the
  per-packet API is kept, add a fast-path and document the per-pop cost.
- [x] (done #215) **[medium] robustness** — Malformed RTP padding silently accepted. When the pad byte exceeds the
  payload, the guard (lines 114-117) fails and the raw payload (incl. the bogus pad byte) is returned with
  no error — inconsistent with the other parse paths that raise. Verified: P=1, payload `b'\x05'` →
  `b'\x05'`. Raise `ValueError` when `pad > len(payload)`; decide/document `pad==0`-with-P. Tests both.
- [x] (#221) **[medium] api** (partial — residual: `__len__`, `flush()`, `reset()` are absent; no SSRC-awareness or auto-reset on SSRC change.) — `JitterBuffer` exposes no `__len__`/peek/flush/reset. The media loop needs depth
  (metrics/adaptive), drain-on-BYE (trailing audio), and reset-on-SSRC-change (re-INVITE/source resync) —
  none exist, and an SSRC change mis-classifies the new stream against the stale anchor. Add
  `__len__`/peek/`flush()`/`reset()`; have the buffer know the SSRC and auto-reset on change.
- [ ] **[low] api** — `pack()` cannot re-emit CSRC/extension/padding it can parse (lines 67-79 vs
  100-117), so `RtpPacket` is lossy. Documented as intentional but not a named/tested invariant. State the
  skip-only contract in the docstring; test that parse-of-extension then pack yields a bare header.
- [ ] **[low] robustness** — No length cap on payload/packet size; `pack()` builds multi-kB datagrams and
  the buffer stores them; per-buffered-packet byte size is unbounded. Consider an optional generous
  telephony ceiling, or document the deliberate absence (transport's responsibility).
- [x] **[low] test** — `max_ahead` window boundary is inclusive but untested at the exact edge (line 185;
  verified next=10/max_ahead=256 keeps 266, drops 267). A `>`→`>=` mutant survives. Document inclusive;
  add `next+max_ahead` (kept) / `+1` (dropped) tests. — shipped #290
- [ ] **[low] docs** — The `_seq_before` (32768) vs `max_ahead` (256) interaction creates an undocumented
  effective window `[next .. next+256]` and a wraparound ambiguity if `max_ahead` nears 32768. Add a
  comment; assert `max_ahead < _SEQ_HALF` in `__init__`.
- [x] **[low] test** — No test for a duplicate of a packet still buffered (only an already-popped
  duplicate is tested). A `setdefault`→assignment (last-wins) mutant survives. Push two copies of the same
  buffered seq with distinct payloads; assert first-arrival payload wins. — shipped #290
- [x] **[low] test** — Jitter-buffer tests assert only `.sequence_number`, never payload/timestamp/ssrc
  fidelity through the buffer (the property that matters for STT). Give packets unique payloads/timestamps
  and assert correct payload+timestamp per sequence. — shipped #290
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
- [ ] **[low] robustness** — `RtpPacket.__post_init__` (line 71) range-checks
  `payload_type`/`sequence_number`/`timestamp`/`ssrc` with `0 <= x <= MAX` but never
  `isinstance(x, int)` — a `float` (e.g. `timestamp=1000.5`) satisfies every comparison and
  constructs cleanly, then fails later and lower in the stack: `pack()`'s
  `struct.pack('!...I', 1000.5, ...)` raises `struct.error`, not the module's advertised
  `ValueError`. Add an `isinstance(x, int)` guard (or accept `SupportsInt` and coerce) so the
  failure is immediate, typed, and at construction.
- [ ] **[low] correctness** — `peek()` (line 524) returns `Lost(expected)` with the default
  `count=1`, never the coalesced run-length `pop()` computes (lines 501-521) for the same gap
  — verified: `pop()` at the same anchor can return `Lost(expected, count=N>1)` while a
  `peek()` just before it reports count=1. Latent (no caller currently branches on `peek()`'s
  count), but a future caller trusting `peek()` to preview `pop()` would under-report loss.
  Share the run-length scan (or document `peek()`'s count as advisory-only); test the
  mismatch.

## src/hermes_voip/rtcp.py

- [ ] **[low] robustness** — `SourceDescription._from_body` (line 586) and `Bye._from_body`
  (line 708) both stop once the declared `count` chunks/SSRCs (+ BYE's optional reason) are
  consumed, but neither checks that parsing fully consumed the declared body — verified:
  neither has an `offset == len(body)` / `end == len(body)` guard, so any trailing bytes left
  over after the declared content (there is no legitimate padding-after-content case per RFC
  3550 §6.5/§6.6 — each SDES chunk already self-pads to its own 4-byte boundary) parse
  silently instead of raising. Add the same full-consumption check to both; test a trailing
  extra byte on each.
- [ ] **[low] robustness** — `rtt_from_report_block` (line 1038) clamps the *lower* bound
  (`rtt_units < 0 → 0.0`) but not the upper: `delay = (now - lsr) & _U32` wraps to up to
  2^32-1 compact-NTP units (~18.2 h) when `lsr` is stale/replayed or the clock jumps, and that
  whole span can surface as a bogus multi-hour "RTT" with no ceiling. Clamp to a documented
  sane maximum (or reject values above it) so a stale/replayed report block can't poison
  RTT-based SLO metrics; test the wraparound-to-huge-RTT case.

## src/hermes_voip/dtmf.py

- [x] **[high] robustness** (#180) — `DtmfReceiver` does not validate `history >= 1`. `history=0` leaks `_seen`
  unbounded (eviction guard at line 175 short-circuits; `_order` is a no-op `maxlen=0` deque) — verified
  growing `_seen`, an unbounded leak on a long call, silently breaking the bounded-window contract.
  `history<0` raises a raw `ValueError` from deque. Validate `if history < 1: raise ValueError(...)`
  mirroring `JitterBuffer`; document the minimum.
- [x] (#232) **[medium] polish** — `_order`/`_seen` are two structures kept in sync by hand (lines 175-178) — the
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
- [x] (#232) **[medium] api** — `feed()` conflates "still pressing" / "duplicate end" / "non-digit event" into a
  single `None` (lines 170-172). A controller may need to distinguish a completed flash/non-digit from
  "nothing happened". Return a small result type (enum-tagged / `DtmfPress | None`) or expose whether a new
  end-timestamp was recorded.
- [x] (#223) **[medium] test** — `encode()`'s end-bit/volume byte construction is mutation-weak — one pinned
  vector (`end=True, volume=10` → `0x8A`); no `end=False` (`0x0A`), no `volume=0`/`0x3F`. A `|`→`&` or
  dropped-`_END_BIT` mutant partly survives. Add end=False, volume=0 (`0x80`), volume=0x3F assertions.
- [x] (#223) **[medium] test** — Bounded-window eviction (an evicted timestamp re-emits) is a deliberate tradeoff
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
- [x] **[low] test** — `DtmfEvent.__post_init__` branches untested for the flash (16) accepted path and — shipped #280
  the bounds (255 ok / 256 reject; volume 63 ok / 64 reject; duration 65535 ok / 65536 reject). Add them.
- [ ] **[low] test** — `feed()` non-digit (flash=16) path (lines 179-181) untested. Add: `feed(event=16,
  end=True)` returns `None`, and a same-timestamp re-feed also returns `None` (recorded but not surfaced).
- [ ] **[low] test** — Lost-end / start-only-press surfacing (docstring, lines 156-157) untested. Add:
  only non-end packets never emit (end bit is the sole trigger); a lone end packet emits once then dedups.
- [x] **[low] robustness** — `decode()` ignores the reserved R bit (0x40) without a note (liberal accept; — shipped #280 (boundary tests + R-bit comment)
  not preserved on re-encode). Add a one-line comment; optionally a test feeding R=1.
- [ ] **[low] docs** — Module docstring says the receiver "yields each pressed digit"; `feed()` is a
  push-style `str|None` method, not a generator (rule 27). Reword to "surfaces each pressed digit exactly
  once (one return per completed key-press)".
- [ ] **[low] api** — `_REDUNDANT_END_COUNT=3` is hardcoded with no override, and `event_payloads` has no
  marker-bit / first-packet semantics (RFC 4733 §2.5.1.2 wants marker on the first packet; `rtp.py`'s
  `RtpPacket.marker` exists). Document that marker handling is the transport's job + which index is "first";
  consider making the end-count a keyword arg.
- [x] (done #398 — gap hysteresis: `_INBAND_GAP_RELEASE_FRAMES=3`, clears `_emitted` only after N consecutive gap frames) **[medium] correctness** — `InbandDtmfDetector.feed` (lines 464-490) treats any
  non-validated frame as a full press-end: a gap frame resets `_emitted`/`_candidate`/`_run`
  together (lines 473-479), so a single silent/lost frame in the MIDDLE of one physical
  keypress (packet loss, a brief dip below `_INBAND_MIN_FRAME_ENERGY`) makes the tone's
  resumption debounce as a brand-new press and emit the SAME digit a second time — one
  keypress, two `DtmfDigit`s. This is a security-relevant control (ADR-0009/0010 confirmation
  channel); a duplicated digit could double an irreversible confirmation or desync a
  multi-digit PIN/menu flow. Add hold-over tolerance (require N consecutive gap frames, not
  one, before resetting) or track "still within one press" separately from "candidate
  validated"; test one dropped frame mid-press emits exactly once.
- [ ] **[low] robustness** — `InbandDtmfDetector._detect_frame` (line 494) computes
  `n = len(pcm16) // 2` but calls `struct.unpack(f"<{n}h", pcm16)` on the FULL buffer, not
  `pcm16[:2 * n]` — verified an odd-length buffer (e.g. 5 bytes) raises `struct.error: unpack
  requires a buffer of 4 bytes`, not a `ValueError`, from a `feed()` whose docstring promises
  `str | None`. Reachability from a real G.711 caller is unverified (frames should always be
  even-length), but it's an uncaught-crash path on malformed input. Slice to
  `pcm16[:2 * n]` before unpacking (silently drop the trailing odd byte) or raise a typed
  `ValueError` up front; test an odd-length frame.

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
- [x] (#233) **[medium] api** — Resample path is byte-oriented; nothing returns/consumes `PcmFrame`, forcing the
  hot-path loop to unwrap frames and re-stamp `sample_rate`/`monotonic_ts_ns` by hand (the error-prone
  part — a forgotten rate update makes the frame lie). Add `resample_frame(frame) -> PcmFrame` that sets
  the new rate and propagates ts; document the ts policy. Keep the byte-level primitives.
- [x] (done #217) **[medium] robustness** — `decode_ulaw`/`decode_alaw` validate nothing (asymmetric with the
  validating encode path), so a zero-length/truncated payload becomes a silent empty/short frame. Add
  tests pinning empty-input behaviour; decide whether zero-length payloads should be rejected (here or in
  the transport/jitter layer) and document the deliberate no-validation choice for decode.
- [x] (done #217) **[medium] test** — Continuity test only covers 8k↔16k; the 24k↔8k TTS path (ADR-0004/0005) and
  24k↔16k are untested — exactly the non-integer-ratio cases where boundary clicks appear. Extend the
  parametrize to include 24000↔8000/16000↔24000/24000↔16000; add sample-count sanity for 24k↔8k.
- [x] (done #217) **[medium] test** (partial — residual: wide resample tolerance `300 <= out_samples <= 340` (line 63) unchanged; no a-law near-lossless value test; no golden-vector assertion (e.g.) — Mutation-weak assertions: wide resample tolerances (`300 <= … <= 340`); the
  ulaw/alaw one-byte-per-sample tests assert no decoded values (a `lin2ulaw`→`lin2alaw` swap survives);
  a-law has NO round-trip value assertion at all (a `decode_alaw`→`ulaw2lin` mutant survives). Add an
  a-law near-lossless value test; tighten counts; add an `encode_ulaw != encode_alaw` discrimination test;
  add golden-vector assertions (e.g. mu-law of PCM 0 is 0xFF).
- [ ] **[low] efficiency** — Every encode/decode/resample returns a fresh `bytes`; ~200 short-lived
  allocations/sec/call with no buffer reuse and no measurement recorded (AGENTS 22 wants a number).
  audioop has no in-place variant, but add a microbenchmark of per-frame encode+resample+decode latency
  and record it against the ADR-0005 budget; consider batching frames per `ratecv` call (state-carrying,
  correctness-preserving).
- [x] **[low] docs** — `ratecv` low-pass weights are left at defaults `(1, 0)` with no comment — a real
  audio-quality decision for the STT path. Add a one-line comment (defaults adequate for narrowband
  speech) or expose weights as params; record in ADR-0005. — shipped #298
- [x] **[low] test** (partial — residual: no assertion against a known-good G.711 reference value (e.g. `encode_ulaw(pcm16_of_0) == b'\xff'`); bare `decode_ulaw`/`decode_alaw` distin) — No test pins that `decode_ulaw`/`decode_alaw` are distinct codecs / that
  `ulaw_to_frame` uses mu-law specifically (the frame round-trip uses mu-law both ways, so a global
  mu→a swap still passes). Assert against a known-good G.711 reference value. — shipped #298
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
- [x] **[low] polish** — `_MONO` is private while `G711_SAMPLE_RATE` is public; the mono-only assumption
  is silent (a stereo buffer has even length so `_validate_pcm16` passes and produces garbage). Document
  the mono-only contract in the function docstrings (raw-bytes callers carry no channel metadata). — shipped #298
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
- [x] (#225) **[medium] robustness** — `PcmFrame` performs no validation (no `__post_init__`). An odd-length
  `samples` truncates silently (`len // 2`, line 36); `sample_rate=0`/negative passes downstream. The
  canonical 50pkt/s currency type bypasses `media/audio._validate_pcm16`. Add `__post_init__` asserting
  whole-sample length and `sample_rate > 0` (propagate per rule 37); if the per-frame cost is a concern,
  use a `PcmFrame.validated(...)` factory and document the choice.
- [x] (#225) **[medium] robustness** — `Transcript.confidence` and `GuardResult.score` advertise 0.0..1.0 but
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
- [x] **[medium] robustness** — `gate_tool_call` returns a bare bool — a hard-block leaves no audit trace — verified shipped on main 2026-06-27 (providers/policy.py GateDecision typed reason (ADR-0085))
  of *why* (unconfirmed vs degraded), exactly what an operator needs. Return a typed `GateDecision(allowed,
  reason)` with a discriminated `GateReason`, or accept an audit sink; strengthen tests beyond `is
  True/False` to assert the reason.
- [ ] **[low] api** — `policy.py` imports `GuardResult` only to read one bool in `record()` — more
  coupling than needed. Use `record(self, *, degraded: bool, …)` or a narrow `Protocol`; note the decision
  if guard/policy are intentionally one ADR-0009 unit.
- [x] **[low] api** — `ProviderRegistry` exposes no `__contains__`/`has`/`unregister`/get-factory, no
  memoisation (`make` always instantiates despite the "resolve at startup" intent), and `T` is unbounded.
  Add `__contains__`/`has`, consider a memoising `make` for the singleton case, optionally bound `T`;
  document that `make()` instantiates fresh. — shipped #296
- [x] **[low] test** — Registry tests don't assert the factory is called lazily/exactly once; the
  duplicate-message assertion is a loose substring. Add a counter fake (0 after register, 1/2 after
  make(s)); tighten the duplicate assertion to include kind + quoted name. — shipped #296
- [ ] **[low] efficiency** — `names()` re-sorts the dict each call (registry.py:49) — fine (startup/
  diagnostic), but add a one-line comment so a future caller doesn't reach for it per-turn.
- [x] **[low] api** (partial — residual: the six individual modules (asr.py, tts.py, guard.py, policy.py, transport.py, registry.py) still define no __all__, so deep-path imports ar) — `providers/__init__.py` exports nothing (no `__all__`, no re-exports); none of the
  seven modules define `__all__`. The most-imported contract (ADR-0004) forces deep-path imports. Re-export
  the canonical public names from the package under one `__all__`. — shipped #294
- [x] **[low] api** — `GuardVerdict`/`ToolRisk` are documented "ascending severity" but derive from plain
  `Enum` (uncomparable) — the claim implies a comparability that doesn't exist (rule 27). Make them
  `IntEnum`/add a `severity` property if thresholding is needed, else soften the docstrings. — shipped #294
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
- [x] **[medium] polish/DRY** — **Duplicated control-character injection guard.** Identical constants +
  predicate in `message.py` and `digest.py`. A single shared helper prevents drift of a security
  invariant. (See message.py item.) message.py + digest.py deduped via _chars.contains_control (#277); refer.py migrated to _chars.contains_control — shipped #286
- [x] **[medium] polish/DRY** — **RFC 1982 serial-number arithmetic** is implemented in `rtp.py`
  (`_seq_before`/`_seq_next`) and conceptually needed by `dtmf.py` (timestamp dedup) and future RTCP/RTP.
  Decide: promote to a shared public seqnum module, or keep private and document why each consumer's
  approach differs (rtp uses serial arithmetic; dtmf uses exact-equality + bounded window — both correct
  for their case). — shipped #297
- [x] **[medium] api/consistency** — **`__all__` discipline is inconsistent.** The root `__init__`
  declares `__all__`; `media/audio.py`, `dtmf.py`, `message.py`, and every `providers/*` module do not.
  Adopt `__all__` uniformly so the public surface is explicit and private helpers stay out of
  star-imports/autodoc. media/audio.py (#279) + call_context/hermes_surface/notice_filter/provider_error (#281) now have `__all__`; media/dtls.py, media/srtp.py, media/srtcp.py — shipped #285
- [x] **[medium] api/consistency** — **`typing.Final` on module constants is inconsistent.**
  `media/audio.py` annotates public constants with `Final`; `dtmf.py`/`rtp.py` do not. Adopt `Final`
  uniformly for mutation-resistance and consistency. — shipped #297
- [ ] **[medium] correctness/consistency** (partial — residual: audioop.error not wrapped/documented as propagating.) — **Exception-type contract is inconsistent across the
  foundation.** Some guards raise `ValueError`/`SdpError`, others leak `AttributeError` (message.py
  reason-less status) or `audioop.error` (media/audio decode/resample). Define and document one coherent
  exception contract per layer (parse layers raise `ValueError`/domain errors on hostile input; never leak
  a foreign type a caller wouldn't catch). Audit each public function's docstring against what it actually
  raises (rule 27, rule 37).
- [x] **[medium] robustness/consistency** (partial — residual: Resampler rate validation IS now implemented (src/hermes_voip/media/audio.py:201-215: validates positive rates and plain int). However: src/) — **Constructor-time validation is uneven.** `JitterBuffer` — shipped #282 (transport Literal + expires>=0)
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

- [x] (#250) **[low] correctness** — `build_in_dialog_request` can emit a CSeq >= 2**31, the exact value its own
  parser rejects (RFC 3261 §8.1.1.5). `dialog.py:230` computes `next_cseq = dialog.local_cseq + 1` with no
  upper-bound guard, while the parse path (`_cseq`, lines ~339-345) explicitly raises `DialogError` when
  `sequence >= _MAX_CSEQ (2**31)`. A dialog at `local_cseq = 2**31 - 1` produces `CSeq: 2147483648 BYE`
  (verified) — a value the same module would reject on parse. Fix: guard `next_cseq` against `_MAX_CSEQ` in
  `build_in_dialog_request`; TDD red test asserting the dialog at `2**31 - 1` raises rather than emitting
  the out-of-range value.

## src/hermes_voip/incall.py

- [x] (#229) **[medium] correctness** — An offer-carrying re-INVITE with no `m=audio` line is misclassified as
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

- [x] (#231) **[medium] robustness / test** — SIP parse error in TLS/WSS `_dispatch` kills the entire connection and
  drops all active calls with no regression test. `transport/connection.py:601-605` and
  `transport/ws_connection.py:364-368` call `SipRequest.parse()` / `SipResponse.parse()` with no
  `try/except`; a single malformed message tears down all calls simultaneously. The behaviour is documented as
  intentional but has zero regression tests for: (1) parse error → connection loss (not silent drop);
  (2) reconnect supervisor fires; (3) active calls terminate cleanly. Needs tests in `tests/test_adapter_reconnect.py`.
- [x] **[medium] robustness** — `WssSipTransport` inbound CANCEL falls through to unroutable in `ws_connection.py`. (Wave-3: ws_connection.py CANCEL handling implemented on branch fix/wss-inbound-cancel-2 but NOT merged — review found it is a partial-ship: also needs adapter.py to wire on_cancel into the WssSipTransport construction (mirror the TLS path) + fix the stale adapter.py:1148 comment. Complete the adapter wiring orchestrator-direct or in a fresh session, then merge.) (#300)

## src/hermes_voip/guard/_onnx_runtime.py

- [x] **[low] robustness** — `_injection_label_index` (line 146): `json.loads(config_path.read_text(...))` has
  no `try/except`. A corrupt `config.json` (partial write, truncated model download) raises a bare
  `json.JSONDecodeError` with no context about which file or why. Wrap with
  `ValueError(f"corrupt config.json at {config_path}: {e}") from e` for actionable diagnostics. — shipped #289

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
- [x] (done #210) **[high] observability** — Wire RTCP `CallQuality` snapshot to structured log fields. `adapter.py:5181-5190`
  logs RTCP teardown quality as a plain `%`-formatted string with no `extra={}` kwargs (runbook-0014 §Packet
  loss & jitter marks emission as "SOURCE EXISTS, EMISSION TBD"). No test asserts the teardown log line is
  emitted when `_rtcp_active` is True — every adapter test that reaches `_teardown_call` explicitly sets
  `engine._rtcp_active=False` to skip the path (`tests/test_adapter.py:680,746,1628,2086,2148,2247`). Emit
  structured log dict with `event='rtcp_call_quality'`; add a test with a fake engine where
  `_rtcp_active=True` using `pytest caplog`.
- [x] (done #210) **[high] observability** — Add structured per-call lifecycle log events with machine-parseable fields for
  SLO counting. Current log lines at `adapter.py:2819-2822` (INVITE received), `:2852-2856` and `:2944-2950`
  (REJECTED), `:4722` (200 OK), `:4861` (CallLoop started) use positional `printf`-style with no `extra=`
  dict. Runbook-0014 §Call setup success marks these events as NOT YET INSTRUMENTED. Emit
  `logging.info(..., extra={'event':'invite_received','call_id':...,'outcome':'rejected','sip_code':488})`
  — no new infra, stdlib `logging` supports `extra=` natively.
- [x] (done #210) **[medium] observability** — Emit per-call duration and concurrent-call gauge to structured log at
  teardown. `adapter.py:902` maintains `_admitted_calls` (live set) whose `len()` is the real-time concurrency
  count but is never logged at admission (`:5248`) or release (`:5258`). Call duration is never logged: the
  INVITE-received timestamp is a log string only (`adapter.py:2819`) with no `time.monotonic()` anchor and
  `_teardown_call` (`:5173`) has no start-time to subtract. Runbook-0014 §Concurrent calls marks
  `voip.calls.active_count`/`started`/`ended` as NOT YET INSTRUMENTED. Add `time.monotonic()` at admission;
  log `duration_s` and `len(_admitted_calls)` at release.
- [x] (done #399 — teardown emits `event='rtcp_dormant'` with a fixed reason code, coalesced to `not_negotiated` when no decision was reached) **[medium] observability** — Log RTCP dormant-path reason per call so operators know which calls lack
  quality data. When RTCP stays dormant (secured call without SRTCP keys, kill-switch off, or PT conflict),
  `adapter.py:5179` silently skips the call-quality log — no record that a given call produced no quality data
  or why. On the live test gateway (SDES-SRTP, runbook-0014 §Secured paths: RTCP is dormant), every
  production call is silent at teardown. Emit a single `DEBUG/INFO` "INVITE %s: RTCP dormant — no call
  quality data (secured=%s, enabled=%s)" at teardown (`adapter.py`, `media/engine.py`).
- [x] (done #210) **[medium] observability** — Add test asserting RTCP call-quality INFO log is emitted at teardown when
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

- [x] (#224) **[medium] security** — Supply-chain advisory audit only fires on dependency-file changes — no scheduled
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
- [x] (#227) **[medium] docs** — Add outbound SIP CANCEL coverage to runbook 0007. ADR-0069 (outbound SIP CANCEL,
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
- [x] (done #219) **[medium] docs** — README reports stale tool count: "9 tools, 1 hook" should be "10 tools, 1 hook".
  `README.md:210` says the plugin registers `9 tools, 1 hook`. `plugin.yaml provides_tools` (lines 41-50) has
  10 entries including `transfer_attended` (added in commit `d8097cc`). The discrepancy will mislead operators.

## docs/plan/IMPLEMENTATION-PLAN.md

- [x] (#227) **[medium] docs** — `IMPLEMENTATION-PLAN.md` is massively stale: describes the entire plugin as
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

- [x] (done #207) **[high] api** — `providers/__init__.py` re-exports only `Providers` and `build_providers`; the 12
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
  (`src/hermes_voip/__init__.py`, `config.py`, `sip.py`, `digest.py`, `message.py`). **Partial fix verified
  2026-07-02:** PR #324 (commit `452a456`) promoted `MediaConfig`, `GatewayConfig`, `ConfigError`, `Providers`,
  `build_providers`, `PcmFrame` into `hermes_voip.__all__` — that half of this item is done. Still verified
  open by a live `dir(hermes_voip)` check: the submodule-attribute leak (`caller_modes`/`config`/`digest`/
  `message`/`plugin`/`registration`/`sip` still resolve as attributes of the package — an `__all__` on a
  module governs `from x import *`, not `dir()`/attribute visibility of an already-imported submodule, so
  this needs an explicit `del` or import restructure, not just an `__all__` addition) and `StreamingASR`/
  `StreamingTTS`/`InjectionGuard` are still absent from the top level. Leaving unchecked.
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
- [x] **[medium] api** — `__all__` missing from modules added after the original backlog audit: `call_context.py`,
  `hermes_surface.py`, `notice_filter.py`, `provider_error.py`, `media/call_loop.py`, `media/dtls.py`,
  `media/srtp.py`, `media/srtcp.py`. All expose implementation-private symbols to star-imports and autodoc.
  The existing backlog item (line 566) enumerates the original modules; this covers the new ones. 4 non-media modules done (#281); media/dtls.py, media/srtp.py, media/srtcp.py — shipped #285

## Packaging / release

- [x] **[high] operability** (resolved by this PR — tag-triggered `.github/workflows/publish.yml` (build → github-release → pypi-publish) + runbook 0019 "Automated publish on tag"; version single-sourced earlier in #187/#181) — No automated version bumping mechanism or release workflow. The version is
  no longer hardcoded in three places: it is single-sourced from `pyproject.toml [project].version`
  (`hermes_voip.__version__` derives from install metadata; `plugin.yaml` ×2 are pinned equal by the suite —
  #187), and pushing a `vX.Y.Z` tag now runs `publish.yml`, which guards tag↔`pyproject` version equality,
  builds + wheel-smokes the artifacts, creates the GitHub Release, and publishes to PyPI via OIDC Trusted
  Publishing (no stored token). The drift guards live in `tests/test_plugin_manifest.py` (all four version-sync
  tests), so a release is a single `pyproject.toml` edit + the two `plugin.yaml` copies.
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

- [x] **[high] security** (#193) — `transfer_blind(target, ...)` passes an agent-supplied `target` (extension OR
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

## Gap-review discoveries (Wave 6, 2026-06-23)

### Correctness

- [x] (done #218) **[medium] correctness** — Dialog route set does not split comma-combined Record-Route headers (RFC 3261 §7.3.1) — mid-dialog routing corrupted on multi-proxy paths. `dialog.py` builds the in-dialog route set directly from `response.headers_all("Record-Route")` (dialog.py:142 for the UAC, :182 for the UAS) without splitting single header values that combine several URIs with top-level commas (e.g. `Record-Route: <sip:p1;lr>, <sip:p2;lr>`), causing the entire multi-proxy route set to collapse into one element and in-dialog BYE/re-INVITE to emit a single malformed `Route:` line. Fix: lift `registration._split_contacts`' angle/quote-aware top-level-comma splitter into a shared helper and apply it over each Record-Route value before building `route_set`; TDD red test parsing a 2xx with a comma-combined Record-Route row and asserting `route_set` has two elements in the correct order plus a BYE emitting two separate `Route:` lines (`src/hermes_voip/dialog.py:142,182,238`, `registration.py:43-71`, `tests/test_dialog.py`).
- [x] (done #216) **[low] robustness** — SIP stream framer faults the connection on a line-folded Content-Length instead of unfolding it (RFC 3261 §7.3.1). `framing._content_length` (framing.py:110-126) splits the head line-by-line but never unfolds RFC 3261 §7.3.1 continuation lines, unlike `message._parse_headers` (message.py:59-67); a `Content-Length:\r\n  42` row yields an empty value that fails `isdecimal()` → `FramingError` and tears down the TLS stream. Fix: unfold continuation lines in `_content_length` (or share `message._parse_headers`'s unfold step) before the name scan; add a unit test feeding a folded Content-Length and asserting the message frames (`src/hermes_voip/transport/framing.py:110-126`, `tests/test_framing.py`).

### Robustness

- [x] (#226) **[medium] robustness** — `intercom` relay_token: CRLF/control chars accepted at load, misfires at door-open time. `load_intercom_config` (intercom.py:245) only calls `.strip()` on the relay bearer token; a value containing embedded CRLF or NUL bytes is silently accepted, then raises a bare `ValueError` inside `_open_blocking`'s `asyncio.to_thread` call that bypasses the `(urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError)` except chain (intercom.py:150-157), violating the `open_entry_handler` docstring's `IntercomRelayError` contract. Fix: add control-char rejection in `load_intercom_config` at intercom.py:245 with a `ConfigError`; add a `ValueError` catch in `_open_blocking` re-raised as `IntercomRelayError` (`src/hermes_voip/intercom.py:150-157,245`, `voip_tools.py`).
- [x] (#226) **[medium] robustness** — `multi_intercom` webhook header names/values: CRLF not validated at load, `ValueError` leaks at fire time. `_parse_headers` (multi_intercom.py:410-428) validates only `isinstance(value, str)` — not control characters (CRLF, NUL) — so a misconfigured intercom JSON with a CRLF-injected header is silently accepted; when `fire_webhook_opening` is called, `http.client` raises `ValueError('Invalid header value …')` that is NOT in the except chain at lines 504-515 (only HTTPError, URLError, TimeoutError, OSError), propagating uncaught through `asyncio.to_thread` and violating the `WebhookError` exception contract. Fix: add `_reject_controls` to `_parse_headers`; add `ValueError` catch in `_fire_webhook_blocking` re-raised as `WebhookError` (`src/hermes_voip/multi_intercom.py:410-428,484-515`).

### Security

- [x] **[high] security** (#198) — SRTP SDES answer accepts the weakest offered crypto suite (no SHA1_80-over-SHA1_32 preference) — downgrade-by-offer-order. `_negotiate_answer_crypto` (sdp.py:2042) accepts `audio.crypto_attrs[0]` — the FIRST `a=crypto` line in raw offer order — with no strength ranking; both AES_CM_128_HMAC_SHA1_80 and _32 are supported (sdp.py:76-80), so a gateway that lists AES_CM_128_HMAC_SHA1_32 before _80 gets the weaker 32-bit SRTP auth tag accepted, a silent integrity/replay-protection downgrade. Fix: select the strongest supported offered suite (SHA1_80 > SHA1_32) instead of `[0]`; TDD a red test offering _32-then-_80 asserting the answer echoes the _80 suite (`src/hermes_voip/sdp.py:2042`, `tests/test_sdp.py`).
- [ ] **[low] security** — Inbound-REFER Refer-To target is parsed without the dialable/injection allowlist that guards outbound REFER and INVITE. `parse_refer` (refer.py:351-368) returns `refer_to`/`replaces` with no validation; `build_triggered_invite` (refer.py:370-409) interpolates that attacker-influenced target straight into a fresh out-of-dialog INVITE request-line and To header — the receive-side analogue of the outbound injection that `_validate_transfer_target` (refer.py:124) already closes. Today it is latent (no consumer of `build_triggered_invite` is wired in `adapter.py`/`voip_tools.py`/`incall.py`), so land only the validation guard as defence-in-depth: validate `refer.refer_to` through the same allowlist in `parse_refer` (or at `build_triggered_invite`) before the follow path is wired (`src/hermes_voip/refer.py:351-409`, `call.py:773-776`).

### Tests

- [x] (#251) **[low] test** — RTCP BYE ingest test is assertion-free: `call_quality` unchanged after BYE is unverified. `tests/test_media_engine_rtcp.py:303` `test_ingest_ignores_bye_without_error` only calls `engine.ingest_rtcp(build_compound((Bye(...),)))` with no assertion — only "must not raise". The BYE handler (engine.py:2313-2316) only logs; a mutation adding a state reset to the BYE branch would pass the suite. Fix: snapshot `engine.call_quality` before ingest and assert it equals the state after (`tests/test_media_engine_rtcp.py:303`, `src/hermes_voip/media/engine.py:2313`).

### Docs / drift

- [x] (#227) **[medium] docs** — `MULTIREG-CALLCONTROL-PLAN.md` is stale: PR7–PR9 marked blocked but all shipped. `docs/plan/MULTIREG-CALLCONTROL-PLAN.md:15` says "Blocked on the live transport (P2/P3): PR7–PR9 — manager.py, call.py, tools.py + adapter wiring". All three modules are fully shipped and wired end-to-end. A future agent reading the plan would skip or duplicate work. Update the plan to reflect current status (note: `docs/plan/IMPLEMENTATION-PLAN.md` staleness is separately tracked at backlog line 828).
- [ ] **[low] docs** — ADR-0066 Decision text claims engine SRTCP wiring is "a separate named follow-on" but it is already done. `docs/adr/0066-srtcp-transform.md:35-36` states the engine wiring "is not built here" and is "a separate, named follow-on". However `adapter.py:623-649` defines `_srtcp_inbound_from_offer`/`_srtcp_outbound_from_answer` and passes them into the engine on every secured call (adapter.py:3467-3468, :3798-3834); ADR-0061's amendment (docs/adr/0061-rtcp-sr-rr-rtcp-mux.md:136-167) explicitly documents the completion. Rule 27: the ADR-0066 Decision section never acknowledges this. Fix: add a Refinement subsection to ADR-0066 noting the engine wiring was completed and referencing the ADR-0061 amendment (`docs/adr/0066-srtcp-transform.md:35-36`).

### API / ergonomics

- [x] **[medium] api** — Add `__all__` to core foundation modules missing from both existing `__all__`-sweep backlog items. — verified shipped on main 2026-06-27 (tests/test_init_exports.py) The two consolidated `__all__`-sweep items in the backlog (line 571: dtmf.py/message.py/media/audio.py/providers/*; line 888: call_context.py/hermes_surface.py/notice_filter.py/provider_error.py/media/{call_loop,dtls,srtp,srtcp}.py) both omit the five core SIP/RTP/RTCP/SDP foundation modules that define the plugin's central typed surfaces: `rtcp.py` (14 public names including RtcpPacket, SenderReport, ReceiverReport, build_compound, parse_compound), `rtp.py` (RtpPacket, JitterBuffer), `sdp.py` (9+ public names including SessionDescription, AudioStream, build_audio_offer), `sip.py` (sip_address_of_record, build_request, build_response, SipResponse, SipRequest), and `registration.py` (RegistrationFlow, RegistrationConfig, outcomes). Fix: add `__all__` to all five modules listing only the intended public surface; add a package-boundary star-import test (`src/hermes_voip/rtcp.py`, `rtp.py`, `sdp.py`, `sip.py`, `registration.py`).
- [x] **[medium] api** — Promote `InboundCallContext` to `hermes_voip` top-level `__all__`. — shipped #278 `InboundCallContext` (call_context.py:298) is the structured per-call datum injected into every answered-call turn (`adapter.py:73,5954`), carrying caller identity, diversion chain, asserted-identity, and call-context text — the central type a plugin author or test author needs to inspect. The existing backlog item at line 863 promotes `MediaConfig`/`GatewayConfig`/`ConfigError`/`Providers`/`PcmFrame`/`StreamingASR`/`StreamingTTS`/`InjectionGuard` but omits `InboundCallContext`, `DiversionHop`, `HistoryInfoEntry`, and `extract_call_context`, which would remain deep-import-only after that fix. Fix: add `InboundCallContext` (and `extract_call_context`) to `hermes_voip/__init__.__all__`; add a public-boundary import test (`src/hermes_voip/__init__.py`, `call_context.py:298`).
- [x] **[low] polish/DRY** — Migrate refer.py:119-177 control-char guard to `hermes_voip._chars.contains_control` (the remaining duplicate after #277) and fix its now-stale comment "mirrors message._C0_END / message._DEL". (review residual #277) — shipped #286

### Performance / efficiency

- [x] (#252) **[low] efficiency** — Replace `struct.unpack_from` + generator peak scan with `audioop.max` in TX amplitude logging. `engine.py:2147-2150` calls `struct.unpack_from(f'<{n_samp}h', chunk)` then `max(abs(s) for s in pcm_vals)` on every outbound frame (50/s) to compute the rolling peak for the 1-second amplitude log; measured cost is ~13,342 ns/frame vs `audioop.max(chunk, 2)` at ~259 ns/frame — a 51x speedup. `audioop` is already imported at engine.py:61. Fix: replace with `audioop.max(chunk, 2)` (one line); no dependencies added (`src/hermes_voip/media/engine.py:2147-2150`).
- [x] **[medium] efficiency** — Measure and document `CallProgressDetector.on_audio_frame` per-frame cost; gate or move off event-loop hot path. — verified shipped on main 2026-06-27 (media/call_progress.py microbench + tests/test_call_progress_microbench.py) `call_loop.py:1151` calls `_feed_call_progress(frame, frame_events)` unconditionally on every decoded inbound frame (50/s) when call-progress is enabled; `call_progress.py:520` unpacks the PCM16 frame into a Python `list[float]` then runs 10+ Goertzel passes — measured ~179 µs/frame at 16 kHz, roughly 9 ms CPU/second against the 20 ms frame budget, running synchronously in the pump asyncio task. No measurement or budget note exists; not in the backlog (the generic efficiency item at line 604 covers `media/audio.py` and `rtp.py`, not `call_progress.py`). Fix options: move to the VAD worker thread; run at VAD-window rate (~31/s) rather than RTP-frame rate (50/s); run every Nth frame. Rule 22 requires a concrete number before merging (`src/hermes_voip/media/call_progress.py:520`, `call_loop.py:1151`).
- [ ] **[low] efficiency** — Replace per-frame Python list allocation in `InbandDtmfDetector._detect_frame` with `struct.unpack_from` tuple. `dtmf.py:430` allocates a new `list[float]` every inbound frame when in-band DTMF receive is enabled (`[float(v) for v in struct.unpack(f'<{n}h', pcm16)]`); measured ~22,848 ns/frame vs a cached format-string `struct.unpack_from`. Fix: use a module-level `_PCM16_FMT_8K` constant and `struct.unpack_from`, omit the `float()` coerce (inner loop arithmetic coerces); saves ~7 µs/frame at 50 fps. The existing backlog "Every encode/decode/resample returns a fresh bytes" item covers `audio.py`/`rtp.py` but not `dtmf.py` (`src/hermes_voip/dtmf.py:430`).
- [x] **[medium] observability** — Add per-frame CPU microbenchmark for `call_progress.on_audio_frame` and record against ADR-0005 budget. — verified shipped on main 2026-06-27 (media/call_progress.py microbench + tests/test_call_progress_microbench.py) Rule 22 requires concrete per-frame latency numbers for every hot-path module; `CallProgressDetector.on_audio_frame` benchmarks at ~179 µs/frame (16 kHz, 10 Goertzel passes) — ~9 ms CPU/second — yet no microbenchmark, no ADR-0005 budget reference, and no per-frame comment exists. Add a benchmark in `tests/` or `benchmarks/` with `pytest-benchmark` or `timeit`; add the measured figure as a comment near `on_audio_frame` (call_progress.py:482) and the pump's feed site (call_loop.py:1151), following the pattern at engine.py:267 (`src/hermes_voip/media/call_progress.py:482`, `call_loop.py:1151`).
- [ ] **[low] efficiency** — Replace Python list + `struct.pack` splat in `linear_fade_out` with `bytearray` + `struct.pack_into`. `audio.py:162-170` calls `list(struct.unpack(f'<{total}h', pcm16))` — allocating a Python list of ~160 ints — then `struct.pack(f'<{total}h', *samples)` with a variadic splat; measured ~51,588 ns/call. A `bytearray`-based rewrite (`ba = bytearray(pcm16)` then `struct.pack_into('<h', ba, offset, val)`) avoids both the list allocation and the `*samples` splat via in-place writes. ADR-0028's barge-in contract requires the fade to complete within one frame, so this cannot be deferred (`src/hermes_voip/media/audio.py:162-170`).
- [x] (#310) **[medium] efficiency** — `RtpMediaTransport._next_datagram()` (engine.py:1841-1872) creates+cancels asyncio tasks for recv_queue.get()/stop_event.wait()/watchdog on EVERY receive iteration: ~100-150 task objects/sec/call on the event-loop hot path. Refactor to a no-per-packet-task wait that preserves stop/watchdog semantics. (perf gap-review Wave-1 2026-06-27)
- [x] (#310) **[medium] efficiency** — `send_audio()` (engine.py:1988-2004) appends to an immutable bytes `_tx_buffer` then front-slices while draining → O(n) reallocation per emitted frame on the outbound media hot path. Replace with a bytearray/cursor (ring-buffer shape) so reframing stays sample-continuous without copying the remainder. (perf gap-review Wave-1 2026-06-27)
- [x] **[low] efficiency** — STT worker counts fed samples via `len(item.tobytes()) // 4` (sherpa_onnx.py:294-295), materialising a ~1280-byte copy per 20ms frame (50/sec/call). Extend the FloatArray protocol (stt/resample.py) with a length/size surface and count without tobytes(). (perf gap-review Wave-1 2026-06-27) — shipped #295

### Observability

- [x] **[medium] observability** — Add structured `extra={}` fields to spoken-error replacement log in `adapter.py`. — verified shipped on main 2026-06-27 (adapter.py provider_error_replaced + tests/test_adapter.py caplog) The runbook-0014 §Error handling lists `voip.errors.spoken_to_caller` as NOT YET INSTRUMENTED. The only code path that detects an error-spoken-to-caller event is `adapter.py:1390-1395` where `is_provider_error()` triggers a `WARNING` log with a plain `%`-format string and no `extra={}` kwargs. Fix: add `extra={'event': 'provider_error_spoken', 'call_id': chat_id}` to the existing `logging.warning` call; add a `caplog` test asserting the event fires (`src/hermes_voip/adapter.py:1390-1395`).
- [x] **[low] observability** (#276) — Thread `call_id` into call-progress event log and add structured `extra={}` fields. `call_loop.py:1016` logs `'call-progress: %s at %.2fs'` inside `_surface_call_progress()` with no `extra={}` fields and no `call_id`, so AMD/fax/progress detections (ADR-0064) cannot be correlated to a specific call in structured log queries. `CallLoop` stores `self._call_id` (line 688). Fix: add `extra={'event': 'call_progress', 'call_id': self._call_id, 'kind': event.kind.value, 'elapsed_s': event.elapsed_s}` to the INFO call (one-line change) (`src/hermes_voip/media/call_loop.py:1016`).
- [x] **[low] observability** (#274) — Add structured `extra={}` to registration lifecycle log events in `manager.py`. `manager.py:353-355` emits `'SIP registration established'` as plain `%`-format strings with the `expires` value embedded in message text, not as a typed `extra=` field, requiring automated log aggregators to regex-parse the numeric value. Fix: add `extra={'event': 'sip_registration_established', 'expires_s': outcome.expires}` to the INFO call and `extra={'event': 'sip_registration_refreshed', 'expires_s': outcome.expires}` to the DEBUG call (rule 34 respected — extension number is deliberately omitted per existing comment at manager.py:348-351) (`src/hermes_voip/manager.py:353-355`).
- [x] **[low] observability** (#275) — Emit structured event log on TTS failover to primary so failover rate is SLO-observable. `tts/failover.py:344` and `:350` emit WARNING log lines for pre-audio and mid-utterance primary failure with no `extra={}` fields, so TTS provider availability cannot be counted in structured log aggregation. Fix: add `extra={'event': 'tts_primary_failover', 'emitted_frames': self._emitted}` at both sites (two-line change; the provider interface is call-agnostic by design so no `call_id` is available) (`src/hermes_voip/tts/failover.py:344,350`).

### UX / conversational

- [x] (done #211) **[high] ux** — On a guard REFUSE the caller hears only dead air — no spoken acknowledgment, no recovery path. `call_loop.py:2020` — when `result.verdict is GuardVerdict.REFUSE`, `_screen_and_deliver` records the verdict and returns without calling `deliver_turn` and without scheduling a comfort filler (line 2026 is gated behind the non-REFUSE branch), so a legitimate caller false-positived by the injection guard experiences pure silence and will typically repeat themselves into the same wall, then get no-input-reprompted and eventually hung up on. Fix: on REFUSE, speak a short language-keyed safe decline line through `_speak_phrase_best_effort` so the caller gets conversational feedback; TDD a red test that a REFUSE verdict drives exactly one spoken phrase via TTS and still does NOT call `deliver_turn`; honour the rng/no-immediate-repeat and language-keyed conventions (ADR-0054/0057). The two existing REFUSE backlog items (lines 717-723) cover caller-active flags and mutation coverage only — none addresses spoken feedback to the caller (`src/hermes_voip/media/call_loop.py:2020-2026`).
- [x] **[medium] ux** — Provider/runtime error spoken apology has no operator override and is English-hardcoded with no fallback line per language. — verified shipped on main 2026-06-27 (provider_error.py _SAFE_ERROR_REPLY_BY_LANGUAGE (5 langs) + HERMES_VOIP_ERROR_APOLOGY) `provider_error.py:_SAFE_ERROR_REPLY_BY_LANGUAGE` has only an `'en'` entry; `adapter.py:1388-1389` selects it by `self._media_cfg.language`; unlike comfort-filler/reprompt/goodbye phrases (which the operator can override via env), the error apology is entirely uncustomisable and silently falls back to English for any non-`'en'` language. Fix: add a `safe_error_reply` phrase to the language-keyed config mechanism with a `HERMES_VOIP_SAFE_ERROR_REPLY` env override and a `MediaConfig` field, mirroring comfort-filler plumbing; thread into the adapter's `safe_error_reply` call. This is narrower than the broadly-tracked multi-language item (backlog line 747-762), which lists comfort-filler/reprompt/goodbye/greeting but NOT the error-reply line (`src/hermes_voip/provider_error.py`, `adapter.py:1388`, `config.py`).
- [ ] **[low] ux** — Empty / whitespace-only ASR final is delivered as a real caller turn (phantom empty turn to the agent). `call_loop.py:1300` puts `transcript.text` onto `transcript_q` whenever end-of-turn is reached, with no guard against empty or whitespace-only text; a cough or door slam endpointed as a turn emits an empty final that is guard-screened and handed to `deliver_turn` (line 2027) as a genuine caller utterance, prompting a confused agent reply. Fix: skip delivery when `transcript.text.strip()` is empty; optionally surface to the no-input watchdog as a non-event so it does not reset the silence window; TDD a red test that an end-of-turn transcript with text `''` or `'   '` does NOT reach `deliver_turn` while a non-empty one does (`src/hermes_voip/media/call_loop.py:1300`).

### Operability

- [x] (done #206) **[high] docs** — Runbook 0013 §3 falsely states graceful shutdown is unimplemented — contradicts §Restart in the same file. `docs/runbooks/0013-voip-incident-oncall.md` lines 496-511 say "The plugin does not currently implement graceful shutdown … A hard kill: kill -9" — but ADR-0059 shipped a full BYE-drain `disconnect()` (adapter.py:1175-1240), and the same runbook's Restart §1 (lines 523-536) already correctly documents `kill -TERM` + drain with the expected log line. An on-call engineer reading §3 will hard-kill a running gateway, dropping live callers, when a graceful SIGTERM is available. Fix: update §3 to match §Restart; align both sections to describe the shipped ADR-0059 behaviour (rule 27) (`docs/runbooks/0013-voip-incident-oncall.md:496-511`).
- [x] (#222) **[medium] docs** — Admission-control and shutdown-drain knobs (`HERMES_SIP_MAX_CALLS`, `HERMES_SIP_SHUTDOWN_DRAIN_SECS`) absent from `plugin.yaml` `optional_env`. Both env vars are declared and validated in `config.py` (lines 104-107, 661-717) and operational (runbook-0013 lines 151-156, 526), but neither appears in `src/hermes_voip/plugin.yaml` `optional_env` (verified: grep returns no output). An operator tuning capacity or adjusting how long live callers are given to finish on restart has no manifest-visible signal these knobs exist. Fix: add both to `plugin.yaml optional_env` with description and default (rule 42) (`src/hermes_voip/plugin.yaml`, `config.py`).

### Packaging

- [x] **[medium] packaging** (#196) — Add missing PEP 621 metadata fields to `pyproject.toml`. The `pyproject.toml` is missing standard PEP 621 metadata fields: `license` (SPDX identifier), `authors`/`maintainers`, `keywords`, `classifiers` (trove identifiers: project status, intended audience, environment), and `urls` (homepage, documentation, repository). These fields are expected by package managers and help with discoverability, compliance tooling, and GitHub's license recognition. The package is PUBLIC (CLAUDE.md invariant) and should carry a clearly visible license declaration (`pyproject.toml:1-9`).
- [x] **[low] packaging** (#196) — Add a top-level `LICENSE` file to the repo. The repository lacks a top-level `LICENSE` file. While `pyproject.toml` can declare a license via SPDX identifier, a committed `LICENSE` file is the canonical location that package managers, license-scanning tools, GitHub's automatic license detection, and automated compliance tooling expect to find. The package is PUBLIC (CLAUDE.md invariant) and needs a clearly visible license statement at the repo root.
- [ ] **[low] packaging** — Explicitly declare `py.typed` in wheel artifacts for clarity. The package ships a `py.typed` marker file (`src/hermes_voip/py.typed`) signalling to type checkers that the package is fully typed. While hatchling includes `py.typed` by default, it should be explicitly listed in `[tool.hatch.build.targets.wheel].artifacts` alongside the existing `plugin.yaml` and `SKILL.md` entries (pyproject.toml:99-105) to prevent accidental removal during future refactors (rule 20) (`pyproject.toml:99-105`, `src/hermes_voip/py.typed`).

### Product features

- [x] **[medium] ux** — `place_call` tool flattens SIP outbound failure (busy / no-answer / declined) into a generic redacted error instead of a structured outcome. — verified shipped on main 2026-06-27 (voip_tools.py PlaceCallOutcome + _classify_outbound_failure) `place_call_handler` (voip_tools.py:868-907) catches only `OutboundCallNotAllowed`; an `OutboundCallFailed` (busy 486, no-answer 480/408, declined 603, congestion 503, secure-media 488) falls through to the generic `except Exception` → `_tool_failure` (voip_tools.py:125-128), so the placing agent sees an opaque `'place_call failed: <exc>'` with no SIP status and cannot distinguish "line was busy, retry later" from "caller declined" or "internal error". The `transfer_attended` consult path already handles this correctly (voip_tools.py:1160-1167). Fix: catch `OutboundCallFailed` in `place_call_handler` and return a structured `{"error": ...}` naming the outcome class (busy / no-answer / declined / unreachable) derived from the SIP status, mirroring the consult handler (`src/hermes_voip/voip_tools.py:868-907`, `originate.py:35-44`).
- [x] **[medium] ux** — Agent `place_call` has no bounded ring timeout — an unanswered outbound call blocks on the gateway INVITE timeout. — verified shipped on main 2026-06-27 (voip_tools.py ring_timeout_secs) `place_call_with_objective` (adapter.py:1474-1512) calls `place_call` without `ring_timeout_secs`, and there is no ring/dial-timeout config at all (`rg ring_timeout|dial_timeout|RING_TIMEOUT config.py` returns nothing); when a callee rings-but-never-answers the agent's tool call awaits for however long the gateway's INVITE timeout runs (commonly 30-180 s), tying up the concurrent-outbound slot and the agent. The plumbing already exists: `place_call` accepts `ring_timeout_secs` and arms an abort/CANCEL timer (adapter.py:782, 1538-1599 ADR-0069 outbound CANCEL), but the agent tool path never sets it. Backlog line 788-790 references `ring_timeout_secs` only in a runbook-docs context, not as the missing default on the agent tool path. Fix: add `HERMES_VOIP_RING_TIMEOUT` config (sensible default e.g. 45 s) and thread it through `place_call_with_objective` → `place_call` so an unanswered outbound dial is auto-CANCELled and surfaced as a "no answer" structured outcome (composes with the structured-outcome fix above) (`src/hermes_voip/adapter.py:1474-1512`, `config.py`, `voip_tools.py`).

## Review follow-ups (Wave 6 release-blocker reviews, 2026-06-27)

- [ ] **[low] correctness** — sdp: the SIP 2xx-answer parse site (`adapter.py` ~:2034) recomputes `_sip_supported_encodings()` instead of threading the stored offered codecs like the WebRTC site (~:2689); harmonize (pre-existing; #234 review).
- [x] (#247) **[low] test** — transport: add a `caplog` test asserting the malformed-message skip log carries only exception type + length (non-PII), catching a regression that logs raw SIP content (#231).
- [ ] **[low] feature** — registration: optional in-transaction `stale=true` nonce retry (deferred by ADR-0080).
- [x] (#246) **[low] security** — CI: pin `gate.yml`/`gitleaks.yml`/`supply-chain.yml` third-party action `uses:` to commit SHAs, matching `publish.yml` (#235).
- [x] (#248) **[medium] test** — rtp: JitterBuffer SSRC auto-reset has no hysteresis — one foreign-SSRC packet flushes buffered audio (safe only because SRTP auth is above); consider N-consecutive-packet confirmation (#221).
- [x] (#249) **[low] test** — manifest: the `plugin.yaml` admission-knob test asserts presence only, not default-value parity with `config.py` (`MAX_CALLS=8`, `SHUTDOWN_DRAIN_SECS=5.0`); add a cross-check (#222).

## Operator-filed issues (Wave 7, 2026-06-27)

Tracked as GitHub issues #239–#244; resolved in PRs #253–#254.  The items
below were not in the backlog before the issues were filed.

- [x] (#254) **[medium] packaging** — `[webrtc]` extra pins `websockets==16.0`, conflicting with Hermes's own pin of 15.0.1 (issue #240). Relax to `websockets>=15.0,<17` so Hermes and hermes-voip can co-install; note the stable API surface used (connect/subprotocols/max_size/ping_interval). ADR-0083 records the constraint policy (`pyproject.toml`, `uv.lock`, `docs/adr/0083-extra-dependency-constraint-policy.md`).
- [x] (#254) **[medium] packaging** — `[media]` extra pins `cryptography==48.0.1`, conflicting with co-installed packages that require `<47` (issue #241). Relax to `cryptography>=46.0.7,<49` (floor = CVE-2026-39892/-34073 patch; ceiling keeps pyopenssl 26.2.0 satisfiable); only AES-CTR hazmat is used (`pyproject.toml`, ADR-0083).
- [x] (#254) **[medium] packaging** — `[ml]` extra pins `onnxruntime==1.24.4` which has no macOS py3.13 wheel (issue #239). Add `; sys_platform != 'darwin'` marker so macOS users relying on sherpa-onnx's self-bundled onnxruntime are not broken (`pyproject.toml`, ADR-0083).
- [x] (#253) **[low] docs** — `docs/runbooks/0011-voip-enable-plugin.md` did not explain that `hermes plugins enable/list` uses filesystem discovery only and never consults `importlib.metadata` entry-points (issue #244, upstream NousResearch/hermes-agent#23802). Cross-reference added.
- [x] (#253) **[low] docs** — `docs/runbooks/0011-voip-enable-plugin.md` did not warn that the Hermes runtime imports `hermes_voip` from the pip-installed site-packages copy, not a git-cloned `~/.hermes/plugins/hermes-voip/` directory (issue #243). Warning added so operators applying local patches target the right location.
- [x] (#245) **[low] test** — Non-muxed RTCP adapter tests were flaky: `start_rtcp(mux=False)` binds an RTP-port+1 sibling socket; tests using `local_port=0` did not reserve the consecutive pair, so under ephemeral-port churn a sibling test could hold port+1 causing the engine to degrade the call to RTCP-off and `assert rtcp_port is not None` to fail. Fix: new `reserve_consecutive_udp_pair` helper reserves the (P, P+1) pair before binding the engine to P, closing the sub-ms steal window. Reproduced at 7/200 under 800 held loopback sockets.

### Discovered — Wave-4 gap-review

- [ ] **[medium] obs** — Adapter call path: emit a structured `extra={event,…}` per-turn latency/timing log (local-only emission; observability, runbook-0014 SLOs). Note: this touches the hot serialized `adapter.py` — orchestrator-owned, single lane.
- [ ] **[low] docs** — Reconcile drift in `docs/runbooks/0009` against current behaviour (rule 27/42).
- [ ] **[low] docs/packaging** — Document the built-wheel / dist / PyPI trove classifiers + `py.typed` packaging story in README (follows #307).

## Wave-7 gap-review (discovered 2026-06-27)

Two self-referential backlog-hygiene items from the 37-candidate review were applied directly to existing lines instead of being re-added here. Six items are already in flight this wave and stay open until their PRs merge: RTCP UTF-8 CNAME parse, registration `isdigit()` parsing, `classify_provider_error()` tests, Deepgram `CloseStream`, duplicate `rtpmap`, and TLS minimum 1.2. Some of that in-flight set is tracked on pre-existing lines; newly discovered items are appended below.

### Correctness / robustness / security

- [x] (#321) **[medium] correctness** — RTCP SDES UTF-8 CNAME parse raises `UnicodeEncodeError` instead of the contracted `RtcpError` on non-ASCII inbound names. (`src/hermes_voip/rtcp.py`)
- [x] (#331) **[low] correctness** — REGISTER binding matching uses raw string equality instead of RFC 3261 SIP-URI comparison, so equivalent echoed Contacts can refresh against the wrong binding expiry. (`src/hermes_voip/registration.py`)
- [x] (#316) **[medium] robustness** — `isdigit()` admits Unicode digits that `int()` cannot parse, crashing REGISTER 200-OK handling. (`src/hermes_voip/registration.py`)
- [ ] **[low] robustness** — `_next_comfort_phrase()` / `_next_reprompt_phrase()` raise `IndexError` when the phrases tuple is empty. (`src/hermes_voip/media/call_loop.py`)
- [x] (#318) **[medium] security** — Pin the SIP-over-TLS/WSS client TLS minimum to 1.2 for downgrade defence-in-depth. (`src/hermes_voip/adapter.py`)
- [ ] **[low] correctness** — `HERMES_VOIP_RING_TIMEOUT_SECS` is validated at tool-call time instead of startup, so config errors surface late. (`src/hermes_voip/voip_tools.py`)

### Tests

- [x] (#330) **[high] test** — Held-call session refresh needs a regression test that a held call stays `sendonly`. (`tests/test_adapter_session_timers.py`)
- [x] (#317) **[medium] test** — Pin `classify_provider_error()` category tokens and precedence so structured error categories cannot drift. (`tests/test_provider_error.py`)
- [x] (#319) **[medium] test** — Deepgram shutdown should assert the exact `CloseStream` control frame, not just any text frame. (`tests/stt/test_deepgram.py`)

### Docs drift

- [ ] **[low] docs** — ADR-0019 still says "to be implemented" and shows the wrong `build_audio_offer` signature. (`docs/adr/0019-outbound-calling-uac-originate.md`)
- [ ] **[low] docs** — ADR-0010 still claims the expected-length terminator is "not yet wired" with no tracked plan. (`docs/adr/0010-dtmf-handling.md`)
- [ ] **[low] docs** — Runbook 0014 contains aspirational "NOT YET INSTRUMENTED" / "Metrics sink still TBD" language that violates rule 27. (`docs/runbooks/0014-voip-slo-metrics.md`)
- [x] (#400 verified-DONE 2026-07-03: premise stale — runbook-0013 §6 "Registrar granted a non-positive or malformed `Expires` (ADR-0088)" already documents the `select(.event=="sip_registration_failed" and .status_code==0)` jq filter + plaintext line, matching `manager.py` emission) **[medium] docs** — Runbook 0013 "registration down" diagnostics omit the ADR-0088 non-positive-expires failure log pattern. (`docs/runbooks/0013-voip-incident-oncall.md`)

### API / ergonomics

- [x] (#359) **[medium] api** — The backlog false-closed `media/call_loop.py` `__all__`; the module still lacks an explicit public boundary. (`src/hermes_voip/media/call_loop.py`) — shipped #359 (`media/call_loop.py` now declares an explicit `__all__`).
- [ ] **[low] api** — `aio.py` lacks `__all__`, so internal threading scaffold classes leak on star-imports. (`src/hermes_voip/aio.py`)

### Performance / efficiency

- [ ] **[low] efficiency** — Replace `struct.unpack_from` + generator peak scan with `audioop.max` in `call_loop._play()`. (`src/hermes_voip/media/call_loop.py`)
- [ ] **[low] efficiency** — Widen call-progress / Goertzel helpers to accept `Sequence[float]` so `on_audio_frame` can avoid a per-frame `list[float]` allocation. (`src/hermes_voip/media/call_progress.py`)
- [ ] **[low] efficiency** — Remove the redundant `float()` coercion in `EchoCanceller.push_reference()`'s per-sample loop. (`src/hermes_voip/media/aec.py`)

### Observability

- [x] (#348) **[high] observability** — Emit a structured log event on SIP registration failure so rejects and timeouts are queryable. (`src/hermes_voip/manager.py`) — shipped #348 (secret-safe structured WARNING distinguishing rejected/timeout/transport_failed outcomes)

### UX / conversational

- [ ] **[medium] ux** — A spoke-but-untranscribed caller should get a "didn't catch that" reprompt instead of silence followed by "Are you still there?". (`src/hermes_voip/media/call_loop.py`)

### Product features

- [ ] **[medium] feature** — Outbound agents cannot send DTMF through callee IVRs because `send_dtmf` is ELEVATED while the outbound persona is level 0. (`src/hermes_voip/caller_modes.py`) **Design record added 2026-07-03:** ADR-0104 (`docs/adr/0104-outbound-persona-dtmf-privilege.md`, Status: **Proposed**) recommends a direction-keyed **per-tool** `send_dtmf` grant to the outbound persona only (not a level bump; `open_entry`/transfers stay denied; inbound fails closed), **sequenced after ADR-0103's rate limit** (1380 → 1284) so the IVR-brute-force surface is bounded. Item stays open until the ADR is accepted and implemented.

## Wave-8 gap-review (discovered 2026-06-27, batch 2)

- [x] (#323) **[medium] correctness** — In-dialog NOTIFY handler treats every NOTIFY as `Event: refer` transfer progress without checking the `Event` header; an `Event: message-summary` NOTIFY triggers `parse_notify_sipfrag` and returns 400 Bad Request instead of a plain 200 OK ack. (src/hermes_voip/call.py, src/hermes_voip/refer.py)
- [x] (#326) **[high] security** — Pinned model-file sha256 is recorded in the manifest but never compared to on-disk bytes at provider build/load time; a tampered or wrong-revision model directory (including the GUARD model) loads silently. (src/hermes_voip/providers/build.py, src/hermes_voip/manifest.py)
- [x] (#359) **[medium] test** — Async integration test: REFUSE turn resets no-input watchdog — no end-to-end test combining a REFUSE guard verdict with `_SteppedSleep`/`_HoldOpenTransport`/`_no_input_loop` to kill the two mutation-surviving paths at lines 2095 and 2115. (tests/test_call_loop.py) — shipped #359 (`test_no_input_watchdog_resets_on_refuse_verdict`).
- [x] (#325) **[medium] docs** — runbook-0013:131 falsely states WSS signalling is not yet wired (ADR-0016 roadmap language); ADR-0038 shipped `WssSipTransport` and it is wired in adapter.py:1124-1146. (docs/runbooks/0013-voip-incident-oncall.md)
- [x] (#333) **[medium] docs** — runbook-0015:26-34 says the adapter "does not yet pass" no-input kwargs and "there is no `HERMES_VOIP_*` env var for these knobs"; PR #188 shipped all six `HERMES_VOIP_NO_INPUT_*` / `HERMES_VOIP_GOODBYE*` vars and wired them in adapter.py. (docs/runbooks/0015-voip-silence-reprompt-and-goodbye.md) — verified correct on main; fixed in commit de2e4f5 (PR #333): runbook now states all six kwargs are passed explicitly (`adapter.py` ~line 5104) and documents the env-var table.
- [x] (#324) **[medium] api** — `hermes_voip.transport.__all__` exports `SipOverTlsTransport` but omits `WssSipTransport` and `CallResponseSink`; `from hermes_voip.transport import WssSipTransport` raises `AttributeError`. (src/hermes_voip/transport/__init__.py)
- [ ] **[medium] observability** — Outbound call lifecycle has no structured log events; only `call_loop_started` is emitted for outbound — `outbound_invite_sent`, `outbound_call_connected`, and `outbound_call_failed` are absent, blocking outbound SLO queries. (src/hermes_voip/adapter.py)
- [ ] **[medium] observability** — `_teardown_call` reads `CallQuality` for the `rtcp_call_quality` event but performs no one-way/no-audio inference and emits no `one_way_audio` or `media_degraded` structured event. (src/hermes_voip/adapter.py, src/hermes_voip/media/engine.py)
- [x] (#327) **[high] ux** — DTMF input does not count as caller activity for the no-input watchdog; `feed_dtmf` / `_deliver_dtmf_group` never set `_caller_active_in_window`, so a keypad-only or hearing-impaired caller is reprompted then hung up on mid-navigation. (src/hermes_voip/media/call_loop.py)
- [ ] **[medium] ux** — `hang_up_call` sends BYE immediately with no TTS drain; a farewell spoken in the same turn is clipped mid-word, unlike the loop-initiated goodbye which flushes before `run()` returns. (src/hermes_voip/adapter.py, src/hermes_voip/voip_tools.py)
- [x] (#333) **[high] docs** — runbook-0013 §6 and runbook-0014 §Error-handling falsely state provider-error-spoken-verbatim is unfixed ("NOT FIXED", "known leak — Task #26"); ADR-0063 shipped the fix in `provider_error.py` + adapter.py:1429-1448. (docs/runbooks/0013-voip-incident-oncall.md, docs/runbooks/0014-voip-slo-metrics.md) — verified correct on main; fixed in commit de2e4f5 (both runbooks now reference ADR-0063 shipped intercept)
- [x] (#333) **[high] docs** — runbook-0013 §5 TTS fallback recovery step instructs `HERMES_VOIP_TTS_FALLBACK=sherpa_kokoro` (underscore) but config.py validates only `sherpa-kokoro` (hyphen); following the runbook during an outage triggers `ConfigError` at startup. (docs/runbooks/0013-voip-incident-oncall.md) — verified correct on main (runbook reads `sherpa-kokoro` with hyphen); fixed in commit de2e4f5 (PR #333)
- [x] (#365) **[medium] docs** — runbook-0007 "The knobs" table omits `HERMES_VOIP_RING_TIMEOUT_SECS` and the `failure_outcome` structured result added by ADR-0086 (commits 881b6a9, 786b2c6, 2026-06-27); rule 42 violation. (docs/runbooks/0007-voip-outbound-calling.md) — shipped #365: added the "The `place_call` structured result contract (ADR-0086)" section documenting `failure_outcome`'s four `PlaceCallOutcome` values, their SIP triggers, and the rule-34 no-reason-phrase-echo guarantee (verified against `PlaceCallOutcome`/`place_call_handler` in `voip_tools.py`); `HERMES_VOIP_RING_TIMEOUT_SECS` was already in the knob table from a prior wave.
- [x] (#332) **[medium] feature** — `HERMES_VOIP_DENY_MODE=decline` (polite spoken-decline before BYE, ADR-0020 §6) was designed as Phase 2 but never built; `grep 'DENY_MODE\|deny_mode' src/` returns zero hits; the ADR config table entry is aspirational (rule 27). (src/hermes_voip/adapter.py, src/hermes_voip/config.py, docs/adr/0020-voip-caller-modes.md)
- [ ] **[low] docs** — Rename test `test_sdes_rejects_non_ascii_cname` to an octet-based name; its café×100 input is now rejected by the 255-UTF-8-octet cap, not by ASCII-encodability (post #321). (tests/test_rtcp.py)

## Wave-9 gap-review (discovered 2026-06-28)

Two candidates were already/partially tracked and were not duplicated here: `_granted_expires` multi-Contact fallback (existing registration item) and provider seam `__all__` (partially covered by the existing provider export residual). Newly discovered local-only items follow.

- [x] (#338) **[low] robustness** — RTCP BYE reason should decode UTF-8 strictly and raise `RtcpError` on malformed bytes, matching SDES CNAME handling and rule 37. (`src/hermes_voip/rtcp.py`)
- [x] (#400 verified-DONE 2026-07-03: already handled — Opus decode is caught+concealed (`engine.py`, ADR-0056/0081), G.711/G.722 decode is pure per-byte arithmetic that cannot raise, RFC-4733 telephone-event decode is `except ValueError`-guarded; proven by `test_opus_corrupt_payload_is_concealed_and_call_survives`) **[medium] robustness** — Drop malformed RTP codec payloads with a log instead of letting codec decode exceptions tear down the whole call. (`src/hermes_voip/media/engine.py`, `src/hermes_voip/media/call_loop.py`)
- [x] (#358) **[medium] robustness** — Fail malformed final-INVITE response CSeqs loudly instead of silently skipping ACK/transaction cleanup. (`src/hermes_voip/transport/connection.py`, `src/hermes_voip/transport/ws_connection.py`) — shipped #358 (`_auto_ack_non_2xx` now logs a WARNING on an unparseable CSeq in both transports instead of silently skipping ACK/transaction cleanup).
- [x] (#402) **[medium] security** — Move outbound dial allowlist values out of inline env into a file-backed setting so real numbers/SIP URIs are not stored in shell-visible env. (`src/hermes_voip/outbound_allow.py`, `docs/runbooks/0007-voip-outbound-calling.md`, `docs/adr/0099-outbound-allowlist-file-backed-source.md`) — SHIPPED via #402 (ADR-0099, extends ADR-0029). Added OPTIONAL `HERMES_VOIP_OUTBOUND_ALLOW_FILE` (a path to a gitignored plain list of the same entries, comma/newline separated) whose entries are **UNIONed** with the inline `HERMES_VOIP_OUTBOUND_ALLOW`; a missing/unreadable file raises `ConfigError` (fail-closed, rule 37, matching the caller-modes list-file loaders). The governing ADR is **ADR-0029** (not ADR-0020 as the item guessed) — ADR-0099 extends it and its "Alternatives considered" file-path row was reconciled; runbook 0007 documents the file knob, format, union rule, and rollback. `adapter.py` untouched; mypy `--strict` + ruff clean.
- [x] (#339) **[low] packaging** — Licence-check all declared optional runtime extras, not just default deps, so a bad extra licence fails CI. (`.github/workflows/supply-chain.yml`, `docs/runbooks/0003-supply-chain-audit.md`, `pyproject.toml`)
- [x] (#364) **[medium] test** — Reconnect test should assert the registration manager reattaches active dialogs, not only the new transport sink. (`tests/test_adapter_reconnect.py`, `src/hermes_voip/adapter.py`) — shipped #364 (`test_active_call_dialog_reattached_to_manager_on_reconnect`).
- [x] (#364) **[medium] test** — Reconnect success test should assert degraded-health state clears after a successful reconnect. (`tests/test_adapter_reconnect.py`, `src/hermes_voip/adapter.py`) — shipped #364 (`test_degraded_health_clears_after_successful_reconnect`).
- [x] (#340) **[medium] test** — Add a multi-file manifest licence test where a later file fails, proving validation checks every pinned file. (`tests/providers/test_build.py`, `src/hermes_voip/manifest.py`)
- [x] (#341) **[medium] docs** — Reconcile `hermes plugins list` version expectations so README/runbook agree on whether healthy output shows the shipped plugin version or `0.0.0`. (`README.md`, `docs/runbooks/0011-voip-enable-plugin.md`, `packaging/hermes-plugins/hermes-voip/plugin.yaml`, `src/hermes_voip/plugin.yaml`)
- [ ] **[low] docs** — Refresh stale file-line anchors in the IMPLEMENTATION-PLAN evidence table so present-tense proof points land on current definitions. (`docs/plan/IMPLEMENTATION-PLAN.md`)
- [ ] **[low] api** — Trim `hermes_voip.providers.build.__all__` to the real builder API and stop exporting internal default wiring/maps/constants. (`src/hermes_voip/providers/build.py`, `src/hermes_voip/manifest.py`)
- [x] (#356) **[high] efficiency** — Gate G.722 hot-path CPU cost with a benchmark/budget or stop preferring it by default while the pure-Python codec remains expensive. (`src/hermes_voip/adapter.py`, `src/hermes_voip/media/engine.py`, `src/hermes_voip/media/g722.py`) — shipped #356 (ADR-0094 + combined encode/decode CPU budget gate).
- [ ] **[high] efficiency** — AEC hot-path CPU budget: MEASURED, premise falsified — this needs a design decision (a new ADR), not a budget-constant add. The NLMS AEC at the shipped default (`aec_filter_ms=64` → 512 taps, `_AEC_MAX_TAPS`) measures ~39.8 ms/frame (37–43 ms) at 16 kHz while the filter is ADAPTING — the common case whenever the agent is speaking and its own echo is returning — roughly 2x the 20 ms ptime; even 8 kHz adaptation-active measures ~21.2 ms/frame, also over budget. The RX path is SYNCHRONOUS: `_inbound_gen` (`engine.py:1582`) calls `_decode` (`engine.py:1774`) then `_cancel_echo` (`engine.py:1785`) inline in the same async media coroutine with no thread/executor offload, so combined 16 kHz RX cost is ~42 ms/frame. A `< 20 ms` budget gate as originally scoped therefore CANNOT pass without a design change — gating on it as-is would force a choice between failing CI and disabling AEC outright. The `engine.py:267` "~13.8 ms at 16 kHz" comment (and ADR-0033's matching "Consequences" figure) measured a FROZEN/non-adapting filter and did not account for the NLMS update loop, which materially raises the per-frame cost while adapting. Needs a Proposed ADR weighing: (a) shorten the default filter so cancel+decode fits under ptime — but ADR-0033's design history (commit `ff73953`) found a 16 ms window leaves a ~40 ms-delayed broadband echo essentially uncancelled, so this trades echo-cancellation reach for CPU; (b) disable AEC by default at 16 kHz; (c) offload `_cancel_echo` to a worker thread; (d) a block/partitioned frequency-domain AEC (ADR-0033 rejected FDAF for adding algorithmic latency — would need re-litigating against the no-added-latency requirement); (e) accept a numpy dependency in the `media`-extra path (`aec.py` currently forbids this by design — a rule 22/35 trade-off). (`src/hermes_voip/media/engine.py`, `src/hermes_voip/media/aec.py`, `src/hermes_voip/config.py`) **Design record added 2026-07-02:** ADR-0095 (`docs/adr/0095-aec-realtime-cpu-budget.md`, Status: Proposed — Deferred) now records this measurement, the synchronous-RX-path arithmetic, and a scored (a)-(e) option set with a recommendation (interim: disable AEC by default at 16 kHz; durable: thread-offload or block-FDAF, operator's call). This item stays open — no option is adopted yet, only the design record.
- [ ] **[medium] observability** — Emit structured SIP transport loss/retry/recovery events so uptime and flap windows are queryable without regex. (`src/hermes_voip/adapter.py`, `docs/runbooks/0014-voip-slo-metrics.md`)
- [ ] **[medium] ux** — Add language-keyed default polite-decline phrases so `HERMES_VOIP_DENY_MODE=decline` is not English-only by default. (`src/hermes_voip/config.py`, `src/hermes_voip/adapter.py`)
- [ ] **[low] ux** — Sanitize custom polite-decline text before TTS so markdown/URLs/emoji are not spoken raw in deny-mode decline. (`src/hermes_voip/adapter.py`, `src/hermes_voip/media/call_loop.py`)
- [x] (#342) **[medium] operability** — Expose `HERMES_VOIP_CARTESIA_API_KEY` in the plugin manifest so supported Cartesia configuration is discoverable and verifiable. (`src/hermes_voip/plugin.yaml`, `src/hermes_voip/config.py`)
- [ ] **[low] operability** — Document the `HERMES_VOIP_TEST_TONE` diagnostic knob in manifest/runbooks for no-audio incident triage. (`src/hermes_voip/plugin.yaml`, `docs/runbooks/0002-voip-live-validation.md`, `docs/runbooks/0013-voip-incident-oncall.md`)
- [x] (#363) **[medium] operability** — Reject inert `HERMES_VOIP_DUPLEX_MODE=full` at config load so operators cannot believe full-duplex is active when runtime ignores it. (`src/hermes_voip/config.py`) — shipped #363.
- [ ] **[medium] feature** — Deliver inbound `take-message` results to an operator-visible channel; today inbound capture reports success to the agent but not to the operator. (`src/hermes_voip/adapter.py`, `docs/adr/0047-bundled-call-skills.md`, `src/hermes_voip/skills/take-message/SKILL.md`)
- [ ] **[medium] feature** — Surface terminal REFER/NOTIFY transfer outcome to the agent/operator instead of treating REFER acceptance as final success. (`src/hermes_voip/call.py`, `src/hermes_voip/adapter.py`, `src/hermes_voip/voip_tools.py`)

## Wave-10 gap-review items (discovered 2026-06-28)

Privacy/vendor-identifier scrub of operator gateway identifiers from tracked files is shipping as PR #343 (currently in CI).

### Security

- [x] (#346) **[high] security** (`docs/adr/0042-*`, `docs/runbooks/0009-*`, `tests/test_no_vendor_identifiers.py`) — Investigated the remaining SIP realm label as sensitive because it appeared in live-gateway ADR/runbook evidence, scrubbed the tracked occurrences to a sanctioned fake realm, and extended the tracked-tree guard to fail on reintroduction without embedding the contiguous live token.
- [x] (#346) **[high] security/ops** (accepted 2026-06-28) (repo-wide) — Git HISTORY still contains the operator gateway identifiers that PR #343 scrubbed from the working tree. Operator decision: retain public history as known-disclosed; no history rewrite/purge.

### Robustness / CI

- [x] (#347) **[medium] robustness/ci** (`.github/workflows/supply-chain.yml`) — The optional-extras licence gate can silently FALSE-GREEN: if `uv export` ever drops or renames the `# via hermes-voip` provenance comment its parser relies on, the package list parses empty, the `[ -s ]` guard takes the else branch, and the gate passes vacuously while extras are actually declared. Add a loud-fail guard: if optional extras are declared in `pyproject.toml` but the parsed package list is empty, FAIL (rule 37), don't skip. — shipped #347 (`tools/check_optional_extras_guard.py` + supply-chain.yml delegation)

### Security / test

- [ ] **[low] security/test** (`tests/test_no_vendor_identifiers.py`) — The product-word sub-check in the vendor-identifier guard is case-sensitive by necessity (the token is an English homograph), so it only matches the brand's canonical all-caps form; a future reintroduction of the brand in non-canonical case could slip past that one sub-check (the distinctive vendor/model/abbreviation fragments remain case-insensitive). Consider a context-aware check.

## Review follow-ups (PR #348 opus review, 2026-06-28)

- [x] (#353) **[medium] security/robustness** (`src/hermes_voip/manager.py`) — `_on_registration_failed()` passes the raw `error` to the operator-supplied `_on_error(extension, error)` callback; `RegistrationRejectedError.__str__` embeds the registrar-controlled free-text `reason`. If an operator wires that callback to a logger or telemetry sink, the registrar-influenced `reason` (plus the extension passed alongside) could reach logs through a path the new structured-log secret-safety guard (PR #348) does NOT cover. Add a docstring warning on the `_on_error` callback contract (the `error` argument may carry registrar-controlled text; callers must not log it verbatim alongside sensitive context) and/or pass a sanitized error category instead of the raw exception. Discovered in the PR #348 opus review. — shipped #353 (`RegistrationRejectedError` safe-by-default rendering, `.reason` retained as explicit untrusted opt-in, `RegistrationFailureCategory`, ADR-0093).

## Wave-11 gap-review (discovered 2026-07-02, docs-reconcile lane)

- [ ] **[low] robustness** — The malformed-final-INVITE-CSeq WARNING (#358, `_auto_ack_non_2xx` in `transport/connection.py` + `transport/ws_connection.py`) is not rate-limited or deduplicated; both log call sites fire unconditionally per malformed response, so a hostile or badly-broken peer streaming unparseable CSeqs could amplify log volume without bound. Add rate-limiting/dedup. (`src/hermes_voip/transport/connection.py`, `src/hermes_voip/transport/ws_connection.py`)
- [ ] **[low] test** — The REFUSE no-input-watchdog test (#359, `tests/test_call_loop.py::test_no_input_watchdog_resets_on_refuse_verdict`) is timing-coupled: it drives `_SteppedSleep` and polls with fixed-count `for _ in range(20): await asyncio.sleep(0)` loops (one with no early-exit condition at all) rather than waiting on an explicit event/condition. Make it event-driven so it cannot silently false-green if `_screen_and_deliver`'s async structure changes (e.g. gains or loses a yield point). (`tests/test_call_loop.py`)
- [x] (#362) **[medium] test/security** — `tests/test_manager.py::_assert_failure_log_is_secret_safe` substring-scans a serialized `LogRecord` for secret sentinels (including short digit strings like `"1000"`/`"1001"`). It already excludes `relativeCreated`/`created`/`msecs`/`thread`/`process` from the scan for exactly this reason (documented in-line as a fixed flake source), but deliberately still scans `taskName` because asyncio task names are application-set and can carry a dial target. `taskName` defaults to the framework's process-global auto-numbered `"Task-N"` for every unnamed task, and `RegistrationManager` creates three unnamed tasks (`src/hermes_voip/manager.py:549`, `:572`, `:658`, none pass `name=`) — so once the process-wide task counter climbs past 1000/1001 in a long test session, a coincidental `"Task-1000"`/`"Task-1001"` substring match flakes the assertion. Replace the substring scan with a deterministic check: assert secret VALUES are absent from the code-controlled fields (`msg`/`args`/`message`/the attached `extra`) via exact-field or word-boundary matching, closing the whole numeric-collision class without narrowing which fields are scanned. (`tests/test_manager.py`, `src/hermes_voip/manager.py`) — shipped #362: `_assert_failure_log_is_secret_safe` (`tests/test_manager.py:162`) now checks code-controlled fields deterministically instead of a raw substring scan, closing the `Task-1000`/`Task-1001` collision class.
- [ ] **[low] ux/observability** — A wildcard `HERMES_VOIP_OUTBOUND_RESULT_CHANNEL` (e.g. `slack:*`, issue #355/#360) whose pattern does not match the triggering origin resolves to `None` in `resolve_result_channel`, and the caller (`adapter.py::_report_to_fallback_channel`) logs a generic `"call %s ended (outbound, no origin, no result channel): %s"` INFO line — the same message used when no channel is configured at all, even though one WAS configured and simply didn't match. This silently suppresses origin delivery and actively mislabels why. Log the mismatch distinctly (e.g. include the configured pattern and the origin it failed to match) so operators can see why a result wasn't delivered instead of concluding no channel was set. (`src/hermes_voip/outbound_allow.py`, `src/hermes_voip/adapter.py`)

## Wave-12 gap-review (discovered 2026-07-02, voip_tools.py tool-abuse surface)

Full read of `voip_tools.py` end-to-end plus its call sites (`adapter.py`, `media/engine.py`,
`call.py`, `tools.py`) and tests, focused on the agent's tool-calling surface (authorization
binding, argument-smuggling, missing validation, exception-escape, rate-limiting). The tool
argument schemas expose no `call_id`/session-targeting parameter for any of the 10 tools — the
call is always resolved server-side via `_current_call_id()` — so no tool call can name an
arbitrary OTHER call/session directly; cross-call isolation itself depends on the external,
unvendored `gateway.session_context` module and is out of scope for this repo to fix or test.
`transfer_blind`'s unallowlisted destination is a deliberate, recorded decision (ADR-0031's
"Alternatives considered": the live DTMF confirmation IS the per-call authorization; a
prompt-injected target the operator confirms without scrutiny is an accepted residual risk) —
not a new finding. `open_entry`'s `name` argument and `report_call_result`'s `summary` argument
were checked and are clean (exact allow-list membership; agent-authored text relayed only to the
operator's own result channel). Three other findings from this same pass — `send_dtmf`'s
uncapped `digits` length sharing the TX mutex with the agent's own audio, `transfer_attended`'s
missing outer exception net (traced to `call.py:383`'s unguarded signalling send), and the lack
of a redial/repeat-call cooldown — converged independently with a peer review and are tracked
as dedicated fix tasks (#41 VT-1/VT-2/VT-3, #42 VT-4, #43) rather than duplicated here.

- [ ] **[low] docs/polish** — `CallControlTools` (`src/hermes_voip/tools.py:242`) is fully implemented and thoroughly tested (`tests/test_tools.py`, 30+ tests including a TOCTOU regression explicitly citing "codex HIGH") but has zero production call sites. `grep -rn "CallControlTools(" src/` has no hits; `plugin.py`'s actual tool-registration entrypoint calls `register_voip_tools` (`voip_tools.py`), never `tools.py`'s `CallControlTools`. Other files (`adapter.py`, `dtmf_confirm.py`) cite it only in docstrings as a design-parity comparison ("identical to `CallControlTools._irreversible`", "mirrors `CallControlTools.list_registrations`"). Verified NOT a live security gap: the actual production path (`adapter.py::transfer_blind_on_call`, L5906-6013) independently re-implements the same TOCTOU protection `CallControlTools` pioneered (re-checks the guard both before and after the confirmation await). This is maintenance dead-weight and a review hazard (a reviewer could mistake `CallControlTools` for the live authorization surface and audit/patch the wrong implementation) rather than a live vulnerability — distinct from task #42's `call_loop.py` duplicate-`gate_voip_tool` finding (different file, different function). Either remove `CallControlTools`/its Protocols/tests as superseded by `voip_tools.py`'s handler-function design, or — if intentionally kept as a reference/alternate implementation — say so explicitly in its module docstring and point the cross-file "identical to"/"mirrors" comments at the live code instead.
- [ ] **[low] test** — `_voip_tool_names()` vs `_VOIP_TOOLS` completeness has no automated full-set-equality test. `tests/test_voip_tools.py::test_gate_owns_the_control_tools` spot-checks only 3 of the 10 tool names are members of `_voip_tool_names()`, not a full-set equality against `_VOIP_TOOLS`. Currently consistent (manually verified 10/10) but structurally fragile — a future tool addition that's registered but not added to the gate's name-set would not be caught. Assert `vt._voip_tool_names() == {spec.name for spec in vt._VOIP_TOOLS}` (or equivalent) so the two can never silently drift. (`src/hermes_voip/voip_tools.py`, `tests/test_voip_tools.py`)
- [ ] **[medium] security** — No cooldown / rate-limit on repeated IRREVERSIBLE tool calls (`place_call` / `transfer_blind` / `transfer_attended` / `send_dtmf`). `_outbound_extensions` (`adapter.py:1014`, checked at `:1708`, 503 "already in progress", discarded on completion at `:1725`) only blocks a *concurrent* same-extension dial (a re-entrancy guard), and the attended-consult single-slot guard (`adapter.py:6050`) only blocks a second *concurrent* consult — neither limits sequential redial rate nor repeated transfer/DTMF. Once a session holds level-3 / non-degraded privilege, a confused-deputy loop (a persistent prompt injection driving the model) can redial, transfer, and DTMF-spam bounded only by the static outbound allowlist + the per-transfer DTMF-confirm gate, not by attempt count or elapsed time. Design questions to resolve in an ADR BEFORE implementing (rule 40 — undecided agent-visible-behaviour policy): per-session sliding-window vs fixed cooldown; interaction with `degraded` (does tripping a throttle degrade the session, or just refuse the one call?); and it MUST NOT throttle legitimate rapid redials (e.g. immediately calling back a caller who just dropped) or a normal consult→complete/cancel sequence. Do not add throttle code until the ADR is accepted. (`src/hermes_voip/voip_tools.py`, `src/hermes_voip/adapter.py`, new `docs/adr/`) **Design record added 2026-07-03:** ADR-0103 (`docs/adr/0103-irreversible-tool-rate-limit.md`, Status: **Proposed**) frames the decision — recommends option B (per-session sliding-window budget, defaults N=6/60s burst 3, refuse-not-degrade, structured `irreversible_tool_throttled` event) with three open questions for the operator to accept/adjust. Item stays open until the ADR is accepted and implemented.

## Wave-13 gap-review (discovered 2026-07-02, message.py SIP-parser ingress surface)

Full read of `src/hermes_voip/message.py` (`SipRequest.parse` / `SipResponse.parse` /
`_parse_headers` / `build_request` / `build_response`) plus its transport catchers, focused on
the ADR-0081 exception-escape axis (one malformed inbound message must not DoS unrelated calls)
and the ingress→egress data flow. The exception-escape axis is CLEAN: every op in both parsers
raises only `ValueError` on hostile `str` (empirically probed, and now locked by a 35-input
adversarial table asserting `parse` either succeeds or raises `ValueError`, never another
exception type). Three LOW findings were FIXED in
this same lane (shipped with the tests): the status-line regex `\d{3}` → `[0-9]{3}` so non-ASCII
decimal digits are no longer folded (LOW-1); a parse-side status range check 100..699 matching
`build_response` (LOW-2); and length-only parse-error messages replacing the `{...!r}` raw-wire
embeds (LOW-4). Two residual, cross-file items follow.

- [ ] **[low] security/robustness** — `message.py` `parse` is permissive about non-CRLF control
  characters: the request-URI `\S+`, the reason `(.*)`, and header values all retain interior C0
  controls / bare CR / NUL / DEL. This is NOT injectable within `message.py` itself — both
  builders call `_reject_controls` (`contains_control`) on every field before it reaches the wire.
  The residual is defense-in-depth: any OTHER egress path that formats a PARSED header value or
  request-URI into wire text WITHOUT a control-char gate would be injectable. Needs a cross-file
  egress audit: `refer.py` is verified clean (it references `contains_control`), but `dialog.py`,
  `incall.py`, and `session_timer.py` reference neither `contains_control` nor `_reject_controls`,
  so the audit must confirm whether any of them serialize a parsed value into wire text unguarded.
  Tracked as task #47. (`src/hermes_voip/message.py`, `src/hermes_voip/refer.py`, `src/hermes_voip/dialog.py`, `src/hermes_voip/incall.py`, `src/hermes_voip/session_timer.py`)
- [ ] **[low] robustness** — The outbound `send()` / transaction paths re-parse OUR OWN freshly
  built wire text unguarded (`transport/connection.py:349,390`, `transport/ws_connection.py:333,349`,
  `transport/transaction.py:64`, `adapter.py:2059,2789`). Low risk — a parse failure there is our
  builder's bug and propagates out of the builder, not out of the inbound read loop, so it is not
  an ADR-0081 connection-DoS — but worth confirming no caller logs `str(exc)` of these parses (ties
  to the LOW-4 redaction discipline now applied at the parser; the messages are length-only, so
  even a stray `str(exc)` log no longer leaks wire content). (`src/hermes_voip/transport/connection.py`, `src/hermes_voip/transport/ws_connection.py`, `src/hermes_voip/transport/transaction.py`, `src/hermes_voip/adapter.py`)

## Wave-14 gap-review (sdp.py) (discovered 2026-07-02)

Full read of `src/hermes_voip/sdp.py` (SDES `a=crypto` negotiation + keying, ICE candidate parsing, offer/answer build). Three findings were fixed in the same lane under strict TDD and shipped together: **F1 (HIGH)** the SDES wire-suite vs installed-key divergence — `_negotiate_answer_crypto` selected the STRONGEST offered suite for the wire (anti-downgrade) while every SRTP/SRTCP session was keyed from `crypto_attrs[0]` in offer order, so a spec-compliant weak-first offer ran SHA1_32 (4-byte tag) under a SHA1_80 (10-byte tag) wire answer: no audio in either direction and the anti-downgrade control silently defeated (fixed by ordering `crypto_attrs` strongest-first so `crypto_attrs[0]` is the accepted suite by construction); **F2 (MEDIUM)** a Unicode-digit crypto tag — `CryptoAttribute.parse` / `_coerce_crypto` guarded the tag with bare `str.isdigit()` (True for U+00B2) then `int()`, raising a bare `ValueError` that escaped `except SdpError` (fixed with the house `isascii()+isdecimal()` guard); **F3 (MEDIUM)** a non-numeric ICE `rport` — an unguarded `int()` bypassed the `a=candidate` `suppress(SdpError)` and failed the whole offer parse (fixed by wrapping it in the same `ValueError->SdpError` guard as the mandatory fields). The two low-severity residuals below are filed, not fixed.

- [ ] **[low] egress/robustness** — The SDES/plain answer echoes the offer-derived `codec.fmtp` verbatim into its own `a=fmtp` line (`_build_audio_body`, `src/hermes_voip/sdp.py:1512`; the same echo is in `_rtpmap_lines:1530`). `codec.encoding` is intersection-safe (only codecs in OUR `supported` menu are emitted), but `fmtp` is free-form offer text passed through unfiltered. Inbound line-splitting already strips CR/LF as delimiters, so only a *lone* CR embedded mid-field could survive into the field value, and a compliant peer re-absorbs a lone CR on its own inbound split (`sdp.py:36-37`) — so this is not an injection today. But the module docstring reasons explicitly about lone-CR absorption on the INBOUND path and is silent about echoing a lone-CR field back OUTBOUND (rule-27-adjacent: the safety argument is asymmetric). Either sanitise `fmtp` (reject/strip control chars) before echo, or extend the docstring's lone-CR reasoning to cover the outbound echo so the invariant is stated where the risk is. (`src/hermes_voip/sdp.py`)
- [ ] **[low] robustness** — Two unvalidated numeric SDP fields (neither crashes; both benign today): (a) `m=audio` payload-type numbers are parsed with no 0–127 bound (`_AudioAccumulator.set_media_line`, `src/hermes_voip/sdp.py:859`: `[int(pt) for pt in fields[3:]]`) — an out-of-range PT is simply never matched to a codec and dropped, but it is not rejected the way an out-of-range `m=audio` port is (`:846`); (b) `a=maxptime` is stored as a bare `int(rest.strip())` with no positive-value check (`src/hermes_voip/sdp.py:912`), unlike `a=ptime` which is validated `> 0` (`:905-907`). A zero/negative `maxptime` would be a nonsensical upper bound for `negotiate_ptime`; validate it `> 0` (mirror `a=ptime`) or record why it is left lenient. (`src/hermes_voip/sdp.py`)

## Release hygiene (discovered 2026-07-03)

- [ ] **[low] release-hygiene / test-toil** — The per-release version ratchet
  `tests/test_version_0XY.py` hard-codes the expected version and must be renamed +
  rewritten every release (0.2.0 shipped it as `test_version_020.py`, renamed from the
  stale `test_version_012.py` that was still pinning 0.1.3). Generalise it so the
  expected version is DERIVED from `pyproject.toml [project].version`: keep the
  CHANGELOG-ratchet assertions (a `## [<version>]` section that is non-empty, a
  `[Unreleased]` compare link to `v<version>...HEAD`, and a `[<version>]:` compare
  link) but source `<version>` from pyproject, so the file auto-follows the bump under
  one stable name (e.g. `test_release_changelog.py`). The absolute manifest pins it
  currently duplicates are already covered relatively by `tests/test_plugin_manifest.py`;
  the release runbook (0019 step 4) then drops the rename step.
  (`tests/test_version_020.py`, `docs/runbooks/0019-release-process.md`)

## Test / CI robustness (discovered 2026-07-03)

- [x] (#401) **[medium] test/ci** — No global pytest timeout: a flaky async/networking test can
  hang the local gate AND CI indefinitely with no diagnostic. Observed 2026-07-03 — a
  lane's `uv run pytest` (extras synced, so the full e2e/adapter socket suite ran) sat in
  asyncio `do_epoll_wait` at ~1% CPU with no per-test bound; `pyproject.toml`
  `[tool.pytest.ini_options]` sets no timeout, so the run blocked until an external kill.
  Add a fail-safe bound: pytest's built-in `faulthandler_timeout` (no new dep — dumps ALL
  thread stacks and names the hanging test) and/or the `pytest-timeout` dev-dep with a
  generous per-test cap, wired into `pyproject.toml` and CI. Then use the faulthandler dump
  to identify and fix the specific flaky socket/await test. Rule 33 (deterministic builds);
  also prevents the slow pre-push-hook full-suite from reading as a hang.
  (`pyproject.toml`, `docs/stack.md`)

## Gap-review 2026-07-03 (media / stt / tts / auth — outside the ADR-0081 signaling campaign)

A verified REPLENISH gap-review (7 read-only sub-reviewers; findings independently
spot-checked, incl. a from-scratch repro of the TTS deadlock). All 8 findings — the 3 HIGH
robustness bugs plus the 4 medium + 1 low — shipped and merged (see the PR refs on each
item). Confirmed-clean dimensions (no action): call_loop/rtp/rtcp, the injection guard (no
fail-open under adversarial Unicode), srtp/srtcp, digest/keepalive/provider_error/caller_modes.

- [x] (#404) **[high] robustness** — TTS barge-in deadlocked the WHOLE asyncio event loop
  (froze every concurrent call): `PcmFrameStream.cancel()` on the loop thread called
  `response.close()` on an `HTTPResponse` whose `read()` was parked on a worker thread —
  CPython's `close()` cannot interrupt the read and blocks on the buffer lock it holds.
  Fixed by arming the barge-in with `sock.shutdown(SHUT_RDWR)` on the underlying socket.
  (`src/hermes_voip/tts/elevenlabs.py`, `src/hermes_voip/tts/_stream.py`)
- [x] (#405) **[high] robustness** — Unbounded per-SSRC RTCP `_reception` growth: a UDP
  flood on the negotiated RTP port (no source check) could push it past 31 SSRCs, making
  `build_rtcp_report` raise `ValueError` at the RFC 3550 RC/SC ceiling — killing `run_rtcp`
  and, via `stop()`'s `suppress(CancelledError)`, stranding teardown. Fixed with an
  `OrderedDict` LRU cap at 31 + a `run_rtcp` `ValueError` backstop + `stop()`
  non-`CancelledError` robustness (ADR-0098). (`src/hermes_voip/media/engine.py`)
- [x] (#403) **[high] robustness** — Deepgram Flux BINARY websocket frame raised
  `UnicodeDecodeError` (a `ValueError` sibling of, but not caught by,
  `except json.JSONDecodeError`), killing the call's STT stream on one frame. Fixed by
  broadening the guard + widening the socket types to `bytes | str`. Follow-on to #174.
  (`src/hermes_voip/stt/deepgram.py`)
- [x] (#408) **[medium] correctness** — `dialog.py:303-312` `_uri_and_tag` extracts the tag with
  `value.split(">", 1)[1]` (first literal `>`) instead of reusing the `_ANGLE_ADDR` regex;
  a quoted display-name containing a literal `>` (RFC 3261 §25.1 permits it) desyncs the
  tag search and raises `DialogError` on a fully-valid tagged header — rejecting valid calls
  in BOTH directions (live-verified). Fix: reuse `_ANGLE_ADDR.search(value)`.
  (`src/hermes_voip/dialog.py`)
- [x] (#409) **[medium] robustness** — `voip_tools.py:865-877` `_current_call_id` catches only
  `ImportError` on the `gateway.session_context` import; the `get_session_env(...)` call
  itself is unguarded, one function from its fail-closed sibling
  `_proactive_place_call_allowed` (which wraps the identical pattern in
  `except Exception: return False`). `_current_call_id` promises `None`, not a raise, and
  feeds `voip_pre_tool_call` (the privilege gate). Fix: wrap the body in
  `except Exception: return None`. (`src/hermes_voip/voip_tools.py`)
- [x] (#410) **[medium] robustness/security** — `media/sip_dtls_session.py:452/506` outbound DTLS
  handshake: a fatal alert re-raises a bare `OpenSSL.SSL.Error` (not `RuntimeError`), so on
  the outbound/UAC path it propagates through `place_call()` and BYPASSES
  `place_call_handler`'s dedicated `except RuntimeError` redaction branch (whose comment
  notes messages there can embed gateway connection details), hitting the generic handler
  that echoes `str(exc)`. Fix: catch the fatal-alert error in `_pump_dtls_handshake` and
  re-raise as `RuntimeError`. Serialize with the next item (both touch `media/dtls.py`).
  (`src/hermes_voip/media/sip_dtls_session.py`, `src/hermes_voip/media/dtls.py`)
- [x] (#410) **[medium] robustness** — `media/dtls.py:729/783/829` `derive_srtp_sessions` /
  `derive_srtcp_sessions` / `derive_outbound_srtp_session` all do
  `_PROFILE_TO_SUITE.get(profile, _SUITE_80)` — silently DEFAULTING the SRTP suite when no
  DTLS-SRTP profile was negotiated, instead of failing closed like the same file's
  `selected_profile()` (which converts through an enum and raises `RuntimeError` for an
  unrecognized value). Fix: route all three through the fail-closed conversion.
  (`src/hermes_voip/media/dtls.py`)
- [x] (#407) **[low] correctness/docs** — `media/audio.py:236-240` `linear_fade_out` comment
  claims "round toward zero (int() truncation)" but `//` is floor division (rule 27); off by
  exactly 1 LSB on negative samples (no audible impact). Fix the comment or switch to true
  truncation. Bundle with an adjacent lane. (`src/hermes_voip/media/audio.py`)

## Gap-review 2026-07-03 (wave 2 — observability / operability, both shipped)

A second REPLENISH pass after the wave-1 robustness fixes above; two findings, both shipped
and merged this wave. The pass otherwise re-confirmed the dimensions above as clean.

- [x] (#415) **[medium] operability** — `plugin.validate_voip_config` (the Hermes enable
  gate) checked SIP/media env *shape* but never that the selected providers can be *built*:
  the config vocabulary accepted by `load_media_config` is a strict superset of the wired
  `build_providers` dispatch map (e.g. TTS `piper`/`cartesia`, guard `sidecar` are valid
  tokens with no factory), and a self-host provider with an unset/absent model dir also
  passed — so a misconfig surfaced only one step later inside `adapter.connect()`. Fix: a
  shallow, model-free `check_providers_buildable(config)` (reusing the SAME
  `DEFAULT_*_FACTORIES` maps, so membership cannot drift) called from `validate_voip_config`
  — rejects an unwired token or a missing/mis-pathed self-host model dir at the enable gate,
  never the SIP password (runbook `docs/runbooks/0011-voip-enable-plugin.md`).
  (`src/hermes_voip/providers/build.py`, `src/hermes_voip/plugin.py`)
- [x] (#412) **[low] observability** — `IceConnection`'s only log — the RFC 8445
  nominated-pair line — carried no structured `event` name and no call correlator, unlike
  the ADR-0075 convention (`sip_registration_established`/`rtcp_call_quality`), so it could
  not be tied to a call or grepped as a named event. Fix (purely additive): an optional
  `call_id` correlator (default `None` = zero change for existing callers) plus
  `extra={event: ice_pair_nominated, call_id, candidate_type}`, wired from both
  `WebRtcMediaSession` construction sites. (`src/hermes_voip/media/ice.py`,
  `src/hermes_voip/media/webrtc_session.py`, `src/hermes_voip/adapter.py`)
