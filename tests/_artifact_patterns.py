"""Regex patterns identifying test-created namespaces and veth interfaces.

These are referenced from `tests/conftest.py`'s session-level cleanup
fixture and from the fixture's smoke test in
`tests/test_cleanup_fixture_smoke.py`. Centralising them here avoids the
implicit dependency on pytest collection order that an
``import tests.conftest`` style would impose, and keeps both consumers
pinned to the same regexes.

Patterns we own and may purge:

- phase-1 runtime integration: ``nsXXXXXXXX`` (8 hex), ``nsA_XXXXXXXX``,
  ``nsB_XXXXXXXX``, plus veth ifaces ``v[A-D]_XXXXXXXX``.
- phase-6 orchestrator integration: hosts ``h<6hex>[abc]?`` and switches
  ``s<6hex>[abc]?``, plus their auto-generated ifaces
  ``<name>-eth<port>``.

We deliberately do NOT match generic short names like ``h1``, ``s1``,
``switch0``, or any iface without the random-hex suffix, so user-managed
namespaces survive a test run unscathed.
"""

from __future__ import annotations

import re

TEST_NS_PATTERN = re.compile(
    r"^("
    r"ns([A-Z]_)?[0-9a-f]{8}"
    r"|[hs][0-9a-f]{6}[abc]?"
    r")$"
)

TEST_IFACE_PATTERN = re.compile(
    r"^("
    r"v[A-D]_[0-9a-f]{8}"
    r"|[hs][0-9a-f]{6}[abc]?-eth[0-9]+"
    r")$"
)
