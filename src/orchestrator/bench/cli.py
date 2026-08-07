"""`bench-run`: M6 benchmark harness CLI."""
import argparse
import sys
from pathlib import Path

from orchestrator.bench.report import (
    find_run_dbs,
    format_summary_table,
    format_table,
    summarize_db,
    summarize_groups,
)
from orchestrator.bench.runner import CONDITIONS, run_benchmark


def cmd_run(args) -> int:
    manifest = run_benchmark(
        args.suite,
        condition=args.condition,
        out_dir=args.out_dir,
        seed=args.seed,
        repo_root=args.repo_root,
        worktree_root=args.worktree_root,
        max_concurrency=args.max_concurrency,
        worker_model=args.worker_model,
        supervisor_model=args.supervisor_model,
        config_path=args.config,
        fake_worker=args.fake_worker,
        fake_supervisor=args.fake_supervisor,
        kill_one_after_s=args.kill_one_after,
        overwrite=args.overwrite,
    )
    print(f"run_id: {manifest.run_id}")
    print(f"db:     {manifest.db}")
    print(f"dir:    {manifest.run_dir}")
    return 0


def cmd_report(args) -> int:
    try:
        dbs = find_run_dbs(args.path)
    except ValueError as exc:
        print(f"invalid benchmark report selection: {exc}", file=sys.stderr)
        return 2
    if not dbs:
        print(f"no benchmark run DBs found under {args.path}", file=sys.stderr)
        return 1
    rows = [summarize_db(db) for db in dbs]
    if args.summary:
        print(format_summary_table(summarize_groups(rows, group_by=args.group_by)))
    else:
        print(format_table(rows))
    return 0


def cmd_example_suite(args) -> int:
    path = Path(args.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_SUITE)
    print(path)
    return 0


EXAMPLE_SUITE = """[bench]
name = "toy-suite"
repo = "/absolute/path/to/fresh-target-repo"
base_branch = "main"
verify_cmd = "python -m pytest -q"
setup_cmd = "python -m pip install -e ."
protected_paths = ["hidden_tests/**"]
delivery_mode = "scout"
max_retries = 2

[[tasks]]
id = "feature-a"
title = "Implement feature A"
brief = "Natural-language worker brief. Do not mention hidden_tests."
hidden_cmd = "python -m pytest hidden_tests/test_feature_a.py -q"

[[tasks]]
id = "feature-b"
title = "Implement feature B after A"
brief = "Natural-language worker brief for the dependent task."
depends_on = ["feature-a"]
hidden_cmd = "python -m pytest hidden_tests/test_feature_b.py -q"
"""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bench-run")
    sub = p.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run one benchmark condition/seed")
    p_run.add_argument("--suite", required=True)
    p_run.add_argument("--condition", required=True, choices=sorted(CONDITIONS))
    p_run.add_argument("--out-dir", default="data/bench")
    p_run.add_argument("--seed", type=int, default=1)
    p_run.add_argument("--repo-root")
    p_run.add_argument("--worktree-root")
    p_run.add_argument("--max-concurrency", type=int, default=4)
    p_run.add_argument("--worker-model")
    p_run.add_argument("--supervisor-model")
    p_run.add_argument("--config")
    p_run.add_argument("--fake-worker", action="store_true")
    p_run.add_argument("--fake-supervisor", action="store_true")
    p_run.add_argument("--kill-one-after", type=float,
                       help="seconds after run start to kill one active worker")
    p_run.add_argument("--overwrite", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="summarize one run.db or a runs directory")
    p_report.add_argument("path", nargs="?", default="data/bench")
    p_report.add_argument("--summary", action="store_true",
                          help="print grouped mean/spread rollups instead of per-run rows")
    p_report.add_argument("--group-by", default="condition",
                          help="comma-separated fields: condition, suite, seed "
                               "(default: condition)")
    p_report.set_defaults(func=cmd_report)

    p_example = sub.add_parser("example-suite", help="write an example suite TOML")
    p_example.add_argument("path", nargs="?", default="bench/example-suite.toml")
    p_example.set_defaults(func=cmd_example_suite)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
