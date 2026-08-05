"""doctor: the ops checks added after the 2026-06-11 incident — exactly one
restart authority, no unbounded in-tmux crash loop, boot survives a missing
data volume, and exactly one brain (watchdog + state) per DESIGN-BRAIN.md.
Driven entirely through FakeTransport."""

from __future__ import annotations

import pytest

from lulism import doctor as doctor_mod
from lulism import state, util
from lulism.doctor import Level, run_doctor

_REAL_WATCHDOG_COUNT = doctor_mod._watchdog_process_count
_REAL_UNIT_IS_ACTIVE = doctor_mod._unit_is_active


@pytest.fixture(autouse=True)
def _no_real_local_probes(monkeypatch):
    """_ops_checks() asks this machine about its own units, watchdog processes
    and logind state. Pin all four so the suite cannot depend on what the
    developer's box happens to have enabled or running; the tests that care
    override them.

    _local_linger() belongs in this list: whenever a test makes
    _local_watchdog_active() true over the local transport, _ops_checks() shells
    out to `loginctl show-user $(getpass.getuser())`. On a box with a logind
    session that answers "yes" and an extra `ops: brain linger` OK appears in the
    result set; on a CI runner with no user record loginctl fails, the check
    returns None and the result is absent. '' (loginctl gave no answer) is the
    neutral pin — the same result set in both places, and no unmocked subprocess
    reaching real user state."""
    monkeypatch.setattr(doctor_mod, "_watchdog_process_count", lambda: 0)
    monkeypatch.setattr(doctor_mod, "_unit_is_enabled", lambda _u: False)
    monkeypatch.setattr(doctor_mod, "_unit_is_active", lambda _u: False)
    monkeypatch.setattr(doctor_mod, "_local_linger", lambda: "")


VARIABLES = (
    'SKIP_JAVA_CHECK="true"\n'
    'WAIT_FOR_USER_INPUT="false"\n'
    'SERVERSTARTERJAR_FORCE_FETCH="false"\n'
    'JAVA_ARGS="-Xms4G -Xmx8G"\n'
    'RESTART="true"\n'
)


def _layout(fake_t, cfg, *, variables: str = VARIABLES):
    d = cfg.server.server_dir
    fake_t.files[d] = ""
    fake_t.files[f"{d}/start.sh"] = ""
    fake_t.files[f"{d}/variables.txt"] = variables
    fake_t.files[f"{d}/world"] = ""


def _by_name(results):
    return {r.name: r for r in results}


# -------- monitoring-only mode: a box with no Minecraft install must still pass


def test_missing_server_dir_is_a_warning_not_fatal(fake_t, cfg):
    """No Minecraft on the box (yet) is a configuration state, not a broken
    stack: doctor reports the missing server_dir as WARN, marks the server-
    anchored checks SKIPped rather than cascade-failing them, and nothing in
    the result set is FAIL — so the CLI exits 0 and lulism keeps working as a
    host monitor."""
    res = run_doctor(cfg, fake_t)
    by = _by_name(res)

    r = by["remote: server_dir"]
    assert r.level is Level.WARN
    assert "no Minecraft install" in r.detail
    assert "server_dir" in r.hint

    skip = by["remote: server checks"]
    assert skip.level is Level.SKIP
    assert "server_dir" in skip.detail

    assert "remote: start script" not in by  # skipped, not failed
    assert not [x for x in res if x.level is Level.FAIL]


def test_ops_warns_on_every_competing_restart_authority(fake_t, cfg):
    _layout(fake_t, cfg)
    state.set_armed(True)
    fake_t.expect("pgrep -af 'mc-control", out="4242 bash mc-control.sh watchdog\n")
    fake_t.expect("systemctl show minecraft.service",
                  out="LoadState=loaded\nActiveState=active\nRestart=on-failure\n")
    fake_t.expect("findmnt -no TARGET", out="/opt/minecraft\n")
    fake_t.expect("awk -v m=", out="defaults,noatime\n")

    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: legacy watchdog on server"].level is Level.WARN
    assert res["ops: two restart authorities"].level is Level.WARN
    assert res["ops: start.sh RESTART=true"].level is Level.WARN
    assert res["ops: fstab nofail"].level is Level.WARN


