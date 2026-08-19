import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "bench/quality/build_package.py"
SOURCE_ROOT = ROOT.parent / "bench-dirs"


def _build(tmp_path, suite="latest", *tasks):
    if not SOURCE_ROOT.is_dir():
        pytest.skip("local pinned bench-dirs repositories are unavailable")
    output = tmp_path / "package"
    command = [
        str(ROOT / ".venv/bin/python"), str(BUILDER),
        "--repo-root", str(ROOT), "--source-root", str(SOURCE_ROOT),
        "--output", str(output), "--suite", suite,
        "--graph-shape", "task", "--task", tasks[0],
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return output


def test_quality_manifest_preserves_both_historical_suites():
    manifest = json.loads((ROOT / "bench/quality/task-manifest.json").read_text())
    assert len(manifest["suite_tasks"]["latest"]) == 18
    assert len(manifest["suite_tasks"]["original"]) == 19
    assert "tinydb-field-comparison" in manifest["suite_tasks"]["original"]
    assert "tinydb-remove-doc-ids-priority" in manifest["suite_tasks"]["latest"]


def test_quality_builder_separates_worker_sources_and_hidden_tests(tmp_path):
    package = _build(tmp_path, "latest", "arrow-shift-check-imaginary")
    selection = json.loads((package / "tests/selection.json").read_text())
    assert selection["hidden_commit"] == "016ae283acac1beb2281d65e3243880af10ae0e2"
    assert [task["id"] for task in selection["tasks"]] == ["arrow-shift-check-imaginary"]
    assert (package / "fixtures/arrow/arrow/arrow.py").is_file()
    assert (package / "environment/fixtures/arrow/arrow/arrow.py").is_file()
    assert (package / "environment/quality_tasks.json").is_file()
    assert (package / "tests/fixtures/arrow/arrow/arrow.py").is_file()
    assert (package / "tests/quality_tasks.json").is_file()
    assert len(list((package / "tests/hidden").rglob("test_*.py"))) == 18
    assert "COPY fixtures" in (package / "environment/Dockerfile").read_text()
    assert "COPY tests/hidden" not in (package / "environment/Dockerfile").read_text()
    assert "COPY hidden" in (package / "tests/Dockerfile").read_text()


def test_original_quality_builder_restores_original_hidden_file_count(tmp_path):
    package = _build(tmp_path, "original", "tinydb-field-comparison")
    assert len(list((package / "tests/hidden").rglob("*.py"))) == 20
    assert (package / "tests/hidden/tinydb/hidden_tests/test_task6_field_comparison.py").is_file()
