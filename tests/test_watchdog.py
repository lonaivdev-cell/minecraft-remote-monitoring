"""The watchdog decision matrix — pure decide() over Observations."""

from __future__ import annotations

import pytest

from mcctl.config import Config
from mcctl.watchdog import Action, Observation, decide

NOW = 1_750_000_000.0


@pytest.fixture
def wcfg() -> Config:
    return Config()


def _obs(**kw) -> Observation:
    base = dict(ts=NOW, ssh_ok=True, proc_up=True, tmux_up=True, port_open=True,
                log_age_s=5, console_ok=True)
    base.update(kw)
    return Observation(**base)


def _st(armed=True, desired="up", restarts=(), halted=False) -> dict:
    return {"armed": armed, "desired": desired, "restarts": list(restarts),
            "halted": halted, "last_alerts": {}}


def test_disarmed_never_acts(wcfg):
    assert decide(_obs(proc_up=False), _st(armed=False), wcfg) == [Action.NOOP]


def test_halted_never_acts(wcfg):
    assert decide(_obs(proc_up=False), _st(halted=True), wcfg) == [Action.NOOP]


def test_ssh_down_alerts_only(wcfg):
    assert decide(_obs(ssh_ok=False, proc_up=False), _st(), wcfg) == [Action.ALERT_SSH]


def test_down_and_desired_up_starts(wcfg):
    assert decide(_obs(proc_up=False), _st(), wcfg) == [Action.START]


def test_down_and_desired_down_is_noop(wcfg):
    """`mcctl stop` must never be fought by the watchdog."""
    assert decide(_obs(proc_up=False), _st(desired="down"), wcfg) == [Action.NOOP]


def test_crash_loop_breaker(wcfg):
    recent = [NOW - 100, NOW - 200, NOW - 300]  # == max_restarts within window
    assert decide(_obs(proc_up=False), _st(restarts=recent), wcfg) == [Action.HALT_CRASHLOOP]


def test_old_restarts_age_out(wcfg):
    stale = [NOW - 7200, NOW - 7300, NOW - 9999]  # outside restart_window
    assert decide(_obs(proc_up=False), _st(restarts=stale), wcfg) == [Action.START]


def test_freeze_restarts(wcfg):
    obs = _obs(log_age_s=999, console_ok=False)
    assert decide(obs, _st(), wcfg) == [Action.RESTART_FROZEN]


def test_quiet_log_alone_is_not_freeze(wcfg):
    """An idle server logs rarely; only stale log + dead console means frozen."""
    assert decide(_obs(log_age_s=999, console_ok=True), _st(), wcfg) == [Action.NOOP]
    assert decide(_obs(log_age_s=10, console_ok=False), _st(), wcfg) == [Action.NOOP]


def test_freeze_respects_crash_loop_breaker(wcfg):
    recent = [NOW - 100, NOW - 200, NOW - 300]
    obs = _obs(log_age_s=999, console_ok=False)
    assert decide(obs, _st(restarts=recent), wcfg) == [Action.HALT_CRASHLOOP]


def test_tps_and_heap_alerts_stack(wcfg):
    obs = _obs(tps=9.5, heap_pct=95.0)
    actions = decide(obs, _st(), wcfg)
    assert Action.ALERT_TPS in actions and Action.ALERT_HEAP in actions


def test_healthy_tps_no_alert(wcfg):
    assert decide(_obs(tps=19.9, heap_pct=50.0), _st(), wcfg) == [Action.NOOP]


def test_disk_alert(wcfg):
    obs = _obs(disk_free=1 * 1024**3)  # below backup.min_free_gb=5
    assert Action.ALERT_DISK in decide(obs, _st(), wcfg)


def test_autosave_due(wcfg):
    wcfg.watchdog.autosave_minutes = 20
    obs = _obs()
    assert Action.AUTOSAVE in decide(obs, _st(), wcfg, last_save_ts=NOW - 21 * 60)
    assert Action.AUTOSAVE not in decide(obs, _st(), wcfg, last_save_ts=NOW - 5 * 60)


def test_autosave_disabled_by_default(wcfg):
    assert decide(_obs(), _st(), wcfg, last_save_ts=0.0) == [Action.NOOP]