def test_ops_systemd_unit_without_restart_is_fine(fake_t, cfg):
    _layout(fake_t, cfg)
    state.set_armed(True)
    fake_t.expect("systemctl show minecraft.service",
                  out="LoadState=loaded\nActiveState=active\nRestart=no\n")
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: systemd unit"].level is Level.OK
    assert "ops: two restart authorities" not in res


def test_ops_disarmed_watchdog_tolerates_systemd_restart(fake_t, cfg):
    _layout(fake_t, cfg)
    state.set_armed(False)
    fake_t.expect("systemctl show minecraft.service",
                  out="LoadState=loaded\nActiveState=active\nRestart=on-failure\n")
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: systemd unit"].level is Level.OK


def test_ops_clean_box_reports_ok_and_skips_unknowable(fake_t, cfg):
    _layout(fake_t, cfg, variables=VARIABLES.replace('RESTART="true"', 'RESTART="false"'))
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: no legacy watchdog on server"].level is Level.OK
    assert res["ops: start.sh RESTART=false"].level is Level.OK
    # no systemctl/findmnt output (not a systemd box / unknown mount) => no noise
    assert "ops: systemd unit" not in res
    assert "ops: fstab nofail" not in res


def test_ops_fix_rewrites_restart_flag(fake_t, cfg):
    _layout(fake_t, cfg)
    res = _by_name(run_doctor(cfg, fake_t, fix=True))
    assert res["ops: start.sh RESTART"].level is Level.FIXED
    assert 'RESTART="false"' in fake_t.files[f"{cfg.server.server_dir}/variables.txt"]


def test_ops_nofail_present_is_ok(fake_t, cfg):
    _layout(fake_t, cfg)
    fake_t.expect("findmnt -no TARGET", out="/opt/minecraft\n")
    fake_t.expect("awk -v m=", out="defaults,nofail,_netdev\n")
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: fstab nofail"].level is Level.OK


def test_ops_root_mount_needs_no_nofail(fake_t, cfg):
    _layout(fake_t, cfg)
    fake_t.expect("findmnt -no TARGET", out="/\n")
    res = _by_name(run_doctor(cfg, fake_t))
    assert "ops: fstab nofail" not in res


# ---------------- brain placement (DESIGN-BRAIN.md): one watchdog, on the box


def test_local_watchdog_loose_process_pattern_covers_both_names(monkeypatch):
    """_watchdog_process_count()'s pgrep (used when the systemd unit isn't the
    one running -- a manual `lulism watchdog run &`, a dev invocation, or a stop
    that didn't fully reap the process) must catch a daemon under either name. A
    pattern that only matched the post-2.0.0 `lulism watchdog run` would report
    "no watchdog" for a leftover pre-migration process, which is a false
    negative that feeds the brain-placement check and can prompt a second
    watchdog -- the 2026-06-11 two-restart-authorities incident."""
    import re
    import subprocess

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(doctor_mod, "_watchdog_process_count", _REAL_WATCHDOG_COUNT)
    monkeypatch.setattr(doctor_mod, "_unit_is_active", _REAL_UNIT_IS_ACTIVE)
    assert doctor_mod._local_watchdog_active() is False

    pgrep_calls = [c for c in calls if c[0] == "pgrep"]
    assert len(pgrep_calls) == 1, calls
    assert "-c" in pgrep_calls[0], "a bool cannot tell one watchdog from two"
    pattern = pgrep_calls[0][-1]
    assert re.search(pattern, "mcctl watchdog run"), pattern
    assert re.search(pattern, "lulism watchdog run"), pattern
    assert not re.search(pattern, "totally unrelated process"), pattern


