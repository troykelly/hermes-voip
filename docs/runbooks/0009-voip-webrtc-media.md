# Runbook: VoIP WebRTC media plane (DTLS-SRTP + ICE + Opus)

**What it is.** The plugin answers an inbound WebRTC peer — a peer whose SDP offers the
`UDP/TLS/RTP/SAVPF` profile — as a co-equal to SIP-over-TLS: it negotiates **Opus** (48 kHz,
G.711 fallback), keys SRTP via a **DTLS-SRTP** handshake (RFC 5763/5764), runs **ICE** (RFC
8445) connectivity, and carries SRTP media over the ICE-selected pair. A plain `RTP/AVP` or
SDES `RTP/SAVP` offer is unaffected — it takes the SIP-over-TLS path exactly as before.

The WHY lives in **ADR-0032** (WebRTC media wiring + Opus) and **ADR-0016** (WebRTC transport
design). This runbook is the operational HOW.

> **Public repo.** No secrets here. Connection details (host/extension/password) live only in
> the gitignored `.env` / 1Password; the values below are fakes.

## What is wired vs deferred (read before relying on it)

| Capability | Status |
| --- | --- |
| Inbound WebRTC media (DTLS-SRTP + ICE + Opus) | **Wired** (ADR-0032) |
| Opus 48 kHz on the wire; G.711 fallback | **Wired** |
| ICE host candidates + STUN server-reflexive (srflx) | **Wired** |
| **TURN** relay candidates (operator-provided credentials) | **Wired** (ADR-0034); see "The TURN knob" — full live relay needs a real TURN server |
| ICE **consent freshness** (RFC 7675) — long calls behind NAT drop deterministically | **Wired** (ADR-0034; aioice-native, surfaced) |
| **Trickle** ICE — SDP primitives (`a=ice-options:trickle`, `a=end-of-candidates`) + half-trickle answer | **Wired** (ADR-0034) |
| **Outbound** WebRTC origination (our own offer) | **Deferred** — outbound runs over SIP-over-TLS |
| SIP **signalling over Secure-WebSocket** (`HERMES_SIP_TRANSPORT=wss`) | **Wired** (ADR-0038); see "SIP-over-WSS signalling" — full live validation needs the operator's WSS port + credential |
| Trickle ICE **in-dialog transport** (SIP INFO, RFC 8840 `trickle-ice-sdpfrag`) | **Not required** for our SIP/WebRTC targets (ADR-0034 §2 determination) — half-trickle is fully interoperable; we don't advertise the `trickle-ice` SIP option-tag so peers fall back to the full-candidate exchange we serve |
| WebRTC **video** | **Deferred** (ADR-0018) |
| **Live** validation against a real WebRTC client | **Pending** the operator's redeploy |

## Prerequisites

1. **The `webrtc` extra** (`uv sync --extra webrtc` / `--all-extras`): `aioice` (ICE),
   `pyopenssl` (DTLS-SRTP), `opuslib` (Opus), `websockets` (SIP-over-WSS signalling —
   required when `HERMES_SIP_TRANSPORT=wss`).
2. **The system `libopus` shared library** — `opuslib` is a pure-Python ctypes wrapper that
   `dlopen`s `libopus` at runtime (it bundles no native code):

   ```
   sudo apt-get update && sudo apt-get install -y libopus0
   ```

   The devcontainer image already ships `libopus0`; the `webrtc` + `hermes-contract` CI jobs
   install it explicitly. **Without `libopus`**, a WebRTC/Opus call fails the call setup with
   a clear `ImportError` (the media engine never answers dead) — SIP-over-TLS (G.711/G.722)
   keeps working. Verify the library resolves:

   ```
   uv run python -c "import ctypes.util; print(ctypes.util.find_library('opus'))"
   # -> libopus.so.0
   ```

## SIP-over-WSS signalling (`HERMES_SIP_TRANSPORT=wss` — ADR-0038)

To register and receive calls over a gateway's **Secure-WebSocket** edge (the WebRTC
signalling transport, RFC 7118) instead of SIP-over-TLS, select the WSS transport. The
adapter then builds `WssSipTransport` (subprotocol `sip`, WSS Via, `transport=ws`
Outbound Contact) instead of `SipOverTlsTransport`; a SAVPF/DTLS/ICE INVITE arriving over
it flows into the same WebRTC media path above.

