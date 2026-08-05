"""Standalone `verify-gate` CLI (design.md section 7): grades a task's
worktree against its verify_cmd through the exact machinery the scheduler
uses, so the benchmark harness can grade every condition -- orchestrated or
not -- with identical code.

    verify-gate --task <id> [--db data/orchestrator.db] [--json] [--record]
"""
import argparse
import sys

from orchestrator.store import append_event, connect, transition
from orchestrator.verify.gate import DEFAULT_PROTECTED, VerifyRequest, run_verify


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="verify-gate")
    p.add_argument("--task", required=True, help="task id")
    p.add_argument("--db", default="data/orchestrator.db")
    p.add_argument("--protected", action="append", default=None,
                   help="protected-path glob; repeatable (default: empty)")
    p.add_argument("--setup-cmd", help="overrides the task's own setup_cmd if given")
    p.add_argument("--hidden-cmd")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--json", action="store_true")
    p.add_argument("--record", action="store_true",
                   help="also emit verify.* events and transition task state")
    args = p.parse_args(argv)

    conn = connect(args.db)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task,)).fetchone()
    if row is None:
        print(f"unknown task {args.task!r}", file=sys.stderr)
        return 2
    task = dict(row)

    req = VerifyRequest(
        task_id=task["id"], worktree=task["worktree"], base_sha=task["base_sha"],
        verify_cmd=task["verify_cmd"] or "true", setup_cmd=args.setup_cmd or task["setup_cmd"],
        hidden_cmd=args.hidden_cmd, timeout_s=args.timeout,
        protected_paths=tuple(args.protected) if args.protected else DEFAULT_PROTECTED,
    )
    result = run_verify(req)

    if args.record:
        payload = {"cause": result.cause, "exit_code": result.exit_code,
                  "duration_s": result.duration_s, "flaky": result.flaky}
        if result.passed:
            s = append_event(conn, source="verifier", type="verify.passed",
                             task_id=task["id"], payload=payload)
            transition(conn, task["id"], "delivering", cause_seq=s)
        else:
            s = append_event(conn, source="verifier", type="verify.failed",
                             task_id=task["id"], payload=payload)
            transition(conn, task["id"], "triage", cause_seq=s)

    if args.json:
        print(result.to_json())
    else:
        print(f"{'PASS' if result.passed else 'FAIL'} ({result.cause}) in {result.duration_s}s")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
