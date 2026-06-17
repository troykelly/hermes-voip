"""Tests for hermes_voip.spoken_text — spoken-text sanitisation before TTS.

Every LLM-generated agent reply passes through ``sanitize_for_speech`` before
synthesis. These tests pin the contract:

* emoji codepoints (and their UCD-adjacent ranges) are stripped and not voiced;
* markdown formatting markup is stripped so it is not literally spoken;
* links ``[text](url)`` are reduced to their anchor text;
* bare URLs are dropped (nothing is voiced for them);
* plain text passes through unchanged;
* whitespace is normalised to single spaces.

The suite is pure string-processing — no model, no I/O.
"""

from __future__ import annotations

import unicodedata

from hermes_voip.spoken_text import sanitize_for_speech

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    # Emoticons
    (0x1F600, 0x1F64F),
    # Misc symbols and pictographs
    (0x1F300, 0x1F5FF),
    # Transport and map symbols
    (0x1F680, 0x1F6FF),
    # Supplemental symbols and pictographs
    (0x1F900, 0x1F9FF),
    (0x1FA00, 0x1FA6F),
    (0x1FA70, 0x1FAFF),
    # Enclosed characters / regional indicators
    (0x1F1E0, 0x1F1FF),
    # Dingbats
    (0x2700, 0x27BF),
    # Misc symbols
    (0x2600, 0x26FF),
    # Variation selectors (16 = U+FE0F)
    (0xFE00, 0xFE0F),
    # Combining enclosing keycap
    (0x20D0, 0x20FF),
    # ZWJ
    (0x200D, 0x200D),
    # Tags block (emoji tag sequences)
    (0xE0000, 0xE007F),
)


def _has_emoji_codepoint(text: str) -> bool:
    """Return True if any character in *text* falls in a known emoji range."""
    for ch in text:
        cp = ord(ch)
        for lo, hi in _EMOJI_RANGES:
            if lo <= cp <= hi:
                return True
        # Also check Unicode category: So (Symbol, Other) covers many pictographs
        if unicodedata.category(ch) == "So":
            return True
    return False


# ---------------------------------------------------------------------------
# Emoji stripping
# ---------------------------------------------------------------------------


class TestEmojiStripping:
    def test_single_emoji_removed(self) -> None:
        result = sanitize_for_speech("Sure! \U0001f60a")
        assert not _has_emoji_codepoint(result)
        assert "Sure" in result

    def test_multiple_emoji_removed(self) -> None:
        msg = "Sure! \U0001f60a\U0001f44d Here's the plan: step one."
        result = sanitize_for_speech(msg)
        assert not _has_emoji_codepoint(result)
        assert "Here" in result
        assert "plan" in result

    def test_emoji_between_words_no_double_space(self) -> None:
        result = sanitize_for_speech("Great \U0001f44d job")
        assert "  " not in result
        assert "Great" in result
        assert "job" in result

    def test_variation_selector_stripped(self) -> None:
        # U+FE0F is an emoji variation selector
        result = sanitize_for_speech("❤️ love")
        assert not _has_emoji_codepoint(result)

    def test_flag_emoji_stripped(self) -> None:
        # Regional indicator letters form flag emoji (U+1F1E6..U+1F1FF)
        result = sanitize_for_speech("\U0001f1e6\U0001f1fa flag")
        assert not _has_emoji_codepoint(result)

    def test_dingbat_stripped(self) -> None:
        # U+2713 CHECK MARK is in dingbats / misc symbols range
        result = sanitize_for_speech("✓ Done")
        assert not _has_emoji_codepoint(result)

    def test_skin_tone_modifier_stripped(self) -> None:
        # U+1F3FB is EMOJI MODIFIER FITZPATRICK TYPE-1-2
        result = sanitize_for_speech("Hello \U0001f44b\U0001f3fb!")
        assert not _has_emoji_codepoint(result)
        assert "Hello" in result

    def test_zwj_sequence_stripped(self) -> None:
        # Family ZWJ sequence: man + ZWJ + woman + ZWJ + girl
        result = sanitize_for_speech("\U0001f468‍\U0001f469‍\U0001f467 family")
        assert not _has_emoji_codepoint(result)
        assert "family" in result

    def test_plain_text_unchanged(self) -> None:
        text = "Hello, how are you today?"
        assert sanitize_for_speech(text) == text

    def test_no_emoji_no_change_to_words(self) -> None:
        text = "The quick brown fox jumps over the lazy dog."
        result = sanitize_for_speech(text)
        assert result == text


# ---------------------------------------------------------------------------
# Markdown stripping
# ---------------------------------------------------------------------------