| Item | Value |
| --- | --- |
| Transport selector | `HERMES_SIP_TRANSPORT=wss` (default `tls`) |
| Endpoint | `wss://${HERMES_SIP_HOST}:${HERMES_SIP_PORT}${HERMES_SIP_WS_PATH}` |
| WS port | `HERMES_SIP_PORT` (default `443` for `wss`; set the gateway's real WSS port) |
| WS path | `HERMES_SIP_WS_PATH` (default `/ws`) |
| WS credential | `HERMES_SIP_WS_PASSWORD` — the SEPARATE WSS digest password; **unset ⇒ falls back to `HERMES_SIP_PASSWORD`** |
| Read by | `hermes_voip.config.load_gateway_config` → `GatewayConfig.transport / ws_path / ws_password` |
| Applied at | `adapter._establish()` selects the transport class; `registration_config()` applies the WS password override on `wss` |

- **A WebRTC/WSS gateway edge is commonly a different port + a different digest password**
  than the SIP-TLS edge. Set `HERMES_SIP_PORT` to the WSS port and, if the WSS endpoint
  uses its own credential, `HERMES_SIP_WS_PASSWORD`. The password is a **secret** —
  `repr`-suppressed on `GatewayConfig` (never logged); keep the value in `.env` /
  1Password, never a tracked file. If the WSS edge shares the SIP password, leave
  `HERMES_SIP_WS_PASSWORD` unset (the documented fallback).
- **`wss://` verifies the gateway certificate** with the same TLS context the SIP-TLS
  transport uses (no `verify=False`); the SNI host is `HERMES_SIP_HOST` even when behind
  a numeric address.
- **One transport per process.** `HERMES_SIP_TRANSPORT` selects `tls` **or** `wss`; the
  plugin does not run both registration stacks at once.
- **Outbound is still SIP-over-TLS.** Selecting `wss` does not yet route the agent's
  `place_call` over the WebSocket (outbound WebRTC origination is deferred, ADR-0032 §5);
  inbound over WSS is wired end-to-end.
- **Live WSS validation is the operator step.** Unit tests prove transport selection +
  the WSS Contact/Via + the SAVPF-over-WSS → `is_webrtc` routing + the credential override
  (see "How to verify"). A **full live WSS REGISTER + call** needs the operator's real WSS
  **port + `HERMES_SIP_WS_PASSWORD`** in `.env` and a plugin restart, validated like the
  live-gateway path (runbook 0002). A prior live probe saw a `401` on the gateway's
  Secure-WebSocket REGISTER for the test extension; the plugin now emits the correct RFC
  7118 REGISTER, so the remaining variable is the gateway-side endpoint + credential.

## The STUN knob (default revised — ADR-0043)

| Item | Value |
| --- | --- |
| Env var | `HERMES_VOIP_ICE_STUN_URLS` |
| Type | comma-separated list of `stun:` URLs |
| Default (unset) | the public dual-stack list `DEFAULT_ICE_STUN_URLS` (Google + Cloudflare) |
| Disable | set it **empty** (`HERMES_VOIP_ICE_STUN_URLS=`) ⇒ host-only ICE |
| Read by | `hermes_voip.config.load_media_config` → `MediaConfig.ice_stun_urls` |
| Applied at | `WebRtcMediaSession(stun_urls=…)` → the ICE agent, per inbound WebRTC call |

- **Unset (default):** the public STUN list gathers a server-reflexive (srflx) candidate —
  including an **IPv6 srflx** on an IPv6-capable host — so a NAT'd deployment works out of
  the box. These are free, no-auth, stateless reflexive-echo servers (not a paid/SaaS or
  media-carrying dependency; TURN, which relays media, stays operator-provided — see below).
- **Override:** set your own `stun:` URLs (a value, no secret):

  ```
  HERMES_VOIP_ICE_STUN_URLS=stun:stun.example.test:3478,stun:stun2.example.test:3478
  ```

- **Disable (host-only):** set it to empty — works on a LAN / where the peer reaches a host
  candidate directly. A malformed URL fails loudly when the ICE agent is built.

## IPv6-first ICE (ADR-0043)

| Env var | Default | Effect |
| --- | --- | --- |
| `HERMES_VOIP_ICE_USE_IPV6` | `true` | Gather IPv6 ICE candidates (the **preferred** family). |
| `HERMES_VOIP_ICE_USE_IPV4` | `true` | Gather IPv4 ICE candidates (the **fallback** family). |

Both default on: the agent gathers IPv6 **and** IPv4 candidates, and the SDP answer lists
IPv6 candidates **first** (`WebRtcMediaSession.ice_candidates`). Set one to `false` for an
IPv6-only or IPv4-only deployment. On a host with global IPv6 that shares the gateway's
network, the IPv6 host candidate is directly reachable — no STUN needed.

> **Live media needs real UDP to the gateway.** A devcontainer behind Docker NAT cannot
> carry WebRTC media: UDP-over-IPv6 does not traverse Docker's NAT66, and UDP-over-IPv4
> yields only a hairpin-NAT address. Run the live WebRTC media validation on a host with
> working dual-stack UDP to the gateway (e.g. the same LAN/IPv6 `/48`). Signalling (WSS)
> works from anywhere with TCP; only the media plane needs the real UDP path.

## The DTLS answerer-role knob (ADR-0050, RFC 8842)

When the plugin answers an inbound WebRTC INVITE it picks its DTLS role from the offer's
`a=setup` and carries that role in the answer. The DTLS **client** sends the `ClientHello`;
the **server** waits for it. If both ends pick the server role the handshake deadlocks.

| Item | Value |
| --- | --- |
| Env var | `HERMES_VOIP_WEBRTC_DTLS_SETUP` |
| Type | one of `auto` / `active` / `passive` (case-insensitive) |
| Default (unset) | `auto` — for an `a=setup:actpass` offer we answer **`active`** (the DTLS client) per RFC 8842 §5.3 |
| `active` | explicit form of the default (active answerer for an actpass offer) |
| `passive` | force the **server** role for an actpass offer (only for a gateway that insists on being the DTLS client) |
| Read by | `hermes_voip.config.load_media_config` → `MediaConfig.webrtc_dtls_setup` |
| Applied at | `WebRtcMediaSession(answer_setup=…)` → `answer_setup_for_offer(forced=…)`, per inbound WebRTC call |

The knob applies **only** to an `a=setup:actpass` offer. A peer that pins itself `active`
(DTLS client) is **always** answered `passive`, and a peer that pins itself `passive` is
**always** answered `active` (RFC 5763 §5) — the knob cannot override a pinned role (that
would create two clients or two servers and deadlock). An unknown value is rejected at
config load.

**Why the default flipped (ADR-0050).** A real Asterisk/UCM-class gateway offers
`a=setup:actpass` but behaves as the DTLS **server**, expecting the answerer to be the
client. The previous `actpass → passive` mapping left both ends as servers, so the DTLS
handshake never started. RFC 8842 §5.3's active answerer (`auto`) is the standards-based
fix. If a specific gateway ever insists on being the DTLS client, set
`HERMES_VOIP_WEBRTC_DTLS_SETUP=passive`.

```sh
# Force the plugin to be the DTLS server for an actpass offer (rarely needed):
HERMES_VOIP_WEBRTC_DTLS_SETUP=passive
```

## The TURN knob (relay candidates — ADR-0034)

When neither a host nor a STUN-reflexive path is usable (symmetric NAT, restrictive
firewalls), a **TURN relay** candidate is needed. The plugin **consumes** an
operator-provided TURN server — **it does not run one** (a TURN server is external
infrastructure: run `coturn`, or use a contracted relay; out of this plugin's scope, rules
40/41).

| Item | Value |
| --- | --- |
| Env vars | `HERMES_VOIP_ICE_TURN_URLS`, `HERMES_VOIP_ICE_TURN_USERNAME`, `HERMES_VOIP_ICE_TURN_PASSWORD` |
| Type | URLs: comma-separated `turn:`/`turns:` list; username/password: strings |
| Default | empty ⇒ **no relay candidate** |
| Read by | `hermes_voip.config.load_media_config` → `MediaConfig.ice_turn_urls/_username/_password` |
| Applied at | `WebRtcMediaSession(turn_urls=…, turn_username=…, turn_password=…)` → `IceConnection` → aioice's TURN client, per inbound WebRTC call |

- **URL shape (RFC 7065):** `turn:<host>[:<port>][?transport=udp\|tcp]` (plain, default port
  **3478**) or `turns:<host>[:<port>]` (TLS, default port **5349**). Only the **first** URL is
  used (aioice accepts one TURN server). Example (values, no secret — the password lives in
  `.env` / 1Password, never here):

  ```
  HERMES_VOIP_ICE_TURN_URLS=turn:turn.example.test:3478?transport=udp
  HERMES_VOIP_ICE_TURN_USERNAME=relay-user
  HERMES_VOIP_ICE_TURN_PASSWORD=<in .env / 1Password>
  ```

- **Credentials are REQUIRED when URLs are set** (RFC 8656 §9.2 long-term credentials). A
  TURN URL with a missing username or password is a **loud `ConfigError` at load** (a
  credential-less TURN URL would gather no relay candidate — never a silent no-op, rule 27).
- **The password is a secret:** it is `repr`-suppressed on `MediaConfig` (never logged) and
  the TURN URL parser does not echo the URL on error.
- **Relay media is still end-to-end DTLS-SRTP:** the TURN server relays *ciphertext* — it is
  not in the media-trust path.
- **Live relay is an operator validation step.** Unit tests prove the URL parsing + the aioice
  TURN-param wiring + the relay-candidate SDP round-trip; a **full live relay** (a real
  allocation + media over the relay) requires a reachable TURN server you point the plugin at,
  validated like the live-gateway path (runbook 0002).

  Quick local TURN to validate against (operator machine; not part of CI):

  ```
  # A throwaway coturn with static long-term creds (replace the values):
  docker run -d --name coturn-test --network host coturn/coturn \
    -n --lt-cred-mech --realm=example.test --user=relay-user:RELAY_PASSWORD \
    --no-tls --no-dtls
  # then set the three env vars above (turn:127.0.0.1:3478) and place a WebRTC call.
  ```

## ICE consent freshness (RFC 7675 — long NAT'd calls don't silently drop)

A long call behind a NAT whose mapping silently expires must be **torn down
deterministically**, not left wedged (gap-analysis #6). `aioice` runs RFC 7675 consent
freshness **internally**: after ICE connects it issues a periodic STUN consent check on the
nominated pair (~every 5 s, randomised) and, after 6 consecutive failures, closes the ICE
connection. The plugin **surfaces** that closure: the closed ICE pipe makes the engine's ICE
reader's `recv()` raise, which the engine turns into a **transport-loss teardown** (the call
ends, `media_timed_out`). This is **independent of media flow** — a held/quiet call is
protected too (the media-inactivity timeout only fires when media *was* flowing and stopped).

- **No knob, no new code:** consent freshness is aioice-native; ADR-0034 adds no timing of its
  own (it keeps aioice's RFC-grounded interval/failure defaults) and only locks + surfaces the
  behaviour. Verify it is armed:

  ```
  uv run pytest tests/test_media_ice.py -k "consent or recv_after_close or unblocks" \
                tests/test_media_engine_ice.py -k "consent or transport_loss"
  ```

## Trickle ICE (SDP primitives — ADR-0034)

The plugin runs **half-trickle** (RFC 8838 §2/§16): its WebRTC answer **advertises trickle +
ICE2** (`a=ice-options:trickle ice2`), carries its **full** candidate set, then marks
`a=end-of-candidates`. It **parses** a peer's `a=ice-options`/`a=end-of-candidates` (exposed
as `AudioMedia.is_trickle` / `AudioMedia.end_of_candidates`) and **always** signals
end-of-candidates to ICE after the offer's candidates.

This is the **complete, interoperable** behaviour for our SIP-over-TLS / SIP-over-WSS targets —
in-dialog SIP-INFO (RFC 8840) candidate trickling is **not required** (ADR-0034 §2), not
deferred. Why: half-trickle is a compliant mode a full-trickle peer MUST accept (RFC 8838),
and because the plugin **does not advertise the `trickle-ice` SIP option-tag**, a compliant
RFC 8840 peer cannot confirm our support and MUST fall back to the full-candidate exchange we
serve (RFC 8840 §4.3/§5.3). Our gateways gather a full candidate set and send it in the initial
SDP anyway, so nothing is withheld for us to receive — and always ending candidates avoids the
ICE hang that withholding the marker (with no INFO receiver) would cause. The only thing that
would change this is a future decision to advertise `trickle-ice` for a peer that genuinely
trickles; that would be tasked into a signalling lane (it is a latency optimisation, not a gap).

## What happens on an inbound WebRTC call (the flow)

1. The INVITE's SDP offer carries `m=audio … UDP/TLS/RTP/SAVPF …` (+ `a=fingerprint`,
   `a=setup`, `a=ice-ufrag`/`a=ice-pwd`/`a=candidate`). `adapter._handle_inbound_invite`
   detects `offer.audio.is_webrtc` and takes the WebRTC branch (`_setup_webrtc_call`).
