import asyncio
import json

import pytest

from dagent.experiment import (
    build_benchmark_cell,
    run_benchmark_cell,
    validate_benchmark_package,
)
from tests.helpers import init_repo


def test_benchmark_package_has_fixed_graphs_profiles_and_tracks():
    data = validate_benchmark_package()
    assert set(data["graphs"]) == {"serial", "wide", "diamond", "mixed"}
    assert {len(graph) for graph in data["graphs"].values()} == {10}
    assert {len(task.get("depends_on", [])) for task in data["graphs"]["wide"]} == {0}
    assert data["profiles"]["worker_timeout"]["mode"] == "timeout"
    assert set(data["tracks"]) == {"cloud-claude", "local-ollama"}


def test_benchmark_cell_keeps_backend_tracks_and_reaches_fault_root(tmp_path):
    config = build_benchmark_cell(
        graph="diamond", policy="orchestrator", seed=1,
        profile="worker_latency", backend_track="local-ollama",
        repo_root=tmp_path, artifact_root=tmp_path / "cell",
    )
    assert config["backend_track"] == "local-ollama"
    assert config["fault_injection"]["task_id"] == "diamond-00"
    assert config["fault_injection"]["delay_s"] == 0.1
    assert config["task_definition_sha256"]


def test_benchmark_fake_cell_publishes_complete_reporting_artifacts(tmp_path):
    repo = init_repo(tmp_path)
    artifact_root = tmp_path / "cell"
    result = asyncio.run(run_benchmark_cell(
        graph="wide", policy="sequential", seed=0, profile="clean",
        backend_track="cloud-claude", repo_root=repo, artifact_root=artifact_root,
    ))

    assert result["cell_status"] == "success"
    assert result["verified_completion_rate"] == 1.0
    for name in ("run_manifest.json", "metrics.json", "result.json", "task_summary.json", "candidate.patch"):
        assert (artifact_root / name).exists(), name
    manifest = json.loads((artifact_root / "run_manifest.json").read_text())
    assert manifest["backend_track"] == "cloud-claude"
    assert manifest["graph_shape"] == "wide"
    assert manifest["task_graph"]["count"] == 10


def test_benchmark_fault_cell_records_target_reached(tmp_path):
    repo = init_repo(tmp_path)
    artifact_root = tmp_path / "fault-cell"
    result = asyncio.run(run_benchmark_cell(
        graph="serial", policy="sequential", seed=2, profile="worker_latency",
        backend_track="cloud-claude", repo_root=repo, artifact_root=artifact_root,
    ))

    assert result["fault_target_reached"] is True
    assert result["fault_target"] == "serial-00"
    assert result["fault_profile"] == "worker_latency"


@pytest.mark.parametrize("profile", [
    "worker_crash", "worker_timeout", "no_candidate", "verification_failure",
    "dependency_failure",
])
def test_benchmark_fault_profiles_are_target_reachable(profile, tmp_path):
    root = tmp_path / profile
    root.mkdir()
    repo = init_repo(root)
    result = asyncio.run(run_benchmark_cell(
        graph="serial", policy="sequential", seed=0, profile=profile,
        backend_track="cloud-claude", repo_root=repo, artifact_root=root / "cell",
    ))

    assert result["fault_target_reached"] is True
    assert result["fault_target"] == "serial-00"
    assert result["cell_status"] in {"failed", "success"}
