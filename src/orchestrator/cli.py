"""`orchestrator` console script: the operator-facing CLI over the library
described in docs/usage.md. Five subcommands, each a thin wrapper over
existing pieces -- no new control-flow, just the plumbing to drive them
without hand-writing a Python script per batch:

    orchestrator add-task --repo R --title T --brief B --delivery-mode M [...]
    orchestrator run       [--fake-worker] [--fake-supervisor] ...
    orchestrator daemon    [--poll-interval S] ...   # like run, never exits
    orchestrator answer TASK_ID "message"
    orchestrator status [TASK_ID] [--digest]

`--repo` also accepts a short name from repos.toml (see docs/usage.md)
instead of a full path.
"""
import argparse
import asyncio
import json
import sys
import tomllib
from collections import Counter
from functools import partial
from pathlib import Path

from orchestrator import config
from orchestrator.scheduler import Scheduler
from orchestrator.store import append_event, connect, create_task, transition
from orchestrator.supervisor import always_escalate, invoke_supervisor
from orchestrator.worker import (
    WorkerIsolationError, spawn_cli_worker, spawn_fake_worker, spawn_sdk_worker,
    validate_worker_boundary,
)

# States worth a stdout line the moment a task lands there: the "your crew
# needs you" and "here's your PR" moments. A backgrounded `run`/`daemon`
# streams these so the calling session can watch this process's stdout
# (e.g. the Monitor tool) instead of polling `status` -- the event-driven
# wake firstmate's watcher gives its user, built on the events table
# that's already this system's source of truth (design.md section 3).
_NOTIFY_STATES = ("needs_human", "delivered", "failed", "dependency_blocked")


