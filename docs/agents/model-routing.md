# Cross-Vendor Model Routing Reference
<!-- cspell:words revertable pre-emptively Sakana Fugu Glasswing Zhipu cutoffs cutoff opencode TTFT cursorrules summarises summarisation hostable chatbots tokenises teardowns batchable uncompacted Cowork ccusage authorised authorisations behaviour LLAP xhigh remoteclip gapped prem -->

**As of:** 15 August 2026 · **Scope:** Anthropic (all current), OpenAI (all current), xAI Grok (via LLAP), Z.ai GLM-5.2, Sakana Fugu / Fugu Ultra
**Read §1 and §5 first. §2 is the lookup table. Everything else is exception handling.**
**This file loads on demand only. Do not import it into AGENTS.md/CLAUDE.md.**

---

## 1. Fast routing rules

| If the task is… | Route to | Why |
| --- | --- | --- |
| Bulk classification, extraction, reformatting, triage | **GPT-5.6 Luna** or **Claude Haiku 4.5** | 5–25× cheaper than frontier; quality difference is noise on mechanical work |
| Everyday coding, refactors, PR review, test writing | **Claude Sonnet 5**, **GLM-5.2** or **grok-build-0.1** | Near-frontier coding at commodity prices; Sonnet's $2/$10 is now permanent |
| Hard code review, deep bug analysis, independent second author | **Grok 4.6** or **GPT-5.6 Terra** | Grok 4.6: 96% SWE-bench Verified at $2/$6 — but slow (~35 s to first token); right for review passes, wrong for interactive loops |
| Long-horizon agentic coding, multi-agent supervision, enterprise work | **Claude Opus 5** | Anthropic's own default recommendation; strongest at writer-verifier subagent patterns |
| Hardest from-scratch engineering; you've already failed with Opus 5 | **Claude Fable 5** or **GPT-5.6 Sol** | 2× the token price — only when the retry cost exceeds the price delta; justification line required |
| Codex-harness / terminal-agent work in OpenAI tooling | **gpt-5.3-codex** | Purpose-built; cheaper than Sol for the same harness |
| Long-context ingestion (whole repos, discovery bundles, transcript piles) | **GLM-5.2**, **GPT-5.6 Terra** or **grok-4.3** | ~1M context at a fraction of frontier price; watch the long-context price steps |
| Authorised defensive security work | **GPT-5.6 Cyber / Daybreak** or **Claude Mythos 5** | Both gated; see §6 |
| Adversarial review of another model's output | **See §4** — never the same vendor family | |
| Realtime voice, transcription, image, video | OpenAI specialist models, §3 | Nobody else in this list competes |

**Default when unsure:** Sonnet 5 for the work, a cheap cross-vendor pass for review, Opus 5 only to arbitrate disputed findings. Escalate on failure, never pre-emptively.

---

## 2. Text / reasoning / agentic models

Prices are USD per million tokens, list rate, standard service tier, short-context band.

### Anthropic

| Model | API ID | Ctx / Max out | $ in → out | Press into service for | Don't route here |
| --- | --- | --- | --- | --- | --- |
| **Claude Fable 5** | `claude-fable-5` | 1M / 128k | 10 → 50 | Highest available capability; long-running agents; always-on adaptive thinking | Anything Opus 5 already does well — it's 2× the price and slower |
| **Claude Mythos 5** | `claude-mythos-5` | 1M / 128k | 10 → 50 | Defensive cybersecurity workflows | Not generally available — invitation-only via Project Glasswing |
| **Claude Mythos Preview** | `claude-mythos-preview` | 1M / 128k | — | Same, earlier snapshot | Same gating |
| **Claude Opus 5** | `claude-opus-5` | 1M / 128k | 5 → 25 | Complex agentic coding, deep reasoning, multi-agent coordination, long-context consistency | High-volume mechanical work; latency-sensitive paths. Its "fast mode" (research preview) bills 10 → 50 — never leave it enabled in scripts |
| **Claude Sonnet 5** | `claude-sonnet-5` | 1M / 128k | 2 → 10 **(made permanent 10 Aug 2026 — the scheduled 1 Sep rise to 3/15 was cancelled)** | The daily driver: coding, agentic loops, browse/computer-use at near-Opus quality | Tasks that have already failed once on Sonnet — escalate, don't retry |
| **Claude Haiku 4.5** | `claude-haiku-4-5-20251001` | 200k / 64k | 1 → 5 | Fastest tier; near-frontier on short mechanical tasks; the only current Claude with *manual* extended thinking | Anything over 200k context; long-horizon agentic work |

