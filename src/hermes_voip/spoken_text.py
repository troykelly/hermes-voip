"""Spoken-text sanitisation — clean agent reply text before TTS synthesis.

An LLM reply (e.g. from gpt-5.5) can contain emoji, markdown formatting, and
raw URLs that a TTS engine voices literally — producing odd artefacts like the
engine saying "smiley face", "asterisk asterisk bold asterisk asterisk", or
"h-t-t-p-s-colon-slash-slash". This module strips all of that to produce
clean, speakable prose before the text enters the TTS synthesis path.

Public API
----------
:func:`sanitize_for_speech` — the single entry point. Pure, synchronous,
stdlib-only (no new runtime dependencies).

Pipeline (applied in order)
----------------------------
1. Markdown link syntax ``[text](url)`` → anchor text only (before URL strip so
   the bare-URL rule does not swallow the anchor text).
2. Code fences (triple-backtick blocks) stripped including interior content,
   because voicing raw code is always noise.
3. Bare URLs (``http://`` / ``https://`` / ``ftp://`` …) stripped entirely.
4. Remaining markdown inline markup (``*``, ``_``, ``#``, backtick, ``>``)
   removed; bullet-list markers at the start of a line removed.
5. Repeated punctuation (``!!!`` → ``!``, ``...`` → ``…``-equivalent space,
   ``???`` → ``?``) collapsed.
6. Emoji + pictograph codepoints stripped: explicit Unicode block ranges that
   cover emoticons, misc symbols, dingbats, variation selectors, ZWJ sequences,
   skin-tone modifiers, regional-indicator flag pairs, and tag sequences.
7. Whitespace normalised: all runs of whitespace (including newlines and tabs)
   collapsed to a single space, then stripped of leading/trailing whitespace.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["sanitize_for_speech", "strip_audio_tags"]

# ---------------------------------------------------------------------------
# Emoji / pictograph codepoint ranges
# ---------------------------------------------------------------------------
# Explicit range table rather than a third-party emoji library to stay
# dependency-free.  Ranges are (lo, hi) inclusive Unicode scalar values.
# Source: Unicode 15.1 emoji data + derived properties.
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    # Emoticons block
    (0x1F600, 0x1F64F),
    # Miscellaneous symbols and pictographs
    (0x1F300, 0x1F5FF),
    # Transport and map symbols
    (0x1F680, 0x1F6FF),
    # Supplemental symbols and pictographs
    (0x1F900, 0x1F9FF),
    # Extended pictographic supplemental-A
    (0x1FA00, 0x1FA6F),
    # Extended pictographic supplemental-B
    (0x1FA70, 0x1FAFF),
    # Regional indicator symbols (flag pairs)
    (0x1F1E0, 0x1F1FF),
    # Enclosed alphanumeric supplement (keycap bases etc.)
    (0x1F100, 0x1F1FF),
    # Dingbats
    (0x2700, 0x27BF),
    # Miscellaneous symbols
    (0x2600, 0x26FF),
    # Variation selectors (U+FE00..U+FE0F) — emoji vs text presentation
    (0xFE00, 0xFE0F),
    # Combining enclosing marks used in emoji sequences
    (0x20D0, 0x20FF),
    # Zero Width Joiner (used to join multi-part emoji sequences)
    (0x200D, 0x200D),
    # Tags block (U+E0000..U+E007F) — emoji tag sequences
    (0xE0000, 0xE007F),
    # Modifier Fitzpatrick skin-tone modifiers
    (0x1F3FB, 0x1F3FF),
)

# Build a flat sorted list of (lo, hi) for fast binary-search-style membership.
# We use a frozenset of individual chars for the small ranges, and the range
# table for the SMP (high codepoint) blocks that a char-by-char frozenset
# approach cannot handle within reason.
_EMOJI_RANGE_TUPLE: tuple[tuple[int, int], ...] = _EMOJI_RANGES


def _is_emoji_cp(cp: int) -> bool:
    """Return True when *cp* (a Unicode scalar value) is in a strip-list range."""
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGE_TUPLE)


def _is_emoji_char(ch: str) -> bool:
    """Return True if *ch* should be stripped as an emoji/pictograph.

    Checks explicit ranges first (fast path for the bulk of emoji), then falls
    back to the Unicode *Symbol, Other* (So) general category which covers many
    pictographic symbols not otherwise enumerated in the range table.
    """
    cp = ord(ch)
    if _is_emoji_cp(cp):
        return True
    # Unicode category "So" = Symbol, Other — covers pictographs not in the
    # block ranges (e.g. U+2713 CHECK MARK, U+2764 HEAVY BLACK HEART, etc.)
    return unicodedata.category(ch) == "So"


# ---------------------------------------------------------------------------
# Compiled regexes (compiled once at import time)
# ---------------------------------------------------------------------------

# Markdown link: [anchor text](URL) — capture group 1 is the anchor text.
# Non-greedy to avoid spanning multiple links on one line.
_RE_MD_LINK: re.Pattern[str] = re.compile(r"\[([^\]]*)\]\([^)]*\)")

# Code fence: ```...``` (possibly with language tag, possibly multi-line).
_RE_CODE_FENCE: re.Pattern[str] = re.compile(r"```[^`]*```", re.DOTALL)

# Bare URL: http/https/ftp scheme followed by non-whitespace characters.
_RE_BARE_URL: re.Pattern[str] = re.compile(
    r"(?:https?|ftp)://\S+",
    re.IGNORECASE,
)

# Heading markers: one or more # at the start of a line (with optional space).
_RE_HEADING: re.Pattern[str] = re.compile(r"^#+\s*", re.MULTILINE)

# Blockquote marker: > at start of line.
_RE_BLOCKQUOTE: re.Pattern[str] = re.compile(r"^>\s?", re.MULTILINE)

# Bullet list markers: -, *, + at start of line (with optional indent).
_RE_BULLET: re.Pattern[str] = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)

# Inline code: `...` (single backtick, non-greedy) — keep the inner text.
_RE_INLINE_CODE: re.Pattern[str] = re.compile(r"`([^`]*)`")

# Bold: **text** or __text__
_RE_BOLD: re.Pattern[str] = re.compile(r"(\*\*|__)(.*?)\1", re.DOTALL)

# Italic: *text* or _text_ (single delimiter, not preceded/followed by same)
_RE_ITALIC: re.Pattern[str] = re.compile(r"(\*|_)(.*?)\1")

# Repeated identical punctuation: !! → !, ??? → ?, !!! → !, ... → .
_RE_REPEAT_PUNCT: re.Pattern[str] = re.compile(r"([!?.]){2,}")

# Any whitespace run (including newlines, tabs) → single space.
_RE_WHITESPACE: re.Pattern[str] = re.compile(r"\s+")

# ElevenLabs v3 audio tag: an inline performance cue in square brackets, e.g.
# ``[laughs]``, ``[breath]``, ``[clears throat]``, ``[whispers]``. The shape is
# deliberately narrow so it matches voice cues but NOT a numeric footnote marker
# (``[3]``), a citation, or a long bracketed aside that is really a sentence: it
# must OPEN with an ASCII letter, then contain only letters / spaces / apostrophes
# / hyphens, up to 31 inner characters. (Markdown links ``[text](url)`` are handled
# earlier in :func:`sanitize_for_speech`, so this never has to disambiguate them.)
# Used to STRIP tags on a model that cannot interpret them (Flash/Turbo/Multilingual
# /Kokoro) so the bracketed word is never voiced literally; a v3 model keeps them.
# Inner chars: ASCII letters, spaces, ASCII apostrophes, and hyphens (ElevenLabs
# audio-tag names are ASCII, e.g. ``[clears throat]``).
_RE_AUDIO_TAG: re.Pattern[str] = re.compile(r"\[[A-Za-z][A-Za-z '-]{0,31}\]")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sanitize_for_speech(text: str) -> str:
    """Sanitise *text* so it is clean spoken prose, safe to pass to TTS.

    Converts an LLM reply (which may contain emoji, markdown, and raw URLs)
    into plain text a TTS engine can voice without artefacts. Pure and
    synchronous; no I/O, no external dependencies.

    Pipeline:
    1. Markdown links ``[anchor](url)`` → anchor text only.
    2. Code fences (`` ``` … ``` ``) → stripped (including content).
    3. Bare URLs → dropped.
    4. Markdown inline markup (bold, italic, headings, blockquotes,
       bullet markers, inline code, stray backticks/asterisks/underscores) →
       stripped.
    5. Repeated punctuation collapsed (``!!!`` → ``!``).
    6. Emoji and pictograph codepoints → stripped.
    7. Whitespace normalised to single spaces.

    Args:
        text: The raw agent reply string (may contain LLM-style markdown/emoji).

    Returns:
        Clean spoken text with no emoji, markdown markup, or raw URLs.
    """
    # 1. Markdown links: [anchor](url) → anchor text
    text = _RE_MD_LINK.sub(r"\1", text)

    # 2. Code fences (```...```) stripped entirely (including interior)
    text = _RE_CODE_FENCE.sub(" ", text)

    # 3. Bare URLs dropped
    text = _RE_BARE_URL.sub(" ", text)

    # 4. Markdown inline markup stripped
    # Bold first (so ** doesn't leave stray * after the italic pass)
    text = _RE_BOLD.sub(r"\2", text)
    text = _RE_ITALIC.sub(r"\2", text)
    text = _RE_HEADING.sub("", text)
    text = _RE_BLOCKQUOTE.sub("", text)
    text = _RE_BULLET.sub("", text)
    # Inline code: keep the inner text, just strip the backtick delimiters
    text = _RE_INLINE_CODE.sub(r"\1", text)
    # Stray backticks (from un-paired or code-fence remnants)
    text = text.replace("`", "")
    # Stray asterisks/underscores not caught by bold/italic (e.g. single *, _)
    text = text.replace("*", "").replace("_", "")
    # Hash symbols (heading remnants at non-line-start positions)
    text = text.replace("#", "")
    # Angle bracket (blockquote remnants)
    text = text.replace(">", "")

    # 5. Repeated punctuation collapsed
    text = _RE_REPEAT_PUNCT.sub(r"\1", text)

    # 6. Emoji and pictograph codepoints stripped
    text = _strip_emoji(text)

    # 7. Whitespace normalised
    return _RE_WHITESPACE.sub(" ", text).strip()


def strip_audio_tags(text: str) -> str:
    """Remove ElevenLabs v3 audio-tag tokens (``[laughs]``, ``[breath]``, …).

    Drops the **whole** bracketed cue — brackets *and* the word inside — so a TTS
    model that cannot interpret v3 audio tags never voices the bracketed word
    literally (Flash/Turbo/Multilingual would otherwise say "breath" for
    ``[breath]``). Only audio-tag-*shaped* brackets are removed (see
    :data:`_RE_AUDIO_TAG`): a numeric footnote like ``[3]`` or a long bracketed
    aside is left intact. The space the removed tag occupied is collapsed so no
    double space remains.

    Pure and synchronous, stdlib-only. Applied at the TTS-provider seam **only on a
    model that does not support audio tags** — a v3-family model preserves them so
    the agent's performance cues actually render (ADR-0027).

    Args:
        text: Spoken-text (already emoji/markdown/URL-sanitised) that may contain
            inline v3 audio-tag cues.

    Returns:
        ``text`` with every audio-tag token removed and whitespace collapsed.
    """
    stripped = _RE_AUDIO_TAG.sub(" ", text)
    return _RE_WHITESPACE.sub(" ", stripped).strip()


def _strip_emoji(text: str) -> str:
    """Remove all emoji and pictograph characters from *text*.

    Characters are kept when they are ASCII or when they are Unicode letters,
    numbers, marks, punctuation, or space separators — anything that belongs
    to normal written prose. Characters in the explicit emoji ranges (block
    table) or in the Unicode *Symbol, Other* (So) category are dropped.
    """
    out: list[str] = []
    for ch in text:
        if _is_emoji_char(ch):
            continue
        out.append(ch)
    return "".join(out)
