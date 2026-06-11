"""tracer: jstat -gcutil parsing and GC delta computation (pure)."""

from mcctl import tracer

HEADER = "  S0     S1     E      O      M     CCS    YGC     YGCT    FGC    FGCT     CGC    CGCT      GCT"
ROW1 = "  0.00  31.25  18.30  45.10  95.20  92.10   100    45.678     5    8.901      0    0.000    54.579"
ROW2 = "  0.00  12.00  3.10   46.00  95.30  92.10   102    45.700     6    9.001      0    0.000    54.701"


def test_is_header():
    assert tracer.is_header(HEADER)
    assert not tracer.is_header(ROW1)
    assert not tracer.is_header("")


def test_parse_row_maps_names():
    names = HEADER.split()
    row = tracer.parse_row(names, ROW1)
    assert row is not None
    assert row["YGC"] == 100.0
    assert row["O"] == 45.10
    assert row["E"] == 18.30
    # wrong column count -> not a data row
    assert tracer.parse_row(names, "1 2 3") is None


def test_delta_detects_young_and_full_gc():
    names = HEADER.split()
    a = tracer.parse_row(names, ROW1)
    b = tracer.parse_row(names, ROW2)
    d = tracer.delta(a, b)
    assert d.young_gcs == 2                       # 100 -> 102
    assert round(d.young_pause_ms, 1) == 22.0     # (45.700 - 45.678) * 1000
    assert d.full_gcs == 1                         # 5 -> 6
    assert round(d.full_pause_ms) == 100           # (9.001 - 8.901) * 1000
    assert d.old_pct == 46.0
    assert d.collected


def test_delta_quiet_interval():
    names = HEADER.split()
    a = tracer.parse_row(names, ROW1)
    d = tracer.delta(a, a)
    assert d.young_gcs == 0 and d.full_gcs == 0 and not d.collected