2. **Codec:** negotiated against the Opus-first WebRTC menu (`_WEBRTC_SUPPORTED_ENCODINGS =
   opus, PCMU, PCMA, telephone-event`), then clamped against the engine's capability table.
3. **ICE gather + answer:** `WebRtcMediaSession.prepare()` gathers ICE candidates and exposes
   our DTLS fingerprint, `a=setup` role (RFC 5763: `actpass`/`active` offer → we are
   `passive`; `passive` offer → we are `active`), and ICE creds/candidates. The SAVPF answer
   (`build_webrtc_answer`) carries them — **no `a=crypto`, no `c=`** (RFC 5763 §5) — and the
   `200 OK` is sent.
4. **ICE + DTLS handshake:** `run_handshake()` applies the peer's ICE creds/candidates, runs
   ICE, pumps the DTLS handshake over the ICE pipe (RFC 7983 first-byte demux), **verifies the
   peer's certificate fingerprint against the offered `a=fingerprint`** (RFC 5763 §5 — a
   mismatch aborts the call), and derives the inbound/outbound `SrtpSession` pair.
5. **Media:** `RtpMediaTransport` carries SRTP over the ICE pipe (no bound UDP socket). Opus
   is decoded at 48 kHz and **downsampled to 16 kHz** for the VAD/STT pipeline (Silero VAD
   accepts only 8/16 kHz); outbound TTS is resampled up to 48 kHz to encode.

