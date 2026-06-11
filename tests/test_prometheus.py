"""Prometheus textfile exporter: pure render + atomic export."""

from __future__ import annotations

from mcctl import metrics, prometheus
from mcctl.config import Config


def _parse(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name, _, val = line.partition(" ")
        out[name] = val
    return out


def test_render_emits_expected_series():
    sample = {"running": True, "players": 3, "tps": 19.8, "mspt": 11.2,
              "heap_used": 4_000_000_000, "heap_max": 8_000_000_000,
              "mem_used": 6_000_000_000, "mem_total": 16_000_000_000,
              "disk_free": 50_000_000_000, "load1": 1.5, "log_age": 4}
    text = prometheus.render(sample, host="box", restarts=2, now=1700.0)
    m = _parse(text)
    assert m['mcctl_up{host="box"}'] == "1"
    assert m['mcctl_players{host="box"}'] == "3"
    assert m['mcctl_tps{host="box"}'] == "19.8"
    assert m['mcctl_heap_used_bytes{host="box"}'] == "4000000000"
    assert m['mcctl_watchdog_restarts_total{host="box"}'] == "2"
    assert m['mcctl_scrape_timestamp_seconds{host="box"}'] == "1700.0"
    # HELP/TYPE present for every series
    assert text.count("# TYPE") >= 12


def test_render_down_and_missing_fields():
    text = prometheus.render({"running": False}, host="", now=1.0)
    m = _parse(text)
    assert m["mcctl_up"] == "0"
    # a missing field gets HELP/TYPE but no value line
    assert "mcctl_tps" not in m
    assert "# TYPE mcctl_tps gauge" in text


def test_render_no_host_label():
    text = prometheus.render({"running": True}, host="")
    assert "mcctl_up 1" in text  # no {host=...}


def test_export_writes_atomically(tmp_path):
    metrics.append_sample({"running": True, "players": 1, "tps": 20.0})
    cfg = Config()
    cfg.server.transport = "local"
    out = tmp_path / "mcctl.prom"
    p = prometheus.export(cfg, out=out)
    assert p == out
    text = out.read_text()
    assert "mcctl_up" in text and "mcctl_players" in text
    assert not (tmp_path / "mcctl.prom.tmp").exists()  # tmp renamed away


def test_export_with_no_samples_reports_down(tmp_path):
    cfg = Config()
    cfg.server.transport = "local"
    out = tmp_path / "m.prom"
    prometheus.export(cfg, out=out)
    assert "mcctl_up local 0" in out.read_text() or "mcctl_up{host=\"local\"} 0" in out.read_text()
