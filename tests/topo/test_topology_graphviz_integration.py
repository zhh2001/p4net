"""Integration tests for `Topology.render_graphviz`.

These run only if the graphviz `dot` binary is available on PATH. Unlike
the BMv2 / p4c suites these don't need root, and they aren't gated by a
``--run-*`` flag — they're pure local subprocess invocations.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from p4net.topo import Topology

GRAPHVIZ = shutil.which("dot")
pytestmark = pytest.mark.skipif(GRAPHVIZ is None, reason="graphviz `dot` not installed")


def _build_topo() -> Topology:
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24", ip6="fd00::1/64")
    t.add_host("h2", ip="10.0.0.2/24")
    t.add_switch("s1", Path("p.p4"), grpc_port=50051)
    t.add_link("h1", "s1", port_b=1)
    t.add_link("h2", "s1", port_b=2)
    return t


def test_render_png(tmp_path: Path) -> None:
    out = tmp_path / "topo.png"
    _build_topo().render_graphviz(out, format="png")
    assert out.stat().st_size > 0
    # PNG magic: 89 50 4E 47 0D 0A 1A 0A.
    head = out.read_bytes()[:8]
    assert head == b"\x89PNG\r\n\x1a\n"


def test_render_svg(tmp_path: Path) -> None:
    out = tmp_path / "topo.svg"
    _build_topo().render_graphviz(out, format="svg")
    text = out.read_text(encoding="utf-8", errors="replace")
    assert text.startswith("<?xml")


def test_render_dot(tmp_path: Path) -> None:
    out = tmp_path / "topo.dot"
    _build_topo().render_graphviz(out, format="dot")
    text = out.read_text()
    assert text.startswith("digraph p4net")
    assert '"h1"' in text
    assert '"s1"' in text