## How to verify

- **Unit / handshake evidence (no live gateway):** `uv sync --extra webrtc --extra media` then

  ```
  uv run pytest tests/test_media_opus.py tests/test_media_engine_opus.py \
                tests/test_media_engine_ice.py tests/test_media_webrtc_session.py
  ```

  `test_media_webrtc_session.py` runs a **real DTLS-SRTP handshake** over an in-memory ICE
  pipe and asserts the role-mirrored SRTP cross-decrypts. The adapter branch (the full
  is_webrtc path) is exercised by `tests/test_adapter_webrtc.py` in the `hermes-contract` CI
  job (which installs hermes + webrtc + media + libopus).

- **SIP-over-WSS signalling (ADR-0038, no live gateway):**

  ```
  uv run pytest tests/test_adapter_wss_signalling.py \
                tests/transport/test_ws_connection.py \
                tests/test_config.py -k "ws_path or ws_password or wss or registration_config"
  ```

  Proves `_establish()` selects `WssSipTransport` for `transport=wss` (and the TLS
  transport for `tls`) wiring the same inbound observers; that the WSS REGISTER uses a
  `WSS` Via + `transport=ws` Contact; that a SAVPF/Opus/DTLS INVITE delivered over a faked
  WSS transport routes into the `is_webrtc` branch (the dialog + call context advertise
  `WSS`); and that `HERMES_SIP_WS_PASSWORD` overrides the digest on `wss` and falls back to
  `HERMES_SIP_PASSWORD` when unset.