def _load_repo_registry(path: str = "repos.toml") -> dict:
    """Flat name -> path lookup, see repos.toml and docs/usage.md. Missing
    file just means no registry is in use; not an error."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        data = tomllib.load(f)
    return data.get("repos", {})


def cmd_add_task(args) -> int:
    conn = connect(args.db)
    repo = _load_repo_registry().get(args.repo, args.repo)
    task_id = create_task(
        conn, title=args.title, brief=args.brief, repo=repo,
        delivery_mode=args.delivery_mode, verify_cmd=args.verify_cmd,
        max_retries=args.max_retries, depends_on=args.depends_on,
        output_artifacts=args.output_artifacts, output_schema=args.output_schema,
        input_contract=args.input_contract, node_verify_cmd=args.node_verify_cmd,
        repair_policy=args.repair_policy,
    )
    print(task_id)
    return 0


def _build_scheduler(conn, args) -> Scheduler:
    cfg = config.load(args.config)
    if args.fake_worker:
        spawn_worker = spawn_fake_worker
    elif getattr(args, "direct_cli", False):
        spawn_worker = spawn_cli_worker
    else:
        spawn_worker = spawn_sdk_worker
    validate_worker_boundary(
        fake_worker=args.fake_worker,
        external_isolation=args.external_isolation,
        trusted_development=args.trusted_development,
    )
    supervisor = always_escalate if args.fake_supervisor else partial(
        invoke_supervisor, model=args.supervisor_model or cfg.model_supervisor)
    return Scheduler(
        conn, args.repo_root, Path(args.worktree_root),
        max_concurrency=args.max_concurrency, spawn_worker=spawn_worker,
        worker_model=args.worker_model or cfg.model_worker, supervisor=supervisor,
        max_nudges=cfg.max_nudges, stall_threshold_s=cfg.stall_threshold_s,
        repeated_failure_threshold=cfg.repeated_failure_threshold,
        wait_ceiling_s=cfg.wait_ceiling_s, verify_timeout_s=cfg.verify_timeout_s,
        transcript_tail_tokens=cfg.transcript_tail_tokens, yolo=args.yolo,
        base_branch=args.base_branch,
    )


async def _notify_loop(db_path: str, poll_s: float = 1.0) -> None:
    """Poll `events` (a second connection -- WAL mode makes this a safe
    concurrent reader alongside the scheduler's writer) for state changes
    landing in _NOTIFY_STATES, printing one line per hit. Runs alongside
    run_until_settled() until cancelled."""
    notify_conn = connect(db_path)
    try:
        last_seq = notify_conn.execute("SELECT COALESCE(MAX(seq), 0) c FROM events").fetchone()["c"]
        while True:
            rows = notify_conn.execute(
                "SELECT seq, task_id, payload FROM events "
                "WHERE seq > ? AND type = 'task.state_changed' ORDER BY seq", (last_seq,),
            ).fetchall()
            for row in rows:
                last_seq = row["seq"]
                to_state = json.loads(row["payload"])["to"]
                if to_state in _NOTIFY_STATES:
                    title = notify_conn.execute(
                        "SELECT title FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()["title"]
                    print(f"[{to_state}] {row['task_id']}  {title}", flush=True)
            await asyncio.sleep(poll_s)
    finally:
        notify_conn.close()


async def _run_with_notify(coro, db_path: str) -> None:
    notifier = asyncio.create_task(_notify_loop(db_path))
    try:
        await coro
    finally:
        notifier.cancel()
        await asyncio.gather(notifier, return_exceptions=True)


def cmd_run(args) -> int:
    conn = connect(args.db)
    try:
        scheduler = _build_scheduler(conn, args)
    except WorkerIsolationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    asyncio.run(_run_with_notify(scheduler.run_until_settled(), args.db))
    _print_status_table(conn)
    return 0


def cmd_daemon(args) -> int:
    conn = connect(args.db)
    try:
        scheduler = _build_scheduler(conn, args)
    except WorkerIsolationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"watching {args.db} for tasks (Ctrl-C to stop)...", file=sys.stderr)
    try:
        asyncio.run(_run_with_notify(
            scheduler.run_until_settled(forever=True, poll_interval_s=args.poll_interval), args.db))
    except KeyboardInterrupt:
        print("\nshutting down...", file=sys.stderr)
    return 0


def cmd_answer(args) -> int:
    conn = connect(args.db)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if row is None:
        print(f"unknown task {args.task_id!r}", file=sys.stderr)
        return 2
    task = dict(row)
    if task["state"] != "needs_human":
        print(f"task {args.task_id} is {task['state']!r}, not needs_human", file=sys.stderr)
        return 1

    s = append_event(conn, source="human", type="human.messaged", task_id=args.task_id,
                     payload={"message": args.message})
    # Symmetric to the supervisor's own `restart`: fold the answer into the
    # brief as feedback for a fresh attempt. There's no live session left to
    # inject into by the time a task reaches needs_human -- escalation always
    # tears the worker down (scheduler/core.py's _handle_triage) -- so
    # "answer" means requeue with more context, not resume in place.
    new_brief = f"{task['brief']}\n\nAnswer from the manager:\n{args.message}"
    transition(conn, args.task_id, "queued", cause_seq=s, brief=new_brief)
    print(f"{args.task_id}: needs_human -> queued (answer folded into brief)")
    return 0


def _print_status_table(conn) -> None:
    rows = conn.execute("SELECT id, title, state, retries FROM tasks ORDER BY created_at").fetchall()
    if not rows:
        print("no tasks")
        return
    for r in rows:
        print(f"{r['id']}  {r['state']:<12}  retries={r['retries']}  {r['title']}")


def _print_task_detail(conn, task_id: str) -> int:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        print(f"unknown task {task_id!r}", file=sys.stderr)
        return 2
    task = dict(row)
    print(f"{task['id']}  {task['title']}")
    print(f"  state:         {task['state']} (retries {task['retries']}/{task['max_retries']})")
    print(f"  repo:          {task['repo']}")
    print(f"  delivery_mode: {task['delivery_mode']}")
    print(f"  verify_cmd:    {task['verify_cmd']}")

    if task["state"] == "needs_human":
        acted = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND type = 'supervisor.acted' "
            "ORDER BY seq DESC LIMIT 1", (task_id,)).fetchone()
        if acted:
            payload = json.loads(acted["payload"])
            print(f"  escalated:     {payload.get('summary', '')}")
            print(f"  question:      {payload.get('question', '')}")
            for i, opt in enumerate(payload.get("options", [])):
                marker = " (recommended)" if payload.get("recommended") == i else ""
                print(f"    [{i}] {opt}{marker}")
            print(f'  resolve with:  orchestrator answer {task_id} "..."')
    elif task["state"] == "delivered":
        delivery_event = conn.execute(
            "SELECT type, payload FROM events WHERE task_id = ? AND source = 'delivery' "
            "AND type IN ('delivery.pr_opened', 'delivery.merged_local', "
            "'delivery.report_written') ORDER BY seq DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        verify_event = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND type = 'verify.passed' "
            "ORDER BY seq DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if delivery_event:
            payload = json.loads(delivery_event["payload"])
            if delivery_event["type"] == "delivery.pr_opened":
                print(f"  delivered:     PR opened at {payload.get('url', '')}")
                print(f"  branch:        {payload.get('branch', '')}")
                print(f"  commit:        {payload.get('commit_sha', '')}")
            elif delivery_event["type"] == "delivery.merged_local":
                before = payload.get("before_sha", "")
                after = payload.get("after_sha", "")
                print(f"  delivered:     merged locally {before[:12]}..{after[:12]}")
                print(f"  review diff:   git -C {task['repo']} diff {before}..{after}")
                print(f"  review log:    git -C {task['repo']} log --oneline {before}..{after}")
            elif delivery_event["type"] == "delivery.report_written":
                print(f"  delivered:     report written to {payload.get('path', '')}")
        if verify_event:
            verify_payload = json.loads(verify_event["payload"])
            if verify_payload.get("patch_path"):
                print(f"  patch:         {verify_payload['patch_path']}")
    return 0


def _print_status_digest(conn) -> None:
    """One-shot, terser summary: state counts, needs_human questions, session
    count. Pull-only read of current state -- no loop, no polling, no
    supervision (design.md's non-goals rule out a chat liaison front-end)."""
    rows = conn.execute("SELECT state FROM tasks").fetchall()
    if not rows:
        print("no tasks")
        return
    counts = Counter(r["state"] for r in rows)
    summary = ", ".join(f"{n} {s}" for s, n in counts.most_common())
    print(f"{len(rows)} tasks: {summary}")

    session_count = conn.execute(
        "SELECT COUNT(DISTINCT session_id) c FROM tasks WHERE session_id IS NOT NULL"
    ).fetchone()["c"]
    print(f"worker sessions spawned: {session_count}")

    needs_human = conn.execute(
        "SELECT id, title FROM tasks WHERE state = 'needs_human' ORDER BY created_at").fetchall()
    if needs_human:
        print(f"\nneeds_human ({len(needs_human)}):")
        for row in needs_human:
            acted = conn.execute(
                "SELECT payload FROM events WHERE task_id = ? AND type = 'supervisor.acted' "
                "ORDER BY seq DESC LIMIT 1", (row["id"],)).fetchone()
            question = json.loads(acted["payload"]).get("question", "") if acted else ""
            print(f"  {row['id']}  {row['title']}")
            if question:
                print(f"    asking: {question}")


def cmd_status(args) -> int:
    conn = connect(args.db)
    if args.digest:
        _print_status_digest(conn)
        return 0
    if args.task_id:
        return _print_task_detail(conn, args.task_id)
    _print_status_table(conn)
    return 0


def _add_scheduler_args(p) -> None:
    p.add_argument("--db", default="data/orchestrator.db")
    p.add_argument("--repo-root", required=True)
    p.add_argument("--worktree-root", default="data/worktrees")
    p.add_argument("--base-branch", default="main")
    p.add_argument("--max-concurrency", type=int, default=4)
    p.add_argument("--worker-model")
    p.add_argument("--supervisor-model")
    p.add_argument("--fake-worker", action="store_true",
                   help="scripted FakeWorker instead of a real Claude Code session "
                        "(free, deterministic -- for dry runs)")
    p.add_argument("--direct-cli", action="store_true",
                   help="launch the installed claude CLI directly instead of the Agent SDK")
    p.add_argument("--fake-supervisor", action="store_true",
                   help="always escalate instead of calling a live LLM supervisor")
    boundary = p.add_mutually_exclusive_group()
    boundary.add_argument(
        "--external-isolation", action="store_true",
        help="declare that Harbor/container isolation is already present",
    )
    boundary.add_argument(
        "--trusted-development", action="store_true",
        help="explicitly allow direct host execution; not benchmark isolation",
    )
    p.add_argument("--yolo", action="store_true")
    p.add_argument("--config", help="TOML overriding config defaults (design.md section 12)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator")
    sub = p.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add-task", help="create a new task")
    p_add.add_argument("--db", default="data/orchestrator.db")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--brief", required=True)
    p_add.add_argument("--repo", required=True)
    p_add.add_argument("--delivery-mode", required=True, choices=["pr", "local", "scout"])
    p_add.add_argument("--verify-cmd")
    p_add.add_argument("--depends-on", action="append", default=[], metavar="TASK_ID")
    p_add.add_argument("--max-retries", type=int, default=2)
    p_add.add_argument("--output-artifacts", help="JSON list/path map of required output artifacts")
    p_add.add_argument("--output-schema", help="JSON output schema declaration")
    p_add.add_argument("--input-contract", help="JSON dependency input contract")
    p_add.add_argument("--node-verify-cmd", help="public node-level verification command")
    p_add.add_argument("--repair-policy", help="JSON repair policy declaration")
    p_add.set_defaults(func=cmd_add_task)

    p_run = sub.add_parser("run", help="drive every pending task to a resting state, then exit")
    _add_scheduler_args(p_run)
    p_run.set_defaults(func=cmd_run)

    p_daemon = sub.add_parser(
        "daemon", help="like run, but keeps polling for newly added tasks instead of exiting")
    _add_scheduler_args(p_daemon)
    p_daemon.add_argument("--poll-interval", type=float, default=1.0)
    p_daemon.set_defaults(func=cmd_daemon)

    p_answer = sub.add_parser("answer", help="resolve a needs_human task by answering its question")
    p_answer.add_argument("task_id")
    p_answer.add_argument("message")
    p_answer.add_argument("--db", default="data/orchestrator.db")
    p_answer.set_defaults(func=cmd_answer)

    p_status = sub.add_parser("status", help="list tasks, or show one task's detail")
    p_status.add_argument("task_id", nargs="?")
    p_status.add_argument("--db", default="data/orchestrator.db")
    p_status.add_argument("--digest", action="store_true",
        help="one-shot terse summary: state counts, needs_human questions, session count")
    p_status.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
