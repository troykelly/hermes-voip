"""Tests for the blast-radius classifier.

The property that matters is that the classifier never *under*-classifies: a change
may be gated more strictly than it strictly needs, never less. Every test here is
written from that direction.
"""

from __future__ import annotations

import pytest
from tools.classify import Class, classify, classify_paths, main


class TestSelfReference:
    """The policy, the classifier and the machinery that runs them are R2."""

    @pytest.mark.parametrize(
        "path",
        [
            "tools/classify.py",
            "tests/test_classify.py",
            "AGENTS.md",
            "CLAUDE.md",
            "docs/standards/engineering.md",
            ".github/workflows/gate.yml",
            ".github/CODEOWNERS",
            ".claude/settings.json",
            ".claude/hooks/enforce-worktree.mjs",
            ".claude/hooks/prefer-native-tools.mjs",
            ".claude/workflows/wave.workflow.js",
            ".claude/skills/orchestrate/tick.sh",
            "orchestrate.config.json",
            ".pre-commit-config.yaml",
        ],
    )
    def test_agent_policy_surface_is_r2(self, path: str) -> None:
        assert classify_paths([path]) is Class.R2

    def test_a_change_cannot_lower_its_own_class(self) -> None:
        # Editing the classifier alongside a trivial doc change is still R2.
        assert classify_paths(["tools/classify.py", "docs/backlog.md"]) is Class.R2


class TestApplyTriggers:
    """A release tag publishes from these, so they carry the publish's blast radius."""

    @pytest.mark.parametrize(
        "path",
        [
            "pyproject.toml",
            "uv.lock",
            "packaging/hermes-plugins/hermes-voip/plugin.yaml",
        ],
    )
    def test_apply_trigger_paths_are_at_least_r2(self, path: str) -> None:
        assert classify_paths([path]) >= Class.R2

    def test_publish_workflow_is_r3(self) -> None:
        assert classify_paths([".github/workflows/publish.yml"]) is Class.R3


class TestR3:
    @pytest.mark.parametrize(
        "path",
        [
            "LICENSE",
            "NOTICE",
            "THIRD_PARTY_NOTICES.md",
            ".gitleaks.toml",
            "src/hermes_voip/digest.py",
        ],
    )
    def test_credential_and_legal_surfaces_are_r3(self, path: str) -> None:
        assert classify_paths([path]) is Class.R3


class TestProductSurface:
    @pytest.mark.parametrize(
        "path",
        [
            "src/hermes_voip/adapter.py",
            "src/hermes_voip/sdp.py",
            "src/hermes_voip/config.py",
            "src/hermes_voip/plugin.yaml",
            "src/hermes_voip/transport/tls.py",
            "src/hermes_voip/media/jitter.py",
            "src/hermes_voip/guard/injection.py",
            "src/hermes_voip/dtmf_confirm.py",
            "src/hermes_voip/providers/cartesia.py",
            "src/hermes_voip/voip_tools.py",
        ],
    )
    def test_signalling_media_and_config_are_r2(self, path: str) -> None:
        assert classify_paths([path]) is Class.R2

    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_plugin_manifest.py",
            "tests/test_workflow_action_pins.py",
            "tests/test_supply_chain_schedule.py",
            "tests/test_no_vendor_identifiers.py",
        ],
    )
    def test_safety_tests_are_r2(self, path: str) -> None:
        assert classify_paths([path]) is Class.R2


class TestDefaults:
    @pytest.mark.parametrize(
        "path",
        [
            "src/hermes_voip/spoken_text.py",
            "src/hermes_voip/_chars.py",
            "tests/test_spoken_text.py",
            "docs/adr/0114-something.md",
            "docs/runbooks/0020-something.md",
            "README.md",
            "some/brand/new/file.py",
        ],
    )
    def test_unmatched_paths_default_to_r1(self, path: str) -> None:
        assert classify_paths([path]) is Class.R1

    def test_an_empty_change_is_r0(self) -> None:
        assert classify_paths([]) is Class.R0

    def test_most_severe_path_wins(self) -> None:
        paths = ["docs/backlog.md", "src/hermes_voip/spoken_text.py", "LICENSE"]
        assert classify_paths(paths) is Class.R3


