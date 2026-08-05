"""Public interfaces that must survive the LULiSM rename unchanged.

Prometheus metric names are queried by Grafana panels and alert rules; config
section names are part of the agent schema. Renaming either is a behaviour
change, which the 2.0.0 rename explicitly forbids. If a test here fails, do not
update the constant — revert whatever renamed the interface.
"""

from __future__ import annotations

import re

from conftest import package_source_tree

from lulism import prometheus
from lulism.config import Config

# The tree these guards audit: src/lulism in this checkout. `lulism.__file__`
# would instead be whichever copy is importable, which is the same directory
# only for an editable install — see conftest.package_source_tree().
PKG = package_source_tree()

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


def test_agent_hello_version_key_is_frozen():
    """`mcctl_version` is a wire-contract key, not prose.

    android/core/.../Models.kt:225 deserializes it as `mcctlVersion` and two
    screens display it. android/ is frozen for this release, so renaming the key
    would blank the version on every installed phone. The golden schema does not
    cover it — agent.hello's payload is a response, not part of build_schema().
    """
    from lulism import agent

    srv = agent.AgentServer.__new__(agent.AgentServer)
    srv.caps = set()
    hello = agent.METHODS["agent.hello"]["fn"](srv, {"capabilities": []})
    assert "mcctl_version" in hello
    assert "lulism_version" not in hello


# Modules where every `mcctl` mention is a preserve-list identifier by design.
# Keep this set as small as it can possibly be: a module-wide exemption hides
# every future leak in that file, which is exactly how ~50 user-facing `mcctl …`
# strings survived into 2.0.0's generated config and doctor hints. Prefer a
# LEAK_ALLOWLIST_LINES entry naming the exact literal.
LEAK_ALLOWLIST_MODULES = {
    "shim.py",        # the deprecated entry point — it *is* the mcctl command
    "util.py",        # LEGACY_APP + legacy_unit_names(): the migrator must know the old names
}

# Individual lines elsewhere that must keep the old name. Allowlisting the exact
# text rather than the whole module keeps the guard live for everything else in
# these files.
LEAK_ALLOWLIST_LINES = {
    ".mcctl/vanilla",  # assets.py: a cache path on the REMOTE server. migrate_legacy_dirs()
                       # only moves local XDG dirs, so renaming this orphans every server's
                       # cached vanilla jar with no migration path.
    "an older mcctl",  # server.py: a historical reference; "an older lulism" would be false.
    '"mcctl" / "mcctl.prom"',          # doctor.py: the legacy textfile doctor warns about.
    "(mcctl|lulism)",                  # doctor.py: pgrep must match a daemon under either name.
    "replaces=('mcctl')",              # doctor.py: names the PKGBUILD field, per the preserve-list.
    "$XDG_STATE_HOME/mcctl/mcctl.prom",  # config.py: the pre-2.0.0 prom_path default, quoted so
                                         # the generated config tells the operator what moved.
}


def test_no_stray_mcctl_identifiers_survive():
    pkg = PKG
    # \bmcctl\b does not match mcctl_tps or mcctl_version ("_" is a word char, so
    # no boundary), which is why the frozen metric names and the agent.hello wire
    # key do not trip this. It DOES match mcctl-watchdog.service — those live in
    # allowlisted modules by design.
    pattern = re.compile(r"\bmcctl\b")
    # units/ ships inside the package and is its only non-.py content, so a
    # `*.py` sweep never reads it: an `ExecStopPost=/usr/bin/mcctl …` or a
    # `Description=mcctl …` would install into ~/.config/systemd/user/ with this
    # guard green. Nothing else covers that text either — the sibling test below
    # compares only their *filenames*, and test_units_migration's check reads
    # only lines starting with `ExecStart=`.
    shipped = (sorted(pkg.rglob("*.py"))
               + sorted(p for p in (pkg / "units").iterdir() if p.is_file()))
    leaks = {}
    for f in shipped:
        if f.name in LEAK_ALLOWLIST_MODULES:
            continue
        hits = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
                if pattern.search(ln)
                and not any(ok in ln for ok in LEAK_ALLOWLIST_LINES)]
        if hits:
            leaks[f.name] = hits
    assert leaks == {}, f"stray mcctl identifiers: {leaks}"


def test_units_shipped_in_the_package_are_lulism_named():
    shipped = {p.name for p in (PKG / "units").iterdir() if p.suffix in {".service", ".timer"}}
    assert shipped and not any(n.startswith("mcctl-") for n in shipped)