def test_watchdog_process_count_parses_pgrep_c(monkeypatch):
    import subprocess

    def counting(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="2\n", stderr="")

    monkeypatch.setattr(doctor_mod.subprocess, "run", counting)
    assert _REAL_WATCHDOG_COUNT() == 2


def test_watchdog_process_count_survives_a_missing_pgrep(monkeypatch):
    def missing(cmd, **kwargs):
        raise OSError("no pgrep here")

    monkeypatch.setattr(doctor_mod.subprocess, "run", missing)
    assert _REAL_WATCHDOG_COUNT() == 0


# -------- the dual-authority verdict: enablement alone never saw the pacman path


@pytest.mark.parametrize(("enabled", "active", "procs", "legacy_warns", "dup_warns"), [
    # nothing left over: the clean post-migration box
    (False, False, 0, False, False),
    # the pipx path: `mcctl watchdog install` wrote the unit, it is still enabled
    (True, False, 1, True, False),
    # the pacman path (`makepkg -si`, PKGBUILD replaces=('mcctl')): pacman deleted
    # the unit FILES, so is-enabled answers "not-found" while the daemon lives on.
    # This is the case the enablement-only check reported as a green tick.
    (False, True, 1, True, False),
    (True, True, 1, True, False),
    # two daemons on one machine: the 2026-06-11 incident itself. Units may say
    # nothing at all (both running loose), so only the count can see it.
    (False, False, 2, False, True),
    (True, False, 2, True, True),
])
def test_dual_restart_authority_verdicts(fake_t, cfg, monkeypatch,
                                         enabled, active, procs, legacy_warns, dup_warns):
    _layout(fake_t, cfg)
    monkeypatch.setattr(doctor_mod, "_unit_is_enabled",
                        lambda u: enabled and u in util.legacy_unit_names())
    monkeypatch.setattr(doctor_mod, "_unit_is_active",
                        lambda u: active and u in util.legacy_unit_names())
    monkeypatch.setattr(doctor_mod, "_watchdog_process_count", lambda: procs)

    res = _by_name(run_doctor(cfg, fake_t))

    # `procs >= 1` sends _ops_checks() down the local-brain branch, which asks
    # logind about the current user. With _local_linger() pinned to '' by the
    # autouse fixture the answer is "no opinion" everywhere, so the result set
    # is the same on a developer's box as on a runner with no logind session.
    assert "ops: brain linger" not in res

    if legacy_warns:
        r = res["ops: pre-2.0.0 units still present"]
        assert r.level is Level.WARN
        assert "mcctl-watchdog.service" in r.detail
        assert ("enabled" in r.detail) is enabled
        assert ("active" in r.detail) is active
        assert "systemctl --user disable --now" in r.hint
        # the hint must be pasteable: bare unit names, not the annotated detail
        assert "(" not in r.hint.split("disable --now", 1)[1]
    else:
        assert res["ops: no pre-2.0.0 units present"].level is Level.OK

    if dup_warns:
        assert res["ops: one watchdog per machine"].level is Level.WARN
        assert f"{procs} on this machine" in res["ops: one watchdog per machine"].detail
    elif procs:
        assert res["ops: one watchdog per machine"].level is Level.OK
    else:
        assert "ops: one watchdog per machine" not in res


def test_two_watchdogs_on_the_box_are_counted_over_ssh(fake_t, cfg, monkeypatch):
    """The box is where the brain lives, so a duplicate there is the dangerous
    one. `_local_watchdog_active()` returns a bool and cannot tell one from two."""
    _layout(fake_t, cfg)
    _ssh_mode(cfg, monkeypatch, local_wd=False)
    fake_t.expect("pgrep -af '(mcctl|lulism) watchdog run'",
                  out="888 python3 /usr/bin/mcctl watchdog run\n"
                      "999 python3 /usr/bin/lulism watchdog run\n")
    fake_t.expect("loginctl show-user", out="Linger=yes\n")
    res = _by_name(run_doctor(cfg, fake_t))
    dup = res["ops: one watchdog per machine"]
    assert dup.level is Level.WARN
    assert f"2 on {cfg.server.host}" in dup.detail
    assert "disable --now" in dup.hint


