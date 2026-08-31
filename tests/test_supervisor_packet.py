"""Packet building (see README.md): compaction, verify_output
extraction, and the caps-derived allowed_actions/nudges_remaining/
retries_remaining fields. Pure reads against a hand-driven event log, no
LLM, no worker.
"""
from dagent.store import append_event, connect, create_task, transition
from dagent.supervisor import build_packet


def test_build_packet_compacts_tool_calls_and_extracts_verify_output():
    conn = connect()
    task_id = create_task(conn, title="t", brief="fix it", repo="r",
                          delivery_mode="scout", verify_cmd="pytest")
    s = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
    transition(conn, task_id, "queued", cause_seq=s)
    s = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id, session_id="123")
    transition(conn, task_id, "running", cause_seq=s, session_id="123", worktree="/wt", base_sha="abc")

    for tool in ["Read", "Read", "Edit", "Bash"]:
        append_event(conn, source="worker", type="worker.tool_used", task_id=task_id, payload={"tool": tool})
    append_event(conn, source="worker", type="worker.messaged", task_id=task_id,
                payload={"text": "running the tests now"})
    s = append_event(conn, source="worker", type="worker.done_claimed", task_id=task_id)
    transition(conn, task_id, "verifying", cause_seq=s)
    fail_seq = append_event(conn, source="verifier", type="verify.failed", task_id=task_id,
                            payload={"cause": "tests_failed", "output_tail": "AssertionError: boom"})
    transition(conn, task_id, "triage", cause_seq=fail_seq)

    packet = build_packet(conn, task_id, fail_seq, yolo=False, live_session=False,
                          max_nudges=2, transcript_tail_tokens=3000)

    assert packet.trigger.type == "verify.failed"
    assert packet.verify_output == "AssertionError: boom"
    assert packet.allowed_actions == ["restart", "escalate"]
    assert packet.retries_remaining == 2
    assert packet.nudges_remaining == 2

    tool_rows = [e for e in packet.event_history if e.type == "worker.tool_used"]
    assert len(tool_rows) == 1  # collapsed into one row
    assert "4 tool calls" in tool_rows[0].summary
    assert "2 Read" in tool_rows[0].summary

    assert "running the tests now" in packet.transcript_tail


def test_build_packet_counts_prior_nudges_against_the_cap():
    conn = connect()
    task_id = create_task(conn, title="t", brief="b", repo="r", delivery_mode="scout", verify_cmd="true")
    s = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
    transition(conn, task_id, "queued", cause_seq=s)
    s = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id, session_id="1")
    transition(conn, task_id, "running", cause_seq=s, session_id="1", worktree="/wt", base_sha="abc")

    append_event(conn, source="supervisor", type="supervisor.acted", task_id=task_id,
                payload={"action": "nudge", "message": "m", "reason": "r"})
    ask_seq = append_event(conn, source="worker", type="worker.asked", task_id=task_id,
                           payload={"question": "which lib?"})
    transition(conn, task_id, "triage", cause_seq=ask_seq)

    packet = build_packet(conn, task_id, ask_seq, yolo=False, live_session=True,
                          max_nudges=2, transcript_tail_tokens=3000)

    assert packet.nudges_remaining == 1  # one already used
    assert "nudge" in packet.allowed_actions
