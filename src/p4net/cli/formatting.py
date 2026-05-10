"""Tiny text-formatting helpers shared by the CLI dispatcher and shell."""

from __future__ import annotations

_ESC = "\x1b["


def bold(text: str, *, color: bool) -> str:
    return f"{_ESC}1m{text}{_ESC}0m" if color else text


def red(text: str, *, color: bool) -> str:
    return f"{_ESC}31m{text}{_ESC}0m" if color else text


def render_pingall_matrix(
    hosts: list[str],
    result: dict[tuple[str, str], bool],
    *,
    color: bool = False,
) -> str:
    """Render a square `pingall` matrix:

    H \\ H  h1   h2
    h1     -    1
    h2     X    -
    N/M succeeded
    """
    if not hosts:
        return "(no hosts to ping)"
    cell_w = max(3, max(len(h) for h in hosts))
    lines: list[str] = []
    header_label = bold("H \\ H".ljust(cell_w), color=color)
    header = header_label + " " + " ".join(h.ljust(cell_w) for h in hosts)
    lines.append(header)
    success = 0
    total = 0
    for src in hosts:
        cells: list[str] = []
        for dst in hosts:
            if src == dst:
                cells.append("-".ljust(cell_w))
                continue
            ok = result.get((src, dst))
            if ok is None:
                cells.append("?".ljust(cell_w))
                continue
            total += 1
            if ok:
                success += 1
                cells.append("1".ljust(cell_w))
            else:
                cells.append("X".ljust(cell_w))
        lines.append(src.ljust(cell_w) + " " + " ".join(cells))
    lines.append(f"{success}/{total} succeeded")
    return "\n".join(lines)
