"""Unit tests for sdk_worker.py's pure logic (design.md section 8, M3) --
sentinel parsing and the worktree-escape guard. No SDK client, no network, no
API cost: this is the deterministic part of the real-worker integration, kept
testable the same way FakeWorker keeps the scheduler testable.
"""
import asyncio
import io
import json

from claude_agent_sdk import ResultMessage

from orchestrator.worker import sdk_worker
from orchestrator.worker.sdk_worker import (
    _parse_terminal,
    _path_escapes_worktree,
    _prompt_with_protocol,
)


def test_prompt_appends_protocol_to_brief():
    prompt = _prompt_with_protocol("fix the bug in foo.py")
    assert prompt.startswith("fix the bug in foo.py")
    assert "DONE_CLAIM:" in prompt
    assert "ASK:" in prompt


def test_parse_terminal_done_claim():
    kind, extra = _parse_terminal("I fixed it.\nDONE_CLAIM: added a null check\n")
    assert kind == "done_claimed"
    assert extra == {"result": "added a null check"}


def test_parse_terminal_ask():
    kind, extra = _parse_terminal("Not sure how to proceed.\nASK: which logging library?")
    assert kind == "asked"
    assert extra == {"question": "which logging library?"}


def test_parse_terminal_no_sentinel_is_unclaimed():
    kind, extra = _parse_terminal("I think I'm done but forgot to say so.")
    assert kind is None
    assert extra == {}


def test_parse_terminal_handles_none():
    assert _parse_terminal(None) == (None, {})


def test_path_inside_worktree_is_allowed(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    assert _path_escapes_worktree(str(wt / "output.txt"), wt) is False
    assert _path_escapes_worktree("output.txt", wt) is False  # relative, resolved against wt


def test_path_outside_worktree_is_denied(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    assert _path_escapes_worktree(str(tmp_path / "other" / "x.txt"), wt) is True
    assert _path_escapes_worktree("/etc/passwd", wt) is True


def test_path_traversal_out_of_worktree_is_denied(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    assert _path_escapes_worktree("../escape.txt", wt) is True


class _FakeClient:
    """Stands in for ClaudeSDKClient: turn 1 asks a question, turn 2 (after
    whatever run() queries back in) claims done. Lets the stdin-nudge path
    (design.md section 6's live intervention) be tested without a live SDK
    session or API cost."""

    def __init__(self, options=None):
        self.queries = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_response(self):
        if len(self.queries) == 1:
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="s1",
                                total_cost_usd=0.01, result="ASK: which port?")
        else:
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=2, session_id="s1",
                                total_cost_usd=0.02, result="DONE_CLAIM: used port 8080")


def test_run_resumes_the_same_session_after_a_stdin_reply(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdk_worker.sys, "stdin", io.StringIO("use port 8080\n"))
    fake = _FakeClient()
    monkeypatch.setattr(sdk_worker, "ClaudeSDKClient", lambda options=None: fake)

    asyncio.run(sdk_worker.run(tmp_path, "set up a server", None))

    # one query for the brief, a second carrying the stdin reply back in --
    # not a fresh session, the same conversation resumed.
    assert fake.queries == [_prompt_with_protocol("set up a server"), "use port 8080"]

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["type"] for e in events] == ["asked", "done_claimed"]
    assert events[1]["payload"]["result"] == "used port 8080"


def test_run_exits_without_resuming_when_stdin_closes(tmp_path, capsys, monkeypatch):
    """No reply ever lands (escalate/abandon tore the process down and closed
    its stdin) -- run() must not hang or fabricate a second query."""
    monkeypatch.setattr(sdk_worker.sys, "stdin", io.StringIO(""))  # EOF immediately
    fake = _FakeClient()
    monkeypatch.setattr(sdk_worker, "ClaudeSDKClient", lambda options=None: fake)

    asyncio.run(sdk_worker.run(tmp_path, "set up a server", None))

    assert fake.queries == [_prompt_with_protocol("set up a server")]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["type"] for e in events] == ["asked"]
