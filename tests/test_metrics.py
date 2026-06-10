import pytest

from mcctl import metrics
from mcctl.config import Config

HEAP_INFO_J17 = """\
4242:
 garbage-first heap   total 12582912K, used 6196224K [0x0000000300000000, 0x0000000600000000)
  region size 4096K, 38 young (155648K), 4 survivors (16384K)
 Metaspace       used 401234K, committed 412345K, reserved 1449984K
"""

HEAP_INFO_J21 = """\
4242:
 garbage-first heap   total reserved 12582912K, committed 8388608K, used 3145728K [0x...)
  region size 4096K, 12 young (49152K), 2 survivors (8192K)
 Metaspace       used 401234K, committed 412345K, reserved 1449984K
"""


def test_parse_heap_info_classic():
    used, total = metrics.parse_heap_info(HEAP_INFO_J17)
    assert used == 6196224 * 1024
    assert total == 12582912 * 1024


def test_parse_heap_info_reserved_committed():
    used, committed = metrics.parse_heap_info(HEAP_INFO_J21)
    assert used == 3145728 * 1024
    assert committed == 8388608 * 1024  # committed, not reserved: what the JVM holds


def test_parse_heap_info_garbage():
    assert metrics.parse_heap_info("no heap here") is None


@pytest.mark.parametrize("pct,needle", [
    (80.0, "no leak"),
    (35.0, "no leak"),
    (25.0, "watch the trend"),
    (14.9, "retained set is real"),
    (0.0, "retained set is real"),
])
def test_verdicts(pct, needle):
    assert needle in metrics.verdict_for(pct)


def test_purge_report_math():
    rep = metrics.PurgeReport(before_used=10 * 1024**3, after_used=4 * 1024**3,
                              committed=12 * 1024**3)
    assert rep.freed == 6 * 1024**3
    assert rep.freed_pct == 60.0
    assert "no leak" in rep.verdict
    d = rep.to_dict()
    assert d["freed_pct"] == 60.0


def test_purge_flow(fake_t, monkeypatch):
    cfg = Config()
    fake_t.expect("GC.heap_info", out=HEAP_INFO_J17)
    fake_t.expect("GC.run", rc=0)
    monkeypatch.setattr(metrics.time, "sleep", lambda s: None)  # purge waits 3s for the GC cycle
    rep = metrics.purge(fake_t, cfg, 4242)
    assert rep.before_used == 6196224 * 1024
    assert fake_t.calls_matching("GC.run")


def test_samples_roundtrip():
    metrics.append_sample({"ts": 1, "tps": 19.5})
    metrics.append_sample({"ts": 2, "tps": 20.0})
    samples = metrics.read_samples(10)
    assert [s["ts"] for s in samples] == [1, 2]
    assert metrics.read_samples(1)[0]["ts"] == 2
