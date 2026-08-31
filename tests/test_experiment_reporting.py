import json

from dagent.experiment import (
    classify_cell, render_markdown, summarize_cells, validate_comparison_cells,
)


def _manifest(**overrides):
    value = {
        "policy": "orchestrator",
        "seed": 7,
        "task_graph": {"count": 1, "sha256": "graph"},
        "repository": {"base_sha": "base"},
        "backend": "fake",
        "context_length": 4096,
        "limits": {"max_concurrency": 2},
        "authentication": {"mechanism": "none"},
        "fault_target_reachability": {"enabled": False},
    }
    value.update(overrides)
    return value


def _metrics(**overrides):
    value = {
        "tasks": 1,
        "delivered": 1,
        "state_counts": {"delivered": 1},
        "fault_target_reached": False,
        "cost_usd": 0.1,
        "wall_time_s": 2.0,
    }
    value.update(overrides)
    return value


def test_success_is_outcome_and_runtime_eligible():
    result = classify_cell(
        manifest=_manifest(), metrics=_metrics(),
        result={"task_states": {"task": "delivered"}}, run_completed=True,
    )

    assert result["cell_status"] == "success"
    assert result["eligible_for_outcome"] is True
    assert result["runtime_comparable"] is True
    assert result["verified_completion_rate"] == 1.0


def test_metrics_expose_terminal_state_counts(tmp_path):
    from dagent.metrics import export_metrics
    from dagent.store import connect, create_task, transition

    conn = connect()
    task_id = create_task(
        conn, title="terminal", brief="clean", repo=str(tmp_path),
        delivery_mode="scout", verify_cmd="true",
    )
    cause = conn.execute("select max(seq) as seq from events").fetchone()["seq"]
    transition(conn, task_id, "dependency_blocked", cause_seq=cause)

    metrics = export_metrics(conn)
    assert metrics["state_counts"] == {"dependency_blocked": 1}
    assert metrics["terminal_state_counts"] == {"dependency_blocked": 1}


def test_unsettled_run_is_censored_not_failed():
    result = classify_cell(
        manifest=_manifest(), metrics=_metrics(state_counts={"running": 1}, delivered=0),
        result={"task_states": {"task": "running"}}, run_completed=False,
    )

    assert result["cell_status"] == "censored"
    assert result["eligible_for_outcome"] is False
    assert result["cell_status_reason"] == "run_not_settled"


def test_unreached_fault_is_inconclusive_even_when_run_settles():
    result = classify_cell(
        manifest=_manifest(fault_target_reachability={"enabled": True, "target": "faulty"}),
        metrics=_metrics(fault_target_reached=False),
        result={"task_states": {"task": "delivered"}}, run_completed=True,
    )

    assert result["cell_status"] == "inconclusive"
    assert result["cell_status_reason"] == "fault_target_not_reached"


def test_failed_outcome_is_not_runtime_comparable():
    result = classify_cell(
        manifest=_manifest(), metrics=_metrics(
            state_counts={"needs_human": 1}, delivered=0,
            first_failure_class="timeout_stall",
        ),
        result={"task_states": {"task": "needs_human"}}, run_completed=True,
    )

    assert result["cell_status"] == "failed"
    assert result["eligible_for_outcome"] is True
    assert result["runtime_comparable"] is False


def test_summary_separates_outcome_from_successful_runtime(tmp_path):
    cells = []
    for name, status, policy in (
        ("success", "success", "orchestrator"),
        ("failed", "failed", "orchestrator"),
        ("censored", "censored", "sequential"),
    ):
        cell_dir = tmp_path / name
        cell_dir.mkdir()
        manifest = _manifest(policy=policy)
        metrics = _metrics(
            state_counts={"delivered": 1} if status == "success" else {"failed": 1},
            delivered=1 if status == "success" else 0,
            cell_status=status,
            cell_status_reason=None if status != "censored" else "run_not_settled",
            eligible_for_outcome=status in {"success", "failed"},
            runtime_comparable=status == "success",
        )
        (cell_dir / "run_manifest.json").write_text(json.dumps(manifest))
        (cell_dir / "metrics.json").write_text(json.dumps(metrics))
        cells.append(cell_dir)

    summary = summarize_cells(cells)
    assert summary["included_for_outcome"] == 2
    assert summary["included_for_runtime"] == 1
    assert summary["outcome_quality"]["by_policy"]["orchestrator"]["cells"] == 2
    assert summary["orchestration_overhead"]["successful_cells_by_policy"]["orchestrator"]["cells"] == 1
    assert "Excluded cells" in render_markdown(summary)


def test_summary_includes_fractional_verifier_quality(tmp_path):
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()
    (cell_dir / "run_manifest.json").write_text(json.dumps(_manifest()))
    (cell_dir / "metrics.json").write_text(json.dumps(_metrics(
        cell_status="success", eligible_for_outcome=True, runtime_comparable=True,
    )))
    job_root = tmp_path / "job"
    verifier = job_root / "verifier"
    verifier.mkdir(parents=True)
    (verifier / "quality_metrics.json").write_text(json.dumps({
        "quality_score": 0.5, "tasks_passed": 1, "tasks_total": 2,
    }))
    # Mirror Harbor's artifact path so the report can locate sibling verifier data.
    artifact = job_root / "artifacts" / "logs" / "artifacts"
    artifact.mkdir(parents=True)
    for name in ("run_manifest.json", "metrics.json"):
        (artifact / name).write_text((cell_dir / name).read_text())

    summary = summarize_cells([artifact])
    quality = summary["semantic_quality"]["by_policy"]["orchestrator"]
    assert quality["mean_quality_score"] == 0.5
    assert quality["tasks_passed"] == 1
    assert "Semantic quality" in render_markdown(summary)


def test_comparison_inputs_must_stay_fixed_within_track_graph_and_profile():
    first = _manifest(backend_track="cloud-claude", graph_id="wide", fault_profile="clean",
                      model={"worker": "model-a", "supervisor": "model-a"})
    second = dict(first, model={"worker": "model-b", "supervisor": "model-b"})
    violations = validate_comparison_cells([first, second])
    assert len(violations) == 1
    assert violations[0]["reason"] == "comparison_inputs_changed"
