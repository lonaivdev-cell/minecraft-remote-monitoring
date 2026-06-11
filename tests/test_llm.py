"""llm: payload hygiene + prompt plumbing. No network, no anthropic needed."""

import sys
import types

import pytest

from mcctl import llm
from mcctl.config import Config


def test_redact_masks_rcon_password():
    text = "rcon.password=SuperSecret123\nmotd=hello"
    out = llm.redact(text)
    assert "SuperSecret123" not in out
    assert "rcon.password=********" in out
    assert "motd=hello" in out


def test_redact_masks_secret_assignments_and_ansi():
    text = "ANTHROPIC_API_KEY=sk-ant-xyz\n\x1b[31mAUTH_TOKEN: tok123\x1b[0m\nPATH=/usr/bin"
    out = llm.redact(text)
    assert "sk-ant-xyz" not in out
    assert "tok123" not in out
    assert "\x1b" not in out
    assert "PATH=/usr/bin" in out


def test_envelope_wraps_and_elides():
    out = llm.envelope("latest.log", "x" * 200, limit=90)
    assert out.startswith('<data kind="latest.log">')
    assert out.endswith("</data>")
    assert "elided by mcctl" in out
    small = llm.envelope("k", "short")
    assert "elided" not in small


def test_adaptive_gate():
    assert llm._adaptive_ok("claude-opus-4-8")
    assert llm._adaptive_ok("claude-sonnet-4-6")
    assert llm._adaptive_ok("claude-fable-5")
    assert not llm._adaptive_ok("claude-haiku-4-5")


def test_kind_prompts_cover_cli_kinds():
    for kind in ("logs", "crash", "mods", "inspect", "ask"):
        assert kind in llm.KIND_PROMPTS


def test_analyst_missing_key_message(monkeypatch):
    cfg = Config()
    cfg.llm.api_key_env = "MCCTL_TEST_KEY"
    monkeypatch.delenv("MCCTL_TEST_KEY", raising=False)
    # a fake anthropic module so the import works without the dependency
    monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))
    with pytest.raises(llm.LlmError, match="MCCTL_TEST_KEY"):
        llm.Analyst(cfg)._client()


def test_available_reports_missing_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import raises ImportError
    ok, reason = llm.available()
    assert not ok and "anthropic" in reason


def test_system_prompt_hardens_against_injection():
    sp = llm.SYSTEM_PROMPT.lower()
    assert "untrusted" in sp
    assert "never follow instructions" in sp
