"""Tiny terminal charting: sparklines, vertical block charts, series summaries.

Pure functions over lists of `float | None` (None = a gap / no sample). Shared by
the `mcctl history` CLI view and the GTK history page's text fallback, and kept
dependency-free so it is trivially unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass

SPARK_CHARS = "▁▂▃▄▅▆▇█"
# 9 levels (space .. full block) for the top, partially-filled cell of a column.
_BLOCKS = " ▁▂▃▄▅▆▇█"


def _present(values: list[float | None]) -> list[float]:
    return [v for v in values if v is not None]


def sparkline(values: list[float | None], lo: float, hi: float, width: int = 40) -> str:
    """One-line sparkline of the last `width` values mapped onto SPARK_CHARS."""
    vals = values[-width:]
    if not _present(vals):
        return "no data yet"
    span = max(hi - lo, 1e-9)
    out = []
    for v in vals:
        if v is None:
            out.append(" ")
            continue
        idx = int((max(lo, min(hi, v)) - lo) / span * (len(SPARK_CHARS) - 1))
        out.append(SPARK_CHARS[idx])
    return "".join(out)


def block_chart(values: list[float | None], *, lo: float, hi: float,
                width: int = 60, height: int = 8) -> list[str]:
    """A `height`-row vertical bar chart of the last `width` values.

    Returns the rows top-to-bottom (each `len`-of-displayed-values wide). Each
    column's height is value-proportional, using partial block glyphs for the
    fractional top cell; gaps (None) render as blank columns."""
    vals = values[-width:]
    span = max(hi - lo, 1e-9)
    # fractional column height in [0, height] for each sample (None -> -1 = gap)
    cols = [-1.0 if v is None else max(0.0, min(1.0, (v - lo) / span)) * height for v in vals]
    rows: list[str] = []
    for r in range(height - 1, -1, -1):       # top row first
        cells = []
        for col in cols:
            if col < 0:
                cells.append(" ")
                continue
            cell = col - r                     # how much of this row the bar fills
            if cell >= 1:
                cells.append(_BLOCKS[-1])
            elif cell <= 0:
                cells.append(" ")
            else:
                cells.append(_BLOCKS[round(cell * 8)])
        rows.append("".join(cells))
    return rows


@dataclass(slots=True)
class Summary:
    n: int          # number of present (non-None) samples
    min: float | None
    max: float | None
    avg: float | None
    last: float | None


def summarize(values: list[float | None]) -> Summary:
    present = _present(values)
    if not present:
        return Summary(0, None, None, None, None)
    last = next((v for v in reversed(values) if v is not None), None)
    return Summary(len(present), min(present), max(present), sum(present) / len(present), last)
