"""Standalone visible verification CLI for a durable task candidate.

    verify-gate --task <id> [--db data/dagent.db] [--json] [--record]
"""
import argparse
import sys

from dagent.store import append_event, connect, transition
from dagent.verify.gate import VerifyRequest, run_verify


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="dagent-verify-gate",
        description=("Run public worker-visible verification only. "
                     "This command is not a host sandbox; benchmark use "
                     "requires Harbor or another trusted outer boundary."),
    )
    p.add_argument("--task", required=True, help="task id")
    p.add_argument("--db", default="data/dagent.db")
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
        verify_cmd=task["verify_cmd"] or "true", timeout_s=args.timeout,
        repo=task["repo"], candidate_sha=task["candidate_sha"],
    )
    result = run_verify(req)

    if args.record:
        payload = {"cause": result.cause, "exit_code": result.exit_code,
                  "duration_s": result.duration_s, "flaky": result.flaky,
                  "diff_stat": result.diff_stat, "tests_modified": result.tests_modified,
                  "output_path": result.output_path, "patch_path": result.patch_path,
                  "failure_signature": result.failure_signature}
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