class TestR0IsAllowlistOnly:
    """R0 is decided by an explicit allowlist, never by diff content.

    A content-based downgrade (treat a diff of only ``#`` lines as R0) was implemented
    and removed after adversarial review: a ``#`` line can be inside a docstring or a
    multi-line string, and ``# ruff: noqa`` / ``# mypy: ignore-errors`` are whole-file
    policy directives that disable a checker. Deciding that needs the file, not the
    diff line. These tests pin the replacement invariant — content NEVER lowers a
    class — and keep the reported triggers as regressions.
    """

    @pytest.mark.parametrize("path", ["docs/backlog.md", "docs/plan/ROADMAP.md"])
    def test_allowlisted_non_executable_files_are_r0(self, path: str) -> None:
        assert classify_paths([path]) is Class.R0

    def test_changelog_is_not_r0_because_a_release_publishes_it(self) -> None:
        # publish.yml lifts the `## [X.Y.Z]` body into `gh release create --notes-file`,
        # so its text is published externally and cannot be an unreviewed R0 edit.
        assert classify_paths(["CHANGELOG.md"]) > Class.R0

    def test_a_lint_suppression_directive_is_not_downgraded(self) -> None:
        diff = ["+# ruff: noqa"]
        assert classify(["src/hermes_voip/spoken_text.py"], diff) is Class.R1

    def test_a_typechecker_suppression_directive_is_not_downgraded(self) -> None:
        diff = ["+# mypy: ignore-errors"]
        assert classify(["src/hermes_voip/spoken_text.py"], diff) is Class.R1

    def test_a_hash_line_inside_a_string_constant_is_not_downgraded(self) -> None:
        # Indistinguishable from a comment at the diff-line level; must stay R1.
        diff = ["-#!/bin/sh", "+#!/bin/bash --posix"]
        assert classify(["src/hermes_voip/aio.py"], diff) is Class.R1

    def test_comment_only_change_to_an_r2_file_stays_r2(self) -> None:
        diff = ["+# just a comment"]
        assert classify(["src/hermes_voip/adapter.py"], diff) is Class.R2

    def test_diff_content_never_lowers_the_path_derived_class(self) -> None:
        for path, expected in [
            ("src/hermes_voip/spoken_text.py", Class.R1),
            ("src/hermes_voip/adapter.py", Class.R2),
            ("src/hermes_voip/digest.py", Class.R3),
        ]:
            for diff in ([], ["+# c"], ["+"], ["+    return None"]):
                assert classify([path], diff) is expected


class TestCredentialAndActuationSurfaces:
    """Found by adversarial review: these were falling through to R1."""

    @pytest.mark.parametrize(
        "path", ["src/hermes_voip/intercom.py", "src/hermes_voip/multi_intercom.py"]
    )
    def test_bearer_token_relay_modules_are_r3(self, path: str) -> None:
        assert classify_paths([path]) is Class.R3

    def test_the_open_entry_grant_policy_module_is_at_least_r2(self) -> None:
        assert classify_paths(["src/hermes_voip/tools.py"]) >= Class.R2


class TestGlobSemantics:
    def test_double_star_matches_nested_paths(self) -> None:
        assert classify_paths([".claude/skills/adr/SKILL.md"]) is Class.R2
        assert classify_paths(["src/hermes_voip/media/deep/nested/mod.py"]) is Class.R2

    def test_double_star_matches_the_directory_itself(self) -> None:
        assert classify_paths(["packaging"]) is Class.R2

    def test_a_similarly_named_sibling_directory_is_not_matched(self) -> None:
        # "packaging_notes/x.md" must not match the "packaging/**" rule.
        assert classify_paths(["packaging_notes/x.md"]) is Class.R1


class TestDeterminism:
    def test_repeated_calls_agree(self) -> None:
        paths = ["src/hermes_voip/adapter.py", "docs/backlog.md", "README.md"]
        results = {classify_paths(paths) for _ in range(5)}
        assert results == {Class.R2}

    def test_path_order_does_not_change_the_result(self) -> None:
        a = ["LICENSE", "docs/backlog.md", "src/hermes_voip/adapter.py"]
        assert classify_paths(a) is classify_paths(list(reversed(a)))


class TestMalformedInputFailsLoudly:
    """A silent under-classification is the one outcome worth crashing over.

    zsh does not word-split an unquoted expansion, so ``--paths $files`` passes a
    whole newline-separated list as one argument. That argument matches no pattern
    and would otherwise classify a real R2 change as R1.
    """

    def test_a_newline_joined_path_list_raises(self) -> None:
        joined = "src/hermes_voip/sdp.py\ntests/test_sdp.py"
        with pytest.raises(ValueError, match="under-classify"):
            classify_paths([joined])

    def test_the_same_paths_passed_properly_are_r2(self) -> None:
        assert (
            classify_paths(["src/hermes_voip/sdp.py", "tests/test_sdp.py"]) is Class.R2
        )


class TestCli:
    def test_explicit_paths_mode_prints_the_class(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--paths", "src/hermes_voip/adapter.py"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("R2\n")
        assert "src/hermes_voip/adapter.py" in out

    def test_empty_paths_mode_is_r0(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--paths"]) == 0
        assert capsys.readouterr().out.startswith("R0\n")