def _refuse(*a, **k):
    raise OSError("closed")


def _ssh_mode(cfg, monkeypatch, *, local_wd: bool):
    """Pretend this is a real desktop→box setup without touching the network
    or this machine's actual systemd units."""
    cfg.server.transport = "ssh"
    cfg.server.host = "127.0.0.1"
    monkeypatch.setattr(doctor_mod.socket, "create_connection", _refuse)
    monkeypatch.setattr(doctor_mod, "_local_watchdog_active", lambda: local_wd)


def test_brain_on_box_with_linger_is_the_target_topology(fake_t, cfg, monkeypatch):
    _layout(fake_t, cfg)
    _ssh_mode(cfg, monkeypatch, local_wd=False)
    fake_t.expect("pgrep -af '(mcctl|lulism) watchdog run'",
                  out="888 python3 /usr/bin/mcctl watchdog run\n")
    fake_t.expect("loginctl show-user", out="Linger=yes\n")
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: brain placement"].level is Level.OK
    assert "box" in res["ops: brain placement"].detail
    assert res["ops: brain linger"].level is Level.OK


def test_brain_on_box_without_linger_warns(fake_t, cfg, monkeypatch):
    _layout(fake_t, cfg)
    _ssh_mode(cfg, monkeypatch, local_wd=False)
    fake_t.expect("pgrep -af '(mcctl|lulism) watchdog run'",
                  out="888 python3 /usr/bin/mcctl watchdog run\n")
    fake_t.expect("loginctl show-user", out="Linger=no\n")
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: brain placement"].level is Level.OK
    assert res["ops: brain linger"].level is Level.WARN
    assert "enable-linger" in res["ops: brain linger"].hint


def test_two_brains_warn_loudly(fake_t, cfg, monkeypatch):
    _layout(fake_t, cfg)
    _ssh_mode(cfg, monkeypatch, local_wd=True)
    fake_t.expect("pgrep -af '(mcctl|lulism) watchdog run'",
                  out="888 python3 /usr/bin/mcctl watchdog run\n")
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: brain placement"].level is Level.WARN
    assert "BOTH" in res["ops: brain placement"].detail
    assert "ops: brain linger" not in res


def test_client_brain_is_ok_but_points_at_the_migration(fake_t, cfg, monkeypatch):
    _layout(fake_t, cfg)
    _ssh_mode(cfg, monkeypatch, local_wd=True)
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: brain placement"].level is Level.OK
    assert "DESIGN-BRAIN" in res["ops: brain placement"].detail


def test_no_brain_anywhere_warns_self_healing_off(fake_t, cfg, monkeypatch):
    _layout(fake_t, cfg)
    _ssh_mode(cfg, monkeypatch, local_wd=False)
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: brain placement"].level is Level.WARN
    assert "self-healing is off" in res["ops: brain placement"].detail


@pytest.mark.parametrize("linger,level", [("no", Level.WARN), ("yes", Level.OK), ("", None)])
def test_local_transport_box_checks_its_own_linger(fake_t, cfg, monkeypatch, linger, level):
    """Post-migration: doctor run ON the box (transport=local) owns the linger
    check. All three loginctl answers are pinned here, including '' (no logind
    user record — a CI runner), which must stay quiet rather than guess."""
    _layout(fake_t, cfg)
    monkeypatch.setattr(doctor_mod, "_local_watchdog_active", lambda: True)
    monkeypatch.setattr(doctor_mod, "_local_linger", lambda: linger)
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: brain placement"].level is Level.OK
    if level is None:
        assert "ops: brain linger" not in res
    else:
        assert res["ops: brain linger"].level is level