- **Live (pending the operator's redeploy + a WebRTC client):** point a WebRTC client at the
  extension; in the operator log expect `WebRTC SDP answer built — setup=…`, `webrtc: DTLS-SRTP
  keyed (setup=…)`, `WebRTC media engine connected over ICE`, then `rtp tx/rx` lines. Two-way
  audio confirms the path.

## Live validation status — real Asterisk/UCM gateway (2026-06-18, ADR-0042)

First inbound WebRTC call from the live gateway (a Grandstream UCM whose WebRTC edge is an
embedded Asterisk). What was **proven on the wire**, in order:

1. **WSS REGISTER → `200 OK`** (expires ~299 s) on port `8090`, path `/ws`, subprotocol
   `sip`, realm `voip002`, MD5 `qop=auth` — using the **VoIP-section `Password`** (the SIP
   digest). `HERMES_SIP_WS_PASSWORD` is left **unset**; the item's top-level `password` is
   the GDMS/WAVE portal login and `401`s here (see runbook 0002).
2. **Inbound INVITE** classified to the `operator` group (needs `HERMES_VOIP_CALLER_ALLOW_FILE`
   set, else the default group declines with `603`).
3. **WebRTC SDP answer built** (`setup=passive`, Opus) + **`200 OK` sent** — the gateway's
   offer puts DTLS/ICE at the **SDP session level** in a BUNDLE; the parser now inherits
   them (ADR-0042 §1), so this no longer `488`s.
4. **ICE connectivity check SUCCEEDED.**

**Open item — media does not yet complete from the devcontainer (environment, not code).**
The container is double-NAT'd (Docker on the operator's Mac, Mac on the office LAN): only a
private IPv4 and a **ULA** IPv6, so a public-STUN srflx returns the office's hairpin-NAT IPv4
the gateway cannot reach → the controlling gateway never nominates an ICE pair → DTLS never
starts. The container **does** reach the gateway's **global IPv6** outbound (Docker NAT66),
and the host shares the gateway's IPv6 `/48`. Per the operator's **IPv6-first, IPv4-fallback**
directive the completion path is IPv6-first ICE (gather/prioritise IPv6; resolve STUN/TURN
over IPv6 so the answer advertises a gateway-reachable address); TURN is the IPv4 fallback.
Tracked as the next lane (a dedicated IPv6-first-ICE ADR).

