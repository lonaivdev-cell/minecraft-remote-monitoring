"""charts: sparkline / block chart / summary — pure, no I/O."""

from mcctl import charts


def test_sparkline_maps_range_and_handles_empty():
    assert charts.sparkline([], 0, 20) == "no data yet"
    assert charts.sparkline([None, None], 0, 20) == "no data yet"
    line = charts.sparkline([0, 10, 20], 0, 20)
    assert line[0] == charts.SPARK_CHARS[0]      # min -> lowest glyph
    assert line[-1] == charts.SPARK_CHARS[-1]    # max -> highest glyph


def test_sparkline_clamps_out_of_range():
    line = charts.sparkline([-5, 99], 0, 20)
    assert line[0] == charts.SPARK_CHARS[0]
    assert line[-1] == charts.SPARK_CHARS[-1]


def test_block_chart_dimensions_and_gaps():
    rows = charts.block_chart([0, 5, 10, None, 20], lo=0, hi=20, width=60, height=4)
    assert len(rows) == 4
    assert all(len(r) == 5 for r in rows)        # one column per value
    # the None column is blank in every row
    assert all(r[3] == " " for r in rows)
    # the max-value column is full in the bottom row
    assert rows[-1][4] == charts._BLOCKS[-1]


def test_summarize():
    s = charts.summarize([2.0, None, 4.0, 6.0])
    assert s.n == 3
    assert s.min == 2.0 and s.max == 6.0 and s.avg == 4.0 and s.last == 6.0
    empty = charts.summarize([None, None])
    assert empty.n == 0 and empty.last is None
