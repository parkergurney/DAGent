"""Pure logic in supervisor/llm.py: response parsing and packet dumping.
No network, no SDK call -- the live model call itself is opt-in only, in
tests/integration/test_supervisor_live.py.
"""
import asyncio
import json

import pytest

from orchestrator.supervisor import llm
from orchestrator.supervisor.llm import (
    SupervisorResult, _dump, _parse, _schema_block, invoke_supervisor,
)
from orchestrator.supervisor.schema import ACTION_MODELS, TriagePacket, TriggerEvent


def _packet(**overrides):
    base = dict(task_id="t1", brief="b", repo="r", delivery_mode="scout", verify_cmd=None,
               trigger=TriggerEvent(seq=1, type="worker.asked", source="worker", payload={}),
               verify_output=None, event_history=[], transcript_tail="",
               allowed_actions=["nudge", "restart", "escalate"], nudges_remaining=2,
               retries_remaining=2, yolo=False)
    base.update(overrides)
    return TriagePacket(**base)


def test_parse_plain_json():
    text = '{"action": "escalate", "summary": "s", "question": "q", "options": ["a"], "reason": "r"}'
    action = _parse(text, ["escalate"])
    assert action.action == "escalate"


def test_parse_strips_markdown_fences():
    text = '```json\n{"action": "nudge", "message": "m", "reason": "r"}\n```'
    action = _parse(text, ["nudge"])
    assert action.message == "m"


def test_parse_rejects_out_of_menu_action():
    with pytest.raises(ValueError):
        _parse('{"action": "restart", "reason": "r"}', ["escalate"])


def test_schema_block_includes_only_allowed_actions():
    block = _schema_block(["nudge", "escalate"])
    assert "nudge" in block
    assert "escalate" in block
    assert "restart" not in block


def test_dump_writes_packet_and_action(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "DATA_DIR", tmp_path)
    packet = _packet()
    action = ACTION_MODELS["escalate"](summary="s", question="q", options=["a"], reason="r")
    result = SupervisorResult(action=action, ok=True, tokens_in=10, tokens_out=5,
                              cost_usd=0.01, raw_text="{}")

    _dump(packet, result)

    path = tmp_path / "t1" / "packets" / "1.json"
    assert path.exists()
    saved = json.loads(path.read_text())
    assert saved["packet"]["task_id"] == "t1"
    assert saved["action"]["action"] == "escalate"
    assert saved["ok"] is True
    assert saved["cost_usd"] == 0.01


def test_dump_can_use_a_run_scoped_artifact_root(tmp_path):
    packet = _packet()
    action = ACTION_MODELS["escalate"](summary="s", question="q", options=["a"], reason="r")
    result = SupervisorResult(action=action, ok=True, tokens_in=1, tokens_out=1,
                              cost_usd=0.01, raw_text="{}")

    _dump(packet, result, tmp_path / "run-a" / "supervisor")

    assert (tmp_path / "run-a" / "supervisor" / "packets" / "1.json").exists()
    assert not (tmp_path / "t1").exists()


def test_supervisor_timeout_falls_back_to_escalation(tmp_path, monkeypatch):
    calls = 0

    async def hanging_call(prompt, model, *, timeout_s):
        nonlocal calls
        calls += 1
        raise TimeoutError

    monkeypatch.setattr(llm, "_one_shot", hanging_call)
    result = asyncio.run(
        invoke_supervisor(_packet(allowed_actions=["escalate"]),
                          timeout_s=0.01, artifact_root=tmp_path)
    )

    assert calls == 2
    assert result.ok is False
    assert result.action.action == "escalate"
    assert "timed out after 0.01s" in result.action.reason
