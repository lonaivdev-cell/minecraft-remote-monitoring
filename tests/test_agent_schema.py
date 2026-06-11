"""The agent contract is frozen by a golden file.

If this test fails, the JSON-RPC surface changed. Either the change is additive
and safe (regenerate the golden, commit it consciously) or it is breaking (bump
agent.AGENT_PROTOCOL and ship a new golden). The point is that the contract a
shipped phone binary depends on can never drift by accident.

Regenerate:  python -c "import json,sys; sys.path.insert(0,'src'); from mcctl \
import agent; json.dump(agent.build_schema(), open('tests/golden/agent_schema_v1.json','w'), \
indent=2, sort_keys=True); open('tests/golden/agent_schema_v1.json','a').write('\\n')"
"""

from __future__ import annotations

import json
from pathlib import Path

from mcctl import agent

GOLDEN = Path(__file__).parent / "golden" / "agent_schema_v1.json"


def test_schema_matches_golden():
    current = json.dumps(agent.build_schema(), indent=2, sort_keys=True) + "\n"
    expected = GOLDEN.read_text(encoding="utf-8")
    assert current == expected, (
        "agent schema drifted from the golden file. If the change is intended, "
        "regenerate tests/golden/agent_schema_v1.json (see this file's docstring) "
        "and bump agent.AGENT_PROTOCOL if it is a breaking change."
    )


def test_protocol_version_stamped():
    assert agent.build_schema()["protocol"] == agent.AGENT_PROTOCOL


def test_every_method_has_a_summary():
    for name, spec in agent.METHODS.items():
        assert spec["summary"], f"method {name} is missing a summary"