Notes: Opus 5 and Sonnet 5 expose `effort` (levels low/medium/high/**xhigh/max** on the 5-series; defaults to `high` on API and Claude Code — pin it down explicitly). Adaptive thinking is on by default on Fable/Opus/Sonnet 5; manual `thinking.budget_tokens` now **HTTP-400s** on those three. Non-default `temperature`/`top_p`/`top_k` also error on Sonnet 5. Prompt caching: reads 0.1×, 5-min writes 1.25×, 1-hour writes 2× base input; Claude Code on subscription auto-uses the 1-hour TTL. Server-side **compaction** beta (`compact-2026-01-12`) summarises long conversations in the API itself — same-model summarisation only, and its tokens bill *outside* the top-level usage fields (sum `usage.iterations`). Batch −50%. Reliable knowledge cutoffs: Opus 5 = May 2026; Fable 5 / Sonnet 5 = Jan 2026; Haiku 4.5 = Feb 2025.

### OpenAI

| Model | API ID | Ctx / Max out | $ in → out | Press into service for | Don't route here |
| --- | --- | --- | --- | --- | --- |
| **GPT-5.6 Sol** | `gpt-5.6-sol` (alias `gpt-5.6`) | 1.05M / 128k | 5 → 30 | OpenAI flagship: complex reasoning and coding; currently top of most OpenAI leaderboards | Volume work; anything Terra handles |
| **GPT-5.6 Terra** | `gpt-5.6-terra` | 1.05M / 128k | 2 → 12 | The intelligence/cost balance point; long-context ingestion | Hardest novel engineering |
| **GPT-5.6 Luna** | `gpt-5.6-luna` | 1.05M / 128k | 0.20 → 1.20 | Cost-sensitive high volume — cheapest 1M-context model in this table | Multi-step agentic work needing sustained judgement |
| **GPT-5.6 Cyber** | `gpt-5.6-cyber` | — | 12.50 → 75 | Authorised vulnerability research and security testing | Anything not covered by a trusted-access agreement |
| **Daybreak Red / Blue** | `daybreak-red-latest` / `daybreak-blue-latest` | — | — | Red = advanced offensive-capable cyber (gated); Blue = frontier general model with defensive-cyber safeguards | Same |
| **GPT-5.3 Codex** | `gpt-5.3-codex` | — | 1.75 → 14 | Coding specialist inside the Codex harness; strong price/perf for agentic dev | General reasoning, non-code work |
| **ChatGPT parity** | `chat-latest` | — | 5 → 30 | Reproducing ChatGPT consumer behaviour exactly | API work — pin a real model instead |
| **GPT-5.5, GPT-5.4 (+Pro/mini/nano)** | `gpt-5.5`, `gpt-5.4*` | — | — | Prior generation; still referenced in benchmarks | New routing — superseded by 5.6 |
| **GPT-OSS 120B** | open weights | — | self-host | Best OpenAI open-weight option; air-gapped / on-prem | Frontier-quality expectations |

Notes: all 5.6 models have a Feb 16, 2026 knowledge cutoff, six reasoning levels (`none` → `max`), and support functions, web search, file search and computer use. Prices are the short-context band — see §5. Batch = 50% off. Fast mode (formerly Priority) = 2× standard. Fine-tuning is closed to new users.

### xAI — Grok (served estate-wide through LLAP; `grok` CLI available in every repo)

Reality check, 15 Aug 2026: **there is no public "Grok 5.x"** — the current line is 4.x, flagship `grok-4.6` released 12 Aug 2026. If LLAP's model list shows a different alias, trust LLAP's list; the specs below are the published ones.

| Model | API ID | Ctx / Max out | $ in → out | Press into service for | Don't route here |
| --- | --- | --- | --- | --- | --- |
| **Grok 4.6** | `grok-4.6` | 500k / n.p. | 2 → 6 (**4 → 12 above 200k input**) | Flagship; xAI recommends it for coding; 96% SWE-bench Verified (4th of 82). Hard code review, deep bug analysis, independent second author | Interactive/latency-sensitive paths (~68 tok/s, ~35 s TTFT); terminal-heavy agent loops (Terminal-Bench 26%) |
| **Grok 4.5** | `grok-4.5` | 500k / n.p. | 2 → 6 | Prior flagship; same price as 4.6 — prefer 4.6 | — |
| **grok-build-0.1** | `grok-build-0.1` | 256k / n.p. | 1 → 2 | Dedicated agentic-coding model (May 2026; succeeds grok-code-fast-1); the natural daily tier inside the grok CLI | Contexts over 256k; hardest novel engineering |
| **Grok 4.3** | `grok-4.3` | 1M / n.p. | 1.25 → 2.50 | Cheap/fast tier and 1M long-context ingestion | Frontier-quality expectations |

Notes: OpenAI-compatible API shape only (`api.x.ai/v1`; LLAP fronts it — no Anthropic-compatible endpoint exists). The official CLI is **Grok Build** (`xai-org/grok-build`, Apache-2.0): reads **AGENTS.md natively** (also CLAUDE.md, `.claude/rules/`, `.cursor/rules/`), cascades config `~/.grok/` → cwd, custom base URL via `~/.grok/config.toml` → pointed at LLAP. Inside estate devcontainers, `remoteclip install` already routes plain `grok` (and `codex`/`opencode`) through LLAP via generated user-level config — repository files never need LLAP wiring. Max output and rate limits are **not published**; xAI has had 2026 outage/throttling incidents and a data-retention controversy — treat availability as best-effort, keep regulated data on LLAP-approved routes only. Corporate note: xAI is now "SpaceXAI" after the SpaceX merger; product and model naming unchanged, but expect domain/ID churn that LLAP absorbs.

### Z.ai (Zhipu) and Sakana

| Model | API ID | Ctx / Max out | $ in → out | Press into service for | Don't route here |
| --- | --- | --- | --- | --- | --- |
| **GLM-5.2** | `z-ai/glm-5.2` | 1M / 128k | 1.40 → 4.40 (cached in 0.26) | Best value in the table for long-horizon coding and agentic tool use; **MIT open weights** → self-hostable, no vendor lock; genuinely independent reviewer | Hardest from-scratch coding — it trails the closed frontier there; anywhere PRC-jurisdiction API routing is unacceptable |
| **Fugu** | `sakana/fugu` | — | billed at the underlying model's own rate (no stacking) | Latency-oriented tier: everyday queries, code review, chatbots | Deep multi-step work — that's Ultra's job |
| **Fugu Ultra** (v1.1 current; v1.0 = `fugu-ultra-20260615`) | `sakana/fugu-ultra` | 1M / 128k | 5 → 30 (cached 0.50); **10 → 45 above 272k ctx** | Quality-first multi-agent orchestration: AI research, paper reproduction, patent/literature investigation | Anything interactive (observed latency 8–160 s); anything where token spend is the binding constraint (§5) |

Notes: Fugu is a learned orchestrator routing each task across a swappable pool of frontier models, recursively self-calling. Sakana positions Ultra level with Fable 5 / Mythos Preview on hard engineering/science benchmarks. One OpenAI-compatible API; v1.1 added a Claude Code-compatible interface. Subscriptions $20/$100/$200 per month; pay-as-you-go is served at *higher* priority than subscriptions. GLM-5.2 is a ~744B sparse MoE (~40B active), High/Max thinking effort; the flat GLM Coding Plan (~$18/month) covers supported coding tools only.

---

## 3. OpenAI specialist models (no cross-vendor equivalent here)

| Need | Model | Rate |
| --- | --- | --- |
| Image generation / editing | `gpt-image-2` | $8 in / $30 out per MTok (image); $5 text in |
| Video | `sora-2` / `sora-2-pro` | $0.10/sec; Pro $0.30–$0.70/sec by resolution |
| Realtime speech-to-speech | `gpt-realtime-2.1`, `-mini` | $32/$64 audio; mini $10/$20 |
| Live translation | `gpt-realtime-translate` | ~$0.034/min |
| Live transcription | `gpt-live-transcribe`, `gpt-realtime-whisper` | ~$0.017/min |
| File transcription | `gpt-transcribe` | ~$0.0045/min — cheapest by a wide margin |
| TTS | `gpt-4o-mini-tts` | — |

Hosted tool costs that hit every model: web search $10/1k calls plus content tokens at model rate; file search $2.50/1k calls plus $0.10/GB/day storage; code-interpreter containers $0.03–$1.92 per 20-min session by RAM.

---

## 4. Vendor-adversarial review pairings

The point is *independent failure modes*. Same-family review is near-worthless — shared training data means shared blind spots. Grok (xAI/SpaceXAI) is a genuine fourth independent family alongside Anthropic, OpenAI and Z.ai.

| Work produced by | Review with | Second opinion |
| --- | --- | --- |
| Any Claude model | **GPT-5.6 Sol** or **Grok 4.6** | **GLM-5.2** |
| Any GPT model | **Claude Opus 5** or **Grok 4.6** | **GLM-5.2** |
| Grok | **Claude Opus 5** | **GPT-5.6 Terra** |
| GLM-5.2 | **Claude Opus 5** | **GPT-5.6 Terra** or **Grok 4.6** |
| Anything, cheap sanity pass | **GPT-5.6 Luna**, **Haiku 4.5** or **grok-build-0.1** | — |

**Critical caveat: Fugu / Fugu Ultra are not independent reviewers.** They orchestrate a pool of frontier models that may include the very model that produced the work. Use Fugu Ultra as a strong second author, never as the independence guarantee. Provable independence with no shared vendor: GLM-5.2 self-hosted.

Practical pattern: produce with Sonnet 5 → adversarial review with Grok 4.6 or GPT-5.6 Terra (batched, −50%) → escalate disputed findings to Opus 5. Three passes at those rates still costs less than one Fable 5 pass. Grok reviews are slow (~35 s TTFT) — fine batched, wrong inline.

---

## 5. Token and cost traps (read before doing cost maths)

1. **Claude tokenises ~30% heavier.** Claude 4.7+ tokenizers produce roughly 30% more tokens for the same text; every cross-vendor $/MTok comparison above understates Claude cost by about a third. Measure with the actual tokenizer before committing.
2. **Corrected 10 Aug 2026: Sonnet 5's $2/$10 is permanent.** The scheduled 1 Sep 2026 rise to $3/$15 was cancelled. Delete any budget note still claiming the intro rate expires 31 Aug — and note this strengthens Sonnet-first routing.
3. **Effort is the biggest single lever.** `effort` defaults to `high` on API and Claude Code. Estate repos pin `effortLevel: medium` in `.claude/settings.json`; raise per-session only. Same logic for OpenAI's `none`/`low` reasoning levels and Codex's `model_reasoning_effort` (pinned `medium`).
4. **The prompt-cache key includes model AND effort.** A mid-session `/model` or `/effort` flip invalidates the entire cached prefix — on a long session that is a full re-read at input price. Route per session, not per message.
5. **Long-context price steps.** OpenAI 5.6 roughly doubles above the short band (Sol 5→10 in, 30→45 out). **Grok 4.6 doubles above 200k input (2/6 → 4/12).** Fugu Ultra steps at 272k. Chunking below the threshold is often cheaper than one long call.
6. **Fugu Ultra's orchestration tokens bill on top of visible I/O** — independent teardowns put overhead at ~5–10× visible tokens with a ~1,260-token floor. Treat $5/$30 as a floor, not a rate.
7. **Batch where latency doesn't matter.** OpenAI and Anthropic Batch are both −50%. Adversarial review passes are almost always batchable.
8. **Prompt caching pays for itself on agentic loops** — cached reads run 0.1× input ($0.26 GLM-5.2, $0.50 Opus 5/Sol/Fugu Ultra, $0.02 Luna). 1-hour Claude TTL costs 2× on writes — worth it for long agent sessions, waste for one-shots.
9. **GLM Coding Plan quota is not linear** — ~3× quota burn in the 14:00–18:00 UTC+8 peak, ~2× off-peak; one prompt may invoke the model 15–20×; the plan covers supported coding tools only.
10. **Grok's numbers are thinner than they look.** No published rate limits or max-output figures; assume throttling risk in bulk pipelines. LLAP is the failure-isolation layer.
11. **Ceremony, not reasoning, drives agent bills.** 2026 audits keep finding the spend in re-sent uncompacted context (one audit: 62% of the bill), tool-schema bloat and needless suite re-runs — not model thinking. Compact, prune, and enforce test budgets before reaching for a cheaper model.
12. **Opus 5 "fast mode" (research preview) bills 2× (10 → 50).** Never leave it on in scripts or CI.
13. **Subscription burn is windowed, not just priced.** Claude plan usage meters on a rolling 5-hour window AND a weekly window, shared across Claude Code, chat and Cowork; hitting either is a hard lockout until reset. Opus has its own sub-limit that blocks only Opus — `/model` down and keep working. Burn tracks list price: Opus ≈2.5× Sonnet per token, Fable ≈5× Sonnet (and Fable cannot disable extended thinking). A wide parallel fan-out can eat the weekly window in hours — schedule fan-outs early in the week or move them off-plan (next trap).
14. **Move bursts off the subscription.** `ANTHROPIC_API_KEY` outranks subscription login in Claude Code's auth precedence: export it in the shell that launches a fan-out and those sessions bill pay-as-you-go instead of draining the plan windows. The sanctioned overage path is usage credits (`/usage-credits`, login-auth only) at standard API rates with configurable spend limits and a $2,000/day redemption cap.
15. **You can't manage what you don't meter.** `/usage` (`/cost` is now an alias for it) shows session tokens/cost, plan-window state, and per-skill/subagent/MCP attribution. `CLAUDE_CODE_ENABLE_TELEMETRY=1` exports OTel metrics (`claude_code.token.usage`, `claude_code.cost.usage`) — there is no built-in repo attribute, which is why estate repos commit `OTEL_RESOURCE_ATTRIBUTES=estate.repo=…` in `.claude/settings.json` `env`. ccusage-class tools read the local session JSONL regardless of auth mode. Turn one of these on before tightening budgets further: measured spend beats argued spend.
16. **Output ceilings beat output policing.** Claude Code already truncates Bash results (~30k chars inline; `BASH_MAX_OUTPUT_LENGTH`, default 30k, ceiling 150k), caps MCP results (`MAX_MCP_OUTPUT_TOKENS`, default 25k), and partial-views oversized file reads — and its built-in Grep tool is ripgrep returning paths-only by default, so "blind grep" waste mostly means shelling out to raw `grep`. Naming trap: `CLAUDE_CODE_MAX_OUTPUT_TOKENS` caps the model's *response*, not bash output. Reserve deny-hooks for narrow, unambiguous, high-cost patterns (the test budget; the big-dump guard) — a denied call costs a retry round-trip, so ambiguous verbosity belongs to these ceilings, the core's context rules, and subagent isolation; since mid-2026 a PreToolUse hook can also *rewrite* a command via `updatedInput` instead of bouncing it.

---

## 6. Access, jurisdiction and gating constraints

- **Claude Fable 5** ships with conservative safeguards: some queries are silently answered by **Opus 5** instead (Anthropic: <5% of sessions). If benchmarking Fable 5, verify which model actually responded.
- **Fable 5 / Mythos 5 export controls:** access suspended 12 Jun 2026 under a US Commerce directive, restored 1 Jul 2026. Current but politically exposed.
- **Mythos 5 / Mythos Preview:** invitation-only, Project Glasswing — <https://www.anthropic.com/glasswing>.
- **GPT-5.6 Cyber / Daybreak Red:** trusted-access gating, authorised vulnerability research only.
- **Fugu:** not available in EU/EEA pending GDPR work; no published timeline.
- **GLM-5.2 hosted API:** subject to PRC law. Falls away if you self-host the MIT weights (~8×H200, order of $300k — worth it above ~1B tokens/month or under hard data-residency rules).
- **Grok / LLAP:** xAI models reach the estate only through LLAP — no direct xAI accounts. Undisclosed rate limits, 2026 availability incidents, and a data-retention controversy: keep regulated data off Grok routes unless LLAP policy explicitly allows it.
- **Sonnet 5** refusals return HTTP 200 with `stop_reason: "refusal"`, not an error. Handle that path in agent code.

Anthropic model IDs are **pinned snapshots, not evergreen aliases** — the dateless form (`claude-opus-5`) is still a pin. Don't assume it rolls forward.