class TestMarkdownStripping:
    def test_bold_asterisk_stripped(self) -> None:
        result = sanitize_for_speech("Here is **important** text.")
        assert "*" not in result
        assert "important" in result

    def test_italic_underscore_stripped(self) -> None:
        result = sanitize_for_speech("This is _italic_ text.")
        assert "_" not in result
        assert "italic" in result

    def test_heading_hash_stripped(self) -> None:
        result = sanitize_for_speech("## Section Header")
        assert "#" not in result
        assert "Section" in result

    def test_inline_code_backtick_stripped(self) -> None:
        result = sanitize_for_speech("Use `print()` to output.")
        assert "`" not in result
        assert "print" in result

    def test_blockquote_stripped(self) -> None:
        result = sanitize_for_speech("> This is a quote.")
        assert ">" not in result
        assert "This is a quote" in result

    def test_bullet_marker_removed(self) -> None:
        # Bullet point dash/asterisk at start of line
        result = sanitize_for_speech("Steps:\n- First step\n- Second step")
        assert "First step" in result
        assert "Second step" in result

    def test_link_becomes_anchor_text(self) -> None:
        result = sanitize_for_speech("See [docs](https://x.test) for details.")
        assert "docs" in result
        assert "https" not in result
        assert "[" not in result
        assert "]" not in result
        assert "(" not in result
        assert ")" not in result

    def test_complex_link_anchor_only(self) -> None:
        result = sanitize_for_speech(
            "[click here](https://example.test/foo/bar?a=1&b=2)"
        )
        assert result.strip() == "click here"

    def test_link_nested_in_sentence(self) -> None:
        result = sanitize_for_speech(
            "Visit [our site](https://example.test) to learn more."
        )
        assert "our site" in result
        assert "https" not in result

    def test_bold_and_emoji_together(self) -> None:
        msg = "Sure! \U0001f60a\U0001f44d Here's the **plan**: step one."
        result = sanitize_for_speech(msg)
        assert not _has_emoji_codepoint(result)
        assert "*" not in result
        assert "plan" in result

    def test_code_fence_stripped(self) -> None:
        result = sanitize_for_speech("```python\nprint('hello')\n```")
        assert "```" not in result


# ---------------------------------------------------------------------------
# URL stripping
# ---------------------------------------------------------------------------


class TestUrlStripping:
    def test_bare_https_url_dropped(self) -> None:
        result = sanitize_for_speech("See https://example.test for info.")
        assert "https" not in result
        assert "example.test" not in result

    def test_bare_http_url_dropped(self) -> None:
        result = sanitize_for_speech("Go to http://old.example.test.")
        assert "http" not in result

    def test_text_around_url_preserved(self) -> None:
        result = sanitize_for_speech("Read https://docs.example.test to learn more.")
        assert "Read" in result
        assert "learn more" in result

    def test_url_only_string_becomes_empty_or_whitespace(self) -> None:
        result = sanitize_for_speech("https://example.test/some/path?q=1")
        assert result.strip() == ""


# ---------------------------------------------------------------------------
# Whitespace normalisation
# ---------------------------------------------------------------------------


class TestWhitespaceNormalisation:
    def test_newlines_become_spaces(self) -> None:
        result = sanitize_for_speech("Line one.\nLine two.")
        assert "\n" not in result
        assert "Line one" in result
        assert "Line two" in result

    def test_multiple_spaces_collapsed(self) -> None:
        result = sanitize_for_speech("word   word")
        assert "  " not in result

    def test_leading_trailing_stripped(self) -> None:
        result = sanitize_for_speech("  hello  ")
        assert result == "hello"

    def test_mixed_whitespace_collapsed(self) -> None:
        result = sanitize_for_speech("a\t\n  b")
        assert result == "a b"


# ---------------------------------------------------------------------------
# End-to-end integration scenario
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_rich_llm_reply(self) -> None:
        """Typical rich LLM reply → clean spoken text."""
        reply = (
            "Sure! \U0001f60a\U0001f44d Here's the **plan**:\n"
            "- Step one: go to [the docs](https://docs.example.test).\n"
            "- Step two: read `README.md`.\n"
            "Let me know if you have questions! \U0001f64f"
        )
        result = sanitize_for_speech(reply)
        assert not _has_emoji_codepoint(result)
        assert "*" not in result
        assert "`" not in result
        assert "https" not in result
        assert "[" not in result
        assert "]" not in result
        assert "  " not in result
        assert "plan" in result
        assert "Step one" in result
        assert "the docs" in result
        assert "README" in result

    def test_plain_sentence_unchanged(self) -> None:
        text = "The weather today is sunny with a high of 22 degrees."
        assert sanitize_for_speech(text) == text

    def test_punctuation_aids_prosody_preserved(self) -> None:
        text = "Wait, really? Yes! That's correct."
        result = sanitize_for_speech(text)
        assert "," in result
        assert "?" in result
        assert "!" in result
        assert "." in result

    def test_repeated_punctuation_collapsed(self) -> None:
        result = sanitize_for_speech("Wow!!! Great job!!!")
        # Multiple identical punctuation marks should be collapsed to one
        assert "!!!" not in result
