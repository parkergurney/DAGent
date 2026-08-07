import json

import pytest

from orchestrator.bench.cli import main as bench_main
from orchestrator.bench.report import summarize_db
from orchestrator.bench.runner import run_benchmark
from orchestrator.bench.suite import load_suite
from orchestrator.store import connect
from tests.helpers import init_repo


def _suite_file(tmp_path, repo, *, name="toy", hidden_cmd="true", tasks=None):
    tasks = tasks or [
        """
[[tasks]]
id = "a"
title = "A"
brief = "clean"
hidden_cmd = "{hidden_cmd}"
""".format(hidden_cmd=hidden_cmd)
    ]
    path = tmp_path / "suite.toml"
    path.write_text("""
[bench]
name = "{name}"
repo = "{repo}"
verify_cmd = "true"
delivery_mode = "scout"

{tasks}
""".format(name=name, repo=repo, tasks="\n".join(tasks)))
    return path


def test_suite_loader_defaults_and_dependencies(tmp_path):
    repo = init_repo(tmp_path)
    suite_path = _suite_file(tmp_path, repo, tasks=[
        """
[[tasks]]
id = "a"
title = "A"
brief = "clean"
""",
        """
[[tasks]]
id = "b"
title = "B"
brief = "clean"
depends_on = ["a"]
""",
    ])

    suite = load_suite(suite_path)

    assert suite.name == "toy"
    assert suite.repo == str(repo)
    assert suite.tasks[1].depends_on == ("a",)
    assert suite.tasks[0].delivery_mode == "scout"


def test_suite_loader_rejects_forward_dependency(tmp_path):
    repo = init_repo(tmp_path)
    suite_path = _suite_file(tmp_path, repo, tasks=[
        """
[[tasks]]
id = "b"
title = "B"
brief = "clean"
depends_on = ["a"]
""",
    ])

    with pytest.raises(ValueError, match="depends on unknown"):
        load_suite(suite_path)


def test_sequential_baseline_run_records_metrics(tmp_path):
    repo = init_repo(tmp_path)
    suite_path = _suite_file(tmp_path, repo)

    manifest = run_benchmark(
        suite_path, condition="sequential", out_dir=tmp_path / "bench",
        fake_worker=True, overwrite=True,
    )

    conn = connect(manifest.db)
    row = conn.execute("SELECT state, hidden_cmd FROM tasks WHERE id='bench-a'").fetchone()
    assert row["state"] == "delivered"
    assert row["hidden_cmd"] == "true"
    assert conn.execute(
        "SELECT COUNT(*) c FROM events WHERE type='bench.delivered'"
    ).fetchone()["c"] == 1

    summary = summarize_db(manifest.db)
    assert summary.condition == "sequential"
    assert summary.tasks == 1
    assert summary.delivered == 1


def test_naive_parallel_reserves_distinct_queued_tasks(tmp_path):
    repo = init_repo(tmp_path)
    suite_path = _suite_file(tmp_path, repo, tasks=[
        '[[tasks]]\nid = "a"\ntitle = "A"\nbrief = "clean"',
        '[[tasks]]\nid = "b"\ntitle = "B"\nbrief = "clean"',
        '[[tasks]]\nid = "c"\ntitle = "C"\nbrief = "clean"',
        '[[tasks]]\nid = "d"\ntitle = "D"\nbrief = "clean"',
    ])

    manifest = run_benchmark(
        suite_path, condition="naive-parallel", out_dir=tmp_path / "bench",
        max_concurrency=4, fake_worker=True, overwrite=True,
    )

    summary = summarize_db(manifest.db)
    assert summary.tasks == 4
    assert summary.delivered == 4


def test_hidden_check_failure_counts_as_verify_failure(tmp_path):
    repo = init_repo(tmp_path)
    suite_path = _suite_file(tmp_path, repo, hidden_cmd="test -f missing.marker")

    manifest = run_benchmark(
        suite_path, condition="sequential", out_dir=tmp_path / "bench",
        fake_worker=True, overwrite=True,
    )

    conn = connect(manifest.db)
    task = conn.execute("SELECT state FROM tasks WHERE id='bench-a'").fetchone()
    verify = conn.execute(
        "SELECT payload FROM events WHERE type='verify.failed' ORDER BY seq DESC LIMIT 1"
    ).fetchone()

    assert task["state"] == "failed"
    assert json.loads(verify["payload"])["cause"] == "hidden_tests_failed"


def test_orchestrator_condition_runs_through_scheduler(tmp_path):
    repo = init_repo(tmp_path)
    suite_path = _suite_file(tmp_path, repo)

    manifest = run_benchmark(
        suite_path, condition="orchestrator", out_dir=tmp_path / "bench",
        fake_worker=True, fake_supervisor=True, overwrite=True,
    )

    summary = summarize_db(manifest.db)
    assert summary.condition == "orchestrator"
    assert summary.delivered == 1


def test_bench_cli_report(tmp_path, capsys):
    repo = init_repo(tmp_path)
    suite_path = _suite_file(tmp_path, repo)
    out_dir = tmp_path / "bench"
    run_benchmark(suite_path, condition="sequential", out_dir=out_dir,
                  fake_worker=True, overwrite=True)

    rc = bench_main(["report", str(out_dir)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "condition" in out
    assert "sequential" in out


def test_bench_cli_report_recurses_and_summarizes_across_suites(tmp_path, capsys):
    out_dir = tmp_path / "bench"
    for suite_name in ("suite_one", "suite_two"):
        repo_parent = tmp_path / suite_name / "target"
        repo_parent.mkdir(parents=True)
        repo = init_repo(repo_parent)
        suite_path = _suite_file(tmp_path / suite_name, repo, name=suite_name)
        run_benchmark(suite_path, condition="sequential", out_dir=out_dir,
                      fake_worker=True, overwrite=True)

    rc = bench_main(["report", str(out_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "suite_one-sequential-seed1" in out
    assert "suite_two-sequential-seed1" in out

    rc = bench_main(["report", str(out_dir), "--summary", "--group-by", "condition"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "group\truns\ttasks" in out
    assert "sequential\t2\t2\t2\t100.0%" in out
