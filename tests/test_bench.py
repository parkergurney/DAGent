import json
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator.bench.cli import main as bench_main
from orchestrator.bench.report import summarize_db
from orchestrator.bench.report import find_run_dbs
from orchestrator.bench.runner import _materialize_worker_repo, run_benchmark
from orchestrator.bench.suite import load_suite
from orchestrator.store import connect
from tests.helpers import git, init_repo


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


def test_benchmark_rejects_existing_hidden_material_before_overwrite(tmp_path):
    repo = init_repo(tmp_path)
    hidden = repo / "hidden_tests"
    hidden.mkdir()
    (hidden / "secret.py").write_text("secret\n")
    suite_path = _suite_file(tmp_path, repo)
    suite_path.write_text(suite_path.read_text().replace(
        'verify_cmd = "true"', 'verify_cmd = "true"\nprotected_paths = ["hidden_tests/**"]'))
    out_dir = tmp_path / "bench"
    prior = out_dir / "toy" / "sequential-seed1"
    prior.mkdir(parents=True)
    (prior / "historical.txt").write_text("keep me\n")

    with pytest.raises(ValueError, match="protected hidden-test material"):
        run_benchmark(suite_path, condition="sequential", out_dir=out_dir, overwrite=True)

    assert (prior / "historical.txt").read_text() == "keep me\n"


def test_benchmark_rejects_existing_non_hidden_protected_material(tmp_path):
    repo = init_repo(tmp_path)
    grader = repo / "grader"
    grader.mkdir()
    (grader / "secret.json").write_text("secret\n")
    suite_path = _suite_file(tmp_path, repo)
    suite_path.write_text(suite_path.read_text().replace(
        'verify_cmd = "true"', 'verify_cmd = "true"\nprotected_paths = ["grader/**"]'))

    with pytest.raises(ValueError, match="protected hidden-test material"):
        run_benchmark(suite_path, condition="sequential", out_dir=tmp_path / "bench")


def test_benchmark_rejects_non_hidden_protected_material_in_history(tmp_path):
    repo = init_repo(tmp_path)
    grader = repo / "grader"
    grader.mkdir()
    (grader / "secret.json").write_text("secret\n")
    git("add", "grader/secret.json", cwd=repo)
    git("commit", "-qm", "add grader material", cwd=repo)
    (grader / "secret.json").unlink()
    git("commit", "-am", "remove grader material", cwd=repo)
    suite_path = _suite_file(tmp_path, repo)
    suite_path.write_text(suite_path.read_text().replace(
        'verify_cmd = "true"', 'verify_cmd = "true"\nprotected_paths = ["grader/**"]'))

    with pytest.raises(ValueError, match="protected hidden-test material"):
        run_benchmark(suite_path, condition="sequential", out_dir=tmp_path / "bench")


def test_worker_repo_snapshot_has_no_source_history_objects(tmp_path):
    repo = init_repo(tmp_path)
    marker = "UNIQUE_HIDDEN_GIT_MARKER_7f2c"
    (repo / "historical_hidden.py").write_text(marker + "\n")
    git("add", "historical_hidden.py", cwd=repo)
    git("commit", "-qm", "temporary evaluator material", cwd=repo)
    (repo / "historical_hidden.py").unlink()
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "remove evaluator material", cwd=repo)

    worker_repo = _materialize_worker_repo(repo, "main", tmp_path / "worker-repo")
    for command in (
        ["log", "--all", "-p"], ["rev-list", "--all", "--objects"],
        ["fsck", "--full", "--no-reflogs", "--unreachable"],
        ["show-ref"], ["tag", "-l"], ["remote", "-v"], ["reflog", "--all"],
    ):
        result = subprocess.run(["git", *command], cwd=worker_repo,
                                capture_output=True, text=True, check=False)
        assert marker not in result.stdout + result.stderr
    assert not (worker_repo / ".git" / "objects" / "info" / "alternates").exists()
    assert not (worker_repo / ".git" / "refs" / "remotes").exists()

    worktree = tmp_path / "worker-slots" / "slot-0"
    git("worktree", "add", "-q", str(worktree), "HEAD", cwd=worker_repo)
    (worktree / "public-change.txt").write_text("public\n")
    git("add", "public-change.txt", cwd=worktree)
    git("commit", "-qm", "public worker change", cwd=worktree)
    assert git("status", "--porcelain", cwd=worktree).stdout == ""


def test_benchmark_resolves_symlinked_worker_slot_before_allowlist_check(tmp_path):
    repo = init_repo(tmp_path)
    worktrees = tmp_path / "worker-slots"
    worktrees.mkdir()
    actual = tmp_path / "actual-slot"
    actual.mkdir()
    (worktrees / "slot-0").symlink_to(actual, target_is_directory=True)
    source = actual / "grader"
    suite_path = tmp_path / "suite.toml"
    suite_path.write_text(
        f'''[bench]\nname = "toy"\nrepo = "{repo}"\nverify_cmd = "true"\n'''
        f'''hidden_source_paths = ["{source}"]\n\n'''
        '[[tasks]]\nid = "a"\ntitle = "A"\nbrief = "clean"\n'
    )

    with pytest.raises(ValueError, match="hidden verifier source"):
        run_benchmark(
            suite_path, condition="sequential", out_dir=tmp_path / "bench",
            worktree_root=worktrees,
        )


