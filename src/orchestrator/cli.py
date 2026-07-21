"""`orchestrator` console script: the operator-facing CLI over the library
described in docs/usage.md. Five subcommands, each a thin wrapper over
existing pieces -- no new control-flow, just the plumbing to drive them
without hand-writing a Python script per batch:

    orchestrator add-task --repo R --title T --brief B --delivery-mode M [...]
    orchestrator run       [--fake-worker] [--fake-supervisor] ...
    orchestrator daemon    [--poll-interval S] ...   # like run, never exits
    orchestrator answer TASK_ID "message"
    orchestrator status [TASK_ID]
"""
import argparse
import asyncio
import json
import sys
from functools import partial
from pathlib import Path

from orchestrator import config
from orchestrator.scheduler import Scheduler
from orchestrator.store import append_event, connect, create_task, transition
from orchestrator.supervisor import always_escalate, invoke_supervisor
from orchestrator.worker import spawn_fake_worker, spawn_sdk_worker


def cmd_add_task(args) -> int:
    conn = connect(args.db)
    task_id = create_task(
        conn, title=args.title, brief=args.brief, repo=args.repo,
        delivery_mode=args.delivery_mode, verify_cmd=args.verify_cmd,
        setup_cmd=args.setup_cmd, max_retries=args.max_retries,
        depends_on=args.depends_on,
    )
    print(task_id)
    return 0


def _build_scheduler(conn, args) -> Scheduler:
    cfg = config.load(args.config)
    spawn_worker = spawn_fake_worker if args.fake_worker else spawn_sdk_worker
    supervisor = always_escalate if args.fake_supervisor else partial(
        invoke_supervisor, model=args.supervisor_model or cfg.model_supervisor)
    return Scheduler(
        conn, args.repo_root, Path(args.worktree_root),
        max_concurrency=args.max_concurrency, spawn_worker=spawn_worker,
        worker_model=args.worker_model or cfg.model_worker, supervisor=supervisor,
        max_nudges=cfg.max_nudges, stall_threshold_s=cfg.stall_threshold_s,
        wait_ceiling_s=cfg.wait_ceiling_s, verify_timeout_s=cfg.verify_timeout_s,
        transcript_tail_tokens=cfg.transcript_tail_tokens, yolo=args.yolo,
    )


def cmd_run(args) -> int:
    conn = connect(args.db)
    scheduler = _build_scheduler(conn, args)
    asyncio.run(scheduler.run_until_settled())
    _print_status_table(conn)
    return 0


def cmd_daemon(args) -> int:
    conn = connect(args.db)
    scheduler = _build_scheduler(conn, args)
    print(f"watching {args.db} for tasks (Ctrl-C to stop)...", file=sys.stderr)
    try:
        asyncio.run(scheduler.run_until_settled(forever=True, poll_interval_s=args.poll_interval))
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
    if task["setup_cmd"]:
        print(f"  setup_cmd:     {task['setup_cmd']}")

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
    return 0


def cmd_status(args) -> int:
    conn = connect(args.db)
    if args.task_id:
        return _print_task_detail(conn, args.task_id)
    _print_status_table(conn)
    return 0


def _add_scheduler_args(p) -> None:
    p.add_argument("--db", default="data/orchestrator.db")
    p.add_argument("--repo-root", required=True)
    p.add_argument("--worktree-root", default="data/worktrees")
    p.add_argument("--max-concurrency", type=int, default=4)
    p.add_argument("--worker-model")
    p.add_argument("--supervisor-model")
    p.add_argument("--fake-worker", action="store_true",
                   help="scripted FakeWorker instead of a real Claude Code session "
                        "(free, deterministic -- for dry runs)")
    p.add_argument("--fake-supervisor", action="store_true",
                   help="always escalate instead of calling a live LLM supervisor")
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
    p_add.add_argument("--setup-cmd",
        help="run before verify_cmd, in the baseline scratch checkout and the "
             "worker's own worktree (e.g. 'npm install') -- design.md section 7")
    p_add.add_argument("--depends-on", action="append", default=[], metavar="TASK_ID")
    p_add.add_argument("--max-retries", type=int, default=2)
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
    p_status.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
