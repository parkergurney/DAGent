"""Unit tests for sdk_worker.py's pure logic (design.md section 8, M3) --
sentinel parsing and the worktree-escape guard. No SDK client, no network, no
API cost: this is the deterministic part of the real-worker integration, kept
testable the same way FakeWorker keeps the scheduler testable.
"""
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