def test_benchmark_rejects_hidden_source_inside_worker_allowlist(tmp_path):
    repo = init_repo(tmp_path)
    worktrees = tmp_path / "worker-slots"
    source = worktrees / "slot-0" / "hidden_tests"
    suite_path = tmp_path / "suite.toml"
    suite_path.write_text(
        f'''[bench]\nname = "toy"\nrepo = "{repo}"\nverify_cmd = "true"\n'''
        f'''hidden_source_paths = ["{source}"]\n\n'''
        '[[tasks]]\nid = "a"\ntitle = "A"\nbrief = "clean"\n'
    )

    with pytest.raises(ValueError, match="hidden verifier source"):
        run_benchmark(
            suite_path, condition="sequential", out_dir=tmp_path / "bench",
            worktree_root=worktrees,
        )


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


def test_baseline_conditions_use_run_scoped_delivery(tmp_path):
    repo = init_repo(tmp_path)
    suite_path = _suite_file(tmp_path, repo)

    manifest = run_benchmark(
        suite_path, condition="sequential", out_dir=tmp_path / "bench",
        fake_worker=True, overwrite=True,
    )

    conn = connect(manifest.db)
    delivery_event = conn.execute(
        "SELECT payload FROM events WHERE type='delivery.report_written'"
    ).fetchone()
    assert delivery_event is not None
    report_path = Path(json.loads(delivery_event["payload"])['path'])
    assert report_path.is_relative_to(Path(manifest.run_dir) / "artifacts")
    assert report_path.exists()


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


def test_benchmark_artifacts_are_scoped_to_each_run(tmp_path):
    repo = init_repo(tmp_path)
    suite_path = _suite_file(tmp_path, repo)
    out_dir = tmp_path / "bench"

    first = run_benchmark(suite_path, condition="sequential", out_dir=out_dir,
                          seed=1, fake_worker=True, overwrite=True)
    second = run_benchmark(suite_path, condition="sequential", out_dir=out_dir,
                           seed=2, fake_worker=True, overwrite=True)

    first_payload = json.loads(connect(first.db).execute(
        "SELECT payload FROM events WHERE type='verify.passed'"
    ).fetchone()["payload"])
    second_payload = json.loads(connect(second.db).execute(
        "SELECT payload FROM events WHERE type='verify.passed'"
    ).fetchone()["payload"])
    assert Path(first_payload["output_path"]).is_relative_to(Path(first.run_dir) / "artifacts")
    assert Path(second_payload["output_path"]).is_relative_to(Path(second.run_dir) / "artifacts")
    assert Path(first_payload["patch_path"]).is_relative_to(Path(first.run_dir) / "artifacts")
    assert Path(second_payload["patch_path"]).is_relative_to(Path(second.run_dir) / "artifacts")
    assert first.run_dir != second.run_dir
    assert first_payload["output_path"] != second_payload["output_path"]
    assert first_payload["patch_path"] != second_payload["patch_path"]


def test_report_selection_excludes_nested_history_and_rejects_duplicate_ids(tmp_path):
    repo = init_repo(tmp_path)
    suite_path = _suite_file(tmp_path, repo)
    out_dir = tmp_path / "bench"
    first = run_benchmark(suite_path, condition="sequential", out_dir=out_dir,
                          seed=1, fake_worker=True, overwrite=True)
    second = run_benchmark(suite_path, condition="sequential", out_dir=out_dir,
                           seed=2, fake_worker=True, overwrite=True)

    nested = out_dir / "archive" / "old"
    nested.mkdir(parents=True)
    shutil.copy2(first.db, nested / "run.db")
    assert find_run_dbs(out_dir) == sorted([Path(first.db).resolve(), Path(second.db).resolve()])

    duplicate = out_dir / "toy" / "duplicate"
    duplicate.mkdir()
    shutil.copy2(first.db, duplicate / "run.db")
    (duplicate / "manifest.json").write_text(
        (Path(first.run_dir) / "manifest.json").read_text()
    )
    with pytest.raises(ValueError, match="duplicate benchmark run_id"):
        find_run_dbs(out_dir)


def test_report_selection_returns_exactly_nine_runs(tmp_path):
    repo = init_repo(tmp_path)
    suite_path = _suite_file(tmp_path, repo)
    out_dir = tmp_path / "bench"
    for seed in range(1, 10):
        run_benchmark(suite_path, condition="sequential", out_dir=out_dir,
                      seed=seed, fake_worker=True, overwrite=True)

    assert len(find_run_dbs(out_dir)) == 9


@pytest.mark.parametrize("condition", ["sequential", "naive-parallel", "orchestrator"])
def test_all_conditions_use_the_same_detached_hidden_verifier(tmp_path, condition):
    condition_root = tmp_path / condition
    condition_root.mkdir()
    repo = init_repo(condition_root)
    suite_path = condition_root / "suite.toml"
    suite_path.write_text(f'''[bench]
name = "{condition}"
repo = "{repo}"
verify_cmd = "true"
setup_cmd = "mkdir -p hidden_tests && printf hidden > hidden_tests/secret.txt"
protected_paths = ["hidden_tests/**"]
delivery_mode = "scout"

[[tasks]]
id = "a"
title = "A"
brief = "clean"
hidden_cmd = "test -f hidden_tests/secret.txt && test -f output.txt"
''')

    manifest = run_benchmark(
        suite_path, condition=condition, out_dir=tmp_path / "bench", fake_worker=True,
        fake_supervisor=True, overwrite=True,
    )

    conn = connect(manifest.db)
    assert conn.execute("SELECT state FROM tasks WHERE id='bench-a'").fetchone()["state"] == "delivered"
    assert not list((Path(manifest.run_dir) / "worktrees").rglob("hidden_tests"))
