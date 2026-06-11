"""Event journal: emit/read/follow, since-filtering, rotation."""

from __future__ import annotations

from mcctl import events


class _Stop(Exception):
    pass


def test_emit_then_read_roundtrip():
    events.emit("started", "server up")
    events.emit("alert-tps", "lag", urgency="critical", data={"tps": 9.0})
    evs = events.read()
    assert [e["kind"] for e in evs] == ["started", "alert-tps"]
    assert evs[1]["urgency"] == "critical"
    assert evs[1]["data"]["tps"] == 9.0


def test_read_since_filters_old():
    events.emit("started", "a", ts=100.0)
    events.emit("stopped", "b", ts=200.0)
    later = events.read(since=100.0)
    assert [e["kind"] for e in later] == ["stopped"]


def test_read_limit_keeps_newest():
    for i in range(5):
        events.emit("alert-heap", str(i))
    evs = events.read(limit=2)
    assert [e["detail"] for e in evs] == ["3", "4"]


def test_emit_never_raises_on_bad_dir(monkeypatch, tmp_path):
    # point the journal at a path whose parent is a file => OSError, swallowed
    bad = tmp_path / "afile"
    bad.write_text("x")
    monkeypatch.setattr(events, "events_path", lambda: bad / "nested.jsonl")
    monkeypatch.setattr(events.util, "ensure_dirs", lambda: None)
    ev = events.emit("started", "should not raise")
    assert ev["kind"] == "started"  # returns the dict even though the write failed


def test_follow_streams_backlog_then_appends():
    events.emit("started", "a", ts=1.0)
    events.emit("stopped", "b", ts=2.0)
    seen: list[str] = []
    calls = {"n": 0}

    def sleeper(_secs):
        calls["n"] += 1
        if calls["n"] == 1:
            events.emit("alert-tps", "c", ts=3.0)  # arrives after backlog
        else:
            raise _Stop

    try:
        for ev in events.follow(since=None, poll=0, sleeper=sleeper):
            seen.append(ev["kind"])
    except (_Stop, RuntimeError):
        pass
    assert seen[:2] == ["started", "stopped"]
    assert "alert-tps" in seen
