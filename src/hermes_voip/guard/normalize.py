"""Pre-classification text canonicalisation for the injection guard (ADR-0009 §1).

A telephony caller (or a transcript of one) can disguise an instruction so a
classifier scores its *surface* form benign: zero-width characters inside a word,
fullwidth / homoglyph look-alikes, or a reversibly-encoded payload (base64,
ROT13, leetspeak). ADR-0009 step 1 requires the guard to classify the *decoded*
form, not the disguise.

:func:`normalize` produces a :class:`NormalizedText` with:

* ``canonical`` — the surface text after control-stripping, NFKC normalisation,
  and homoglyph folding (the always-present, de-obfuscated baseline);
* ``candidates`` — ``canonical`` plus any reversible *decode* (base64/ROT13/leet)
  that yielded readable text, de-duplicated, canonical first. Decodes are
  **added**, never substituted: a decode that turns text into noise must not
  blind the classifier to the literal payload, so the surface form is retained;
* ``reasons`` — one audit string per transform that fired, so the guard's
  ``GuardResult.reasons`` can explain *why* a turn was flagged.

The module is pure and dependency-free (stdlib only): no model, no I/O. It is the
deterministic front half of the guard and is unit-tested in isolation.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import re
import unicodedata
from dataclasses import dataclass

__all__ = ["NormalizedText", "normalize"]

# Unicode "Other" (control/format/surrogate/unassigned) and "Separator" we treat
# as noise to strip *inside* the canonical form — except ASCII whitespace, which
# we keep so word boundaries survive. Zero-width space/joiner, bidi controls, and
# embedded NULs all fall here; stripping them re-joins a word split to dodge a
# classifier ("ig<zwsp>nore" -> "ignore").
_STRIPPABLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

# A curated homoglyph confusable map: non-ASCII letters that render like ASCII
# letters (Cyrillic / Greek look-alikes are the common voice-transcript and
# copy-paste evasions). Applied AFTER NFKC (which already folds fullwidth/compat
# forms), so this only needs the same-glyph-different-script confusables NFKC
# leaves alone. Lowercase keys; the fold is applied to the casefolded text.
_HOMOGLYPHS: dict[str, str] = {
    # Cyrillic
    "\u0430": "a",  # U+0430
    "\u0435": "e",  # U+0435
    "\u043e": "o",  # U+043E
    "\u0441": "c",  # U+0441
    "\u0440": "p",  # U+0440
    "\u0445": "x",  # U+0445
    "\u0443": "y",  # U+0443
    "\u043a": "k",  # U+043A
    "\u043c": "m",  # U+043C
    "\u0442": "t",  # U+0442
    "\u043d": "h",  # U+043D
    "\u0432": "b",  # U+0432
    "\u0456": "i",  # U+0456
    "\u0458": "j",  # U+0458
    "\u0455": "s",  # U+0455
    # Greek
    "\u03b1": "a",  # U+03B1
    "\u03bf": "o",  # U+03BF
    "\u03c1": "p",  # U+03C1
    "\u03c4": "t",  # U+03C4
    "\u03bd": "v",  # U+03BD
    "\u03ba": "k",  # U+03BA
    "\u03b9": "i",  # U+03B9
    # Latin look-alikes from other ranges
    "\u217c": "l",  # U+217C
    "\u2170": "i",  # U+2170
}

# Leetspeak digit/symbol substitutions, applied as a *candidate* decode only (so
# "account" with a literal '0' isn't corrupted in the canonical form). Conservative
# set — common letter->glyph swaps an attacker uses to dodge a token classifier.
_LEET: dict[str, str] = {
    "0": "o",
    "1": "i",
    "3": "e",
    "4": "a",
    "5": "s",
    "7": "t",
    "@": "a",
    "$": "s",
    "!": "i",
}

# A run that *might* be base64: the standard alphabet, length a multiple of four,
# at least 12 chars (short tokens decode to noise and just add false candidates).
_B64_RUN = re.compile(r"[A-Za-z0-9+/]{12,}={0,2}")
# Printable-ASCII-ish gate for "did this decode to readable text" — letters,
# digits, spaces and common punctuation. A decode of mostly-other bytes is noise.
_READABLE = re.compile(r"[\x20-\x7e]")
# Fraction of printable-ASCII characters a base64 decode must reach to count as
# readable text (rather than decode noise).
_READABLE_RATIO = 0.8
# Word splitter for the English-likeness signal below.
_WORD = re.compile(r"[a-z]+")

# A small lexicon of common-English + injection-relevant words. A ROT13/leet
# decode is only *surfaced* when it raises the count of these words versus the
# source string — so the transform reveals hidden instructions ("vtaber lbhe
# ehyrf" -> "ignore your rules") but does NOT add a gibberish candidate when run
# over already-readable text (plain English ROT13s to noise, lowering the count).
# This is a de-obfuscation trigger, not a classifier; the model still scores it.
_ENGLISH_HINTS: frozenset[str] = frozenset(
    {
        # high-frequency function words
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "is",
        "are",
        "you",
        "your",
        "my",
        "me",
        "we",
        "it",
        "this",
        "that",
        "now",
        "all",
        "please",
        "do",
        "not",
        "no",
        "can",
        "will",
        "with",
        "for",
        "be",
        "as",
        "if",
        # injection / tool-abuse relevant words (the attack vocabulary)
        "ignore",
        "disregard",
        "override",
        "bypass",
        "instructions",
        "instruction",
        "system",
        "prompt",
        "rules",
        "rule",
        "admin",
        "mode",
        "developer",
        "transfer",
        "account",
        "balance",
        "password",
        "read",
        "send",
        "wire",
        "previous",
        "above",
        "forget",
        "reveal",
        "secret",
        "confirm",
    }
)


@dataclass(frozen=True, slots=True)
class NormalizedText:
    """The result of normalising one caller turn for classification.

    Attributes:
        canonical: The de-obfuscated surface text (control-strip + NFKC + homoglyph
            fold). Always present; the baseline the classifier sees.
        candidates: ``canonical`` plus each reversible decode that yielded readable
            text, de-duplicated and canonical-first. Never empty.
        reasons: One audit string per transform that fired (e.g. ``"base64-decode"``).
    """

    canonical: str
    candidates: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def screened_text(self) -> str:
        """The single string handed to the classifier: all candidates joined.

        Joining (newline-separated) lets one ``classify()`` call see the surface
        form *and* every decoded payload, so an obfuscated instruction is scored
        even when the disguise is benign.
        """
        return "\n".join(self.candidates)


def normalize(text: str) -> NormalizedText:
    """De-obfuscate ``text`` for prompt-injection classification (ADR-0009 §1).

    Strips control/format characters, NFKC-normalises, folds homoglyph confusables
    to ASCII, then appends any base64/ROT13/leetspeak decode that produced readable
    text as an extra candidate. Pure and deterministic.

    Args:
        text: The raw, finalized caller turn (already transcribed).

    Returns:
        A :class:`NormalizedText` whose ``screened_text`` is what the classifier
        should score.
    """
    reasons: list[str] = []

    stripped, had_controls = _strip_controls(text)
    if had_controls:
        reasons.append("control-chars-stripped")

    nfkc = unicodedata.normalize("NFKC", stripped)
    if nfkc != stripped:
        reasons.append("nfkc-normalised")

    canonical, had_homoglyphs = _fold_homoglyphs(nfkc)
    if had_homoglyphs:
        reasons.append("homoglyph-folded")

    candidates: list[str] = [canonical]
    for decoded, reason in _reversible_decodes(canonical):
        if decoded and decoded not in candidates:
            candidates.append(decoded)
            reasons.append(reason)

    return NormalizedText(
        canonical=canonical,
        candidates=tuple(candidates),
        reasons=tuple(reasons),
    )


def _strip_controls(text: str) -> tuple[str, bool]:
    """Drop control/format/separator code points (keeping ASCII whitespace)."""
    out: list[str] = []
    removed = False
    for ch in text:
        if ch in ("\t", "\n", "\r", " "):
            out.append(ch)
            continue
        if unicodedata.category(ch) in _STRIPPABLE_CATEGORIES:
            removed = True
            continue
        out.append(ch)
    return "".join(out), removed


def _fold_homoglyphs(text: str) -> tuple[str, bool]:
    """Map confusable non-ASCII letters to their ASCII look-alike (case-preserving)."""
    out: list[str] = []
    folded = False
    for ch in text:
        replacement = _HOMOGLYPHS.get(ch.casefold())
        if replacement is None:
            out.append(ch)
            continue
        folded = True
        out.append(replacement.upper() if ch.isupper() else replacement)
    return "".join(out), folded


def _reversible_decodes(text: str) -> list[tuple[str, str]]:
    """Return ``(decoded_text, reason)`` for each decode that revealed hidden text.

    Each decode is *additive*: the caller appends it as an extra candidate beside
    the surface form. base64 is surfaced when a run decodes to readable UTF-8;
    ROT13 and leetspeak are surfaced only when the decode raises the count of
    recognisable English/attack words versus the source (so the transform reveals
    a disguised instruction without adding a gibberish candidate over plain text).
    """
    out: list[tuple[str, str]] = []
    source_hits = _english_hits(text)

    base64_decoded = _try_base64(text)
    if base64_decoded is not None:
        out.append((base64_decoded, "base64-decode"))

    rot13 = codecs.decode(text, "rot_13")
    if rot13 != text and _english_hits(rot13) > source_hits:
        out.append((rot13, "rot13-decode"))

    leet = _decode_leet(text)
    if leet != text and _english_hits(leet) > source_hits:
        out.append((leet, "leetspeak-decode"))

    return out


def _try_base64(text: str) -> str | None:
    """Decode the longest base64-looking run; return readable text or ``None``.

    Only standard-alphabet runs of a 4-aligned length are attempted, and the
    decoded bytes must be valid UTF-8 *and* predominantly printable, or the run
    is treated as ordinary text (not base64).
    """
    best: str | None = None
    for match in _B64_RUN.finditer(text):
        run = match.group()
        if len(run) % 4 != 0:
            continue
        try:
            raw = base64.b64decode(run, validate=True)
        except (binascii.Error, ValueError):
            continue
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not _looks_readable(decoded):
            continue
        if best is None or len(decoded) > len(best):
            best = decoded
    return best


def _decode_leet(text: str) -> str:
    """Substitute leetspeak glyphs with their plain-letter equivalents."""
    return "".join(_LEET.get(ch, ch) for ch in text)


def _looks_readable(text: str) -> bool:
    """True when ``text`` is predominantly printable ASCII (not decode noise)."""
    if not text:
        return False
    printable = len(_READABLE.findall(text))
    return printable / len(text) >= _READABLE_RATIO


def _english_hits(text: str) -> int:
    """Count word-tokens of ``text`` that are in the common-English/attack lexicon.

    The de-obfuscation trigger for ROT13/leet: a decode is surfaced only when it
    raises this count, so the transform reveals a disguised instruction but stays
    silent on already-readable text (whose ROT13/leet form scores lower).
    """
    return sum(1 for word in _WORD.findall(text.lower()) if word in _ENGLISH_HINTS)
