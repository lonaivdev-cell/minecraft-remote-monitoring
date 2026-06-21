"""mcctl agent: JSON-RPC dispatch, capability/confirm gating, NDJSON serve loop."""

from __future__ import annotations

import argparse
import io
import json

import pytest

from mcctl import agent, events
from mcctl.cli import Ctx


@pytest.fixture
def srv(cfg):
    args = argparse.Namespace(config=None, verbose=0)
    ctx = Ctx(args)
    ctx._cfg = cfg                      # local transport, isolated dirs
    return agent.AgentServer(ctx, stdout=io.StringIO())


def _call(srv, method, params=None, rid=1):
    req = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        req["params"] = params
    return srv.handle_request(req)


def test_hello_negotiates_capabilities(srv):
    r = _call(srv, "agent.hello", {"capabilities": ["actions", "destructive", "bogus"]})
    assert r["result"]["protocol"] == agent.AGENT_PROTOCOL
    assert r["result"]["capabilities"] == ["actions", "destructive"]
    assert "status" in r["result"]["methods"]


def test_ping_and_unknown_method(srv):
    assert "pong" in _call(srv, "agent.ping")["result"]
    assert _call(srv, "does.not.exist")["error"]["code"] == agent.METHOD_NOT_FOUND


def test_invalid_request_shape(srv):
    assert srv.handle_request({"id": 1, "method": "x"})["error"]["code"] == agent.INVALID_REQUEST
    bad = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "status", "params": 5})
    assert bad["error"]["code"] == agent.INVALID_PARAMS


def test_notification_has_no_response(srv):
    assert srv.handle_request({"jsonrpc": "2.0", "method": "agent.ping"}) is None


def test_capability_gating(srv):
    # 'start' needs the actions capability, not granted yet
    assert _call(srv, "start")["error"]["code"] == agent.CAP_REQUIRED
    _call(srv, "agent.hello", {"capabilities": ["actions"]})
    # now it passes the gate (and fails later for a real reason, not the gate)
    err = _call(srv, "kill")["error"]
    # kill needs actions (granted) but is destructive => confirm required
    assert err["code"] == agent.CONFIRM_REQUIRED


def test_assets_catalog_is_read_only_and_passes_through(srv, monkeypatch):
    from mcctl import assets
    monkeypatch.setattr(assets, "catalog", lambda t, cfg: {
        "textures": [{"id": "minecraft:item/stick", "crc": 1, "size": 2}],
        "count": 1, "bytes": 2,
    })
    r = _call(srv, "assets.catalog")               # no capability needed (read-only)
    assert r["result"]["count"] == 1
    assert r["result"]["textures"][0]["id"] == "minecraft:item/stick"


def test_destructive_needs_both_capability_and_confirm(srv):
    _call(srv, "agent.hello", {"capabilities": ["actions", "destructive"]})
    # restore needs the destructive capability + confirm; without confirm => gated
    assert _call(srv, "backup.restore", {"name": "x"})["error"]["code"] == agent.CONFIRM_REQUIRED


def test_backup_extract_is_actions_not_destructive(srv):
    # extract writes a fresh dir but never clobbers the world -> 'actions', no confirm
    gated = _call(srv, "backup.extract", {"name": "x", "to": "/tmp/y"})["error"]
    assert gated["code"] == agent.CAP_REQUIRED
    _call(srv, "agent.hello", {"capabilities": ["actions"]})
    err = _call(srv, "backup.extract",
                {"name": "world-world-19990101-000000.tar.zst", "to": "/tmp/y"})["error"]
    assert err["code"] == agent.APP_ERROR          # real failure, not the gate
    assert "no such backup" in err["message"]


def test_backup_offsite_surfaces_unconfigured(srv):
    assert _call(srv, "backup.offsite")["error"]["code"] == agent.CAP_REQUIRED
    _call(srv, "agent.hello", {"capabilities": ["actions"]})
    err = _call(srv, "backup.offsite", {"dry": True})["error"]
    assert err["code"] == agent.APP_ERROR          # default config has no remote
    assert "not configured" in err["message"]


def test_status_over_local_transport(srv):
    r = _call(srv, "status", {"fast": True})
    assert "running" in r["result"]
    assert r["result"]["running"] is False  # nothing running in the test box


def test_cmd_validates_empty(srv):
    _call(srv, "agent.hello", {"capabilities": ["actions"]})
    assert _call(srv, "cmd", {"command": "  "})["error"]["code"] == agent.INVALID_PARAMS


def test_events_list_and_subscribe(srv):
    events.emit("started", "up")
    r = _call(srv, "events.list", {"limit": 5})
    assert r["result"]["events"][-1]["kind"] == "started"
    sub = _call(srv, "events.subscribe", {"since": 0})
    assert sub["result"]["streaming"] is True
    srv._stop_events()


def test_subscribed_event_reaches_the_wire(srv):
    import time
    _call(srv, "events.subscribe", {"since": 0})
    try:
        events.emit("freeze-restart", "frozen server restarted", ts=time.time())
        deadline = time.time() + 3.0
        notif = None
        while time.time() < deadline:
            for line in srv._out.getvalue().splitlines():
                obj = json.loads(line)
                if obj.get("method") == "event" and obj["params"]["kind"] == "freeze-restart":
                    notif = obj
                    break
            if notif:
                break
            time.sleep(0.05)
    finally:
        srv._stop_events()
    assert notif is not None and notif["params"]["detail"] == "frozen server restarted"


def test_serve_loop_ndjson(cfg):
    args = argparse.Namespace(config=None, verbose=0)
    ctx = Ctx(args)
    ctx._cfg = cfg
    out = io.StringIO()
    lines = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "agent.hello"}),
        "not json",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "agent.ping"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "agent.shutdown"}),
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "agent.ping"}),  # never reached
    ]) + "\n"
    agent.AgentServer(ctx, stdout=out).serve(stdin=io.StringIO(lines))
    resp = [json.loads(x) for x in out.getvalue().splitlines()]
    by_id = {r.get("id"): r for r in resp}
    assert by_id[1]["result"]["protocol"] == agent.AGENT_PROTOCOL
    assert resp[1]["error"]["code"] == agent.PARSE_ERROR     # the "not json" line
    assert "pong" in by_id[2]["result"]
    assert by_id[3]["result"]["ok"] is True
    assert 4 not in by_id                                     # shutdown stopped the loop
