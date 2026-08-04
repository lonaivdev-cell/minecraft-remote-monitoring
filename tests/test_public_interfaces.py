"""Public interfaces that must survive the LULiSM rename unchanged.

Prometheus metric names are queried by Grafana panels and alert rules; config
section names are part of the agent schema. Renaming either is a behaviour
change, which the 2.0.0 rename explicitly forbids. If a test here fails, do not
update the constant — revert whatever renamed the interface.
"""

from __future__ import annotations

from mcctl import prometheus
from mcctl.config import Config

EXPECTED_METRICS = {
    "mcctl_up",
    "mcctl_players",
    "mcctl_tps",
    "mcctl_mspt_milliseconds",
    "mcctl_heap_used_bytes",
    "mcctl_heap_max_bytes",
    "mcctl_host_mem_used_bytes",
    "mcctl_host_mem_total_bytes",
    "mcctl_disk_free_bytes",
    "mcctl_load1",
    "mcctl_log_age_seconds",
    "mcctl_watchdog_restarts_total",
    "mcctl_scrape_timestamp_seconds",
}

EXPECTED_CONFIG_SECTIONS = ("server", "backup", "watchdog", "metrics", "llm", "ui", "crafting")


def test_prometheus_metric_names_are_frozen():
    # render() emits "# TYPE <name> <type>" for every series, even when the
    # sample lacks that key, so an empty sample still lists all of them.
    text = prometheus.render({}, host="box", restarts=0, now=1700.0)
    names = {ln.split()[2] for ln in text.splitlines() if ln.startswith("# TYPE ")}
    assert names == EXPECTED_METRICS


def test_config_section_names_are_frozen():
    assert tuple(Config().to_dict()) == EXPECTED_CONFIG_SECTIONS
