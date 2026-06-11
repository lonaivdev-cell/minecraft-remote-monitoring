"""doctor: the ops checks added after the 2026-06-11 incident — exactly one
restart authority, no unbounded in-tmux crash loop, boot survives a missing
data volume, and exactly one brain (watchdog + state) per DESIGN-BRAIN.md.
Driven entirely through FakeTransport."""

from __future__ import annotations

from mcctl import doctor as doctor_mod
from mcctl import state
from mcctl.doctor import Level, run_doctor

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
    fake_t.expect("pgrep -af 'mcctl watchdog run'",
                  out="888 python3 /usr/bin/mcctl watchdog run\n")
    fake_t.expect("loginctl show-user", out="Linger=yes\n")
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: brain placement"].level is Level.OK
    assert "box" in res["ops: brain placement"].detail
    assert res["ops: brain linger"].level is Level.OK


def test_brain_on_box_without_linger_warns(fake_t, cfg, monkeypatch):
    _layout(fake_t, cfg)
    _ssh_mode(cfg, monkeypatch, local_wd=False)
    fake_t.expect("pgrep -af 'mcctl watchdog run'",
                  out="888 python3 /usr/bin/mcctl watchdog run\n")
    fake_t.expect("loginctl show-user", out="Linger=no\n")
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: brain placement"].level is Level.OK
    assert res["ops: brain linger"].level is Level.WARN
    assert "enable-linger" in res["ops: brain linger"].hint


def test_two_brains_warn_loudly(fake_t, cfg, monkeypatch):
    _layout(fake_t, cfg)
    _ssh_mode(cfg, monkeypatch, local_wd=True)
    fake_t.expect("pgrep -af 'mcctl watchdog run'",
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


def test_local_transport_box_checks_its_own_linger(fake_t, cfg, monkeypatch):
    """Post-migration: doctor run ON the box (transport=local) owns the linger check."""
    _layout(fake_t, cfg)
    monkeypatch.setattr(doctor_mod, "_local_watchdog_active", lambda: True)
    monkeypatch.setattr(doctor_mod, "_local_linger", lambda: "no")
    res = _by_name(run_doctor(cfg, fake_t))
    assert res["ops: brain placement"].level is Level.OK
    assert res["ops: brain linger"].level is Level.WARN
