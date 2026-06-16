"""Test suite for hermes-voip.

This package marker makes the test tree import under a single stable package root
(``tests``), so cross-directory test helpers can be imported by their full path
(e.g. ``tests.transport._loopback``, ``tests.e2e._fake_gateway``) without mypy
seeing the same file under two module names (``transport._loopback`` via the
implicit-namespace fallback *and* ``tests.transport._loopback``).
"""
