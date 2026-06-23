# Changelog

All notable changes to `hermes-voip` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The package version is **single-sourced** from `pyproject.toml [project].version`
(see [docs/runbooks/0019-release-process.md](docs/runbooks/0019-release-process.md));
`hermes_voip.__version__` and the `plugin.yaml` manifest version track it and are
pinned equal by the test suite.

## [Unreleased]

### Changed

- Version is now single-sourced. `hermes_voip.__version__` derives from the
  installed distribution metadata (`importlib.metadata.version("hermes-voip")`,
  populated from `pyproject.toml [project].version`) instead of a hand-maintained
  literal. The test suite pins `pyproject.toml`, `__version__`, and the
  `plugin.yaml` manifest version equal, so a release is a single edit in
  `pyproject.toml`.

### Added

- Release-process runbook
  ([docs/runbooks/0019-release-process.md](docs/runbooks/0019-release-process.md)):
  the exact, verified steps to cut a release — bump the version, run the
  version-sync tests, update this changelog, tag, `uv build`, and verify the wheel
  installs and ships the plugin manifest.
- This `CHANGELOG.md`.

## [0.1.0] - Unreleased

First tagged release of the `hermes-voip` Hermes plugin: two-way voice over
telephony on any RFC-compliant SIP-over-TLS or WebRTC voice gateway. This section
is the staging area for the `0.1.0` release notes; move entries up from
`[Unreleased]` as the release is cut and set the date when the tag is created.

[Unreleased]: https://github.com/troykelly/hermes-voip/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/troykelly/hermes-voip/releases/tag/v0.1.0