To reproduce the validation so far: run the gateway with `HERMES_SIP_TRANSPORT=wss`,
`HERMES_SIP_PORT=8090`, `HERMES_SIP_WS_PATH=/ws`, the SIP `HERMES_SIP_*` creds, the model
dirs, and `HERMES_VOIP_CALLER_ALLOW_FILE`; dial the extension from a WAVE client; watch the
log for the four lines above.

## Security notes

- No DTLS private key, certificate, or SRTP key material is ever logged, `repr`'d, or raised
  in exception text (`media/dtls.py` / `media/webrtc_session.py`). The peer fingerprint is
  verified **before** any SRTP key is derived (`derive_srtp_sessions` enforces this).
- The DTLS certificate is **ephemeral, generated per endpoint at construction** — nothing is
  written to disk or committed.
- A failure at any step (no common codec, ICE failure, DTLS handshake failure, fingerprint
  mismatch) sends `488`/`500` and the call is **never half-answered** (rule 6).

## Rollback / disable

- **Disable WebRTC entirely:** there is no separate enable flag — WebRTC is driven by the
  offer profile. A peer that does not offer `UDP/TLS/RTP/SAVPF` never touches this path. To
  refuse WebRTC, do not point a WebRTC client at the extension (or omit the `webrtc` extra: a
  WebRTC offer then fails the call cleanly with a 488/ImportError, never dead audio).
- **STUN:** unset `HERMES_VOIP_ICE_STUN_URLS` to fall back to host-only ICE.
- **TURN:** unset `HERMES_VOIP_ICE_TURN_URLS` (and the username/password) to stop gathering a
  relay candidate. Rotating the TURN credential = update `HERMES_VOIP_ICE_TURN_PASSWORD` (in
  `.env` / 1Password) and the TURN server's user, then restart the plugin; nothing is cached.
- This is a Python plugin (no provisioned infrastructure to tear down). STUN/TURN servers, if
  any, are external services the operator runs separately and are out of this plugin's scope.
