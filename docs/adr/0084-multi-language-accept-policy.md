# ADR-0084 — Multi-language accept policy: BCP-47 format validation + English filler fallback

**Status:** Accepted  
**Date:** 2026-06-27  
**Deciders:** Troy Kelly (operator), Claude (agent)

---

## Context

`MediaConfig.language` was validated by membership in `_SUPPORTED_LANGUAGES`, which was
derived exclusively from `_COMFORT_FILLER_PHRASES_BY_LANGUAGE` (only `'en'` present).
As a result, every non-`'en'` language code raised `ConfigError` at startup — even though
the README and feature design describe multi-language support as an intended capability.

The core issue: comfort-filler availability drove the language-acceptance gate.  The two
concerns are orthogonal.  A caller in Spanish should not be rejected at startup just
because the operator has not yet loaded a Spanish phrase set.

## Decision

### Accept policy: BCP-47 primary-subtag format validation

A language code is **accepted** when its primary subtag is well-formed: 2–8 ASCII
alpha-characters (case-insensitive; lowercased on storage).  Sub-tags (e.g. `"pt-BR"`)
are accepted if the primary subtag is valid; the code is stored verbatim (lowercased).

This is implemented as:

```python
import re
_LANGUAGE_RE = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$")
```

Rationale for format rather than registry lookup:
- ISO 639 / IANA registry is large and changes; embedding a list would need maintenance.
- The downstream consumers (`stt`, `tts`, `call_loop`) already resolve per-provider
  language support; rejecting here for a language that a provider supports is
  user-hostile.
- `"zz"` and other 2–8-letter experimental codes are accepted by this grammar.
- A structurally malformed value (digits, empty, single char) signals a typo and is
  caught immediately.

**Accepted grammar boundary:** the primary subtag must be 2–8 ASCII alpha characters.
BCP-47 bare private-use singletons (`"x-foo"`, where `"x"` is the single-character
primary subtag) are **intentionally outside this scope**: an arbitrary `x-*` tag has no
defined meaning for STT/TTS providers and would only shift failure from startup to
call-time.  Operators who require a non-standard provider tag should supply it via
the provider-specific config rather than the language field.

The existing test for `"zz"` (previously expected to fail as "unknown") is updated in
this commit: `"zz"` is a well-formed 2-letter code and is now accepted.  The rejection
test uses `"12"` (all-digits) and `"e"` (single char) — both structurally malformed.

### Comfort-filler fallback: English phrases

When a language has no built-in phrase set in `_COMFORT_FILLER_PHRASES_BY_LANGUAGE`, the
system falls back to the English set (`_DEFAULT_COMFORT_FILLER_PHRASES`).  An operator
who wants Spanish filler provides their own phrases via
`HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES`.  This is not a silent downgrade: the fallback
is documented here; the operator receives no warning at startup (fail-fast only for
malformed codes, not for absent phrase translations — this is consistent with rule 4:
"default and move").

### `_validate_comfort_filler` change

The `language not in _SUPPORTED_LANGUAGES` check is removed.  The language is already
format-validated by `_parse_language` (env path) or in `__post_init__` (direct
construction).  The comfort-filler validator no longer needs to re-validate membership in
the phrase dict.

### `_parse_language` change

The membership check against `_SUPPORTED_LANGUAGES` is replaced by a regex match against
`_LANGUAGE_RE`.

### `_parse_comfort_filler_phrases` change

The `default = _COMFORT_FILLER_PHRASES_BY_LANGUAGE[language]` lookup (which KeyErrors on
an unknown language) is replaced by:

```python
default = _COMFORT_FILLER_PHRASES_BY_LANGUAGE.get(language, _DEFAULT_COMFORT_FILLER_PHRASES)
```

### `_validate_comfort_filler` in `__post_init__` change

The `language not in _SUPPORTED_LANGUAGES` guard is replaced by a regex check so direct
construction with e.g. `language="es"` is also validated structurally without raising.

## Consequences

- Any syntactically valid BCP-47 primary subtag is accepted; the system constructs and
  runs.  Comfort-filler phrases default to English when no built-in set exists.
- Operators who provide `HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES` get their custom set
  regardless of language.
- Adding a new language's built-in phrase set remains a data-only change in
  `_COMFORT_FILLER_PHRASES_BY_LANGUAGE` — nothing else changes (comment at line 310
  already says this and remains accurate).
- The reject boundary moves from "unknown to our phrase dict" to "malformed BCP-47 primary
  subtag", which is the right semantic.
- `_SUPPORTED_LANGUAGES` becomes unused and is removed.
