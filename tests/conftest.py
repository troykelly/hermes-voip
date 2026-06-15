"""Shared pytest configuration.

Ensures the optional ``ml`` extra's sherpa-onnx native module is loadable before
any provider test imports it (see :mod:`hermes_voip.providers.onnx_compat`). This
is a no-op under the default gate where the ``ml`` extra is not installed.
"""

from __future__ import annotations

import pytest

from hermes_voip.providers.onnx_compat import ensure_sherpa_loadable


def pytest_configure(config: pytest.Config) -> None:
    """Make sherpa-onnx loadable at session start (no-op without the ml extra)."""
    ensure_sherpa_loadable()
