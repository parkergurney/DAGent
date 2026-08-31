"""Focused tests for the public workflow preflight compiler."""

import asyncio
import json

import pytest

from dagent import harbor
from dagent.workflow_preflight import (
    WorkflowPreflightError, compile_preflight_plan, validate_fault_target,
)
from tests.helpers import init_repo


def _node(task_id, *, depends_on=None, **fields):
    return {
        "id": task_id,
        "brief": f"implement {task_id}",
        "depends_on": depends_on or [],
        **fields,
    }


def test_malformed_artifact_contract_reports_first_invalid_node():
    with pytest.raises(WorkflowPreflightError, match="node 'bad'.*output_artifacts") as caught:
        compile_preflight_plan([
            _node("good"),
            _node("bad", output_artifacts=[{"not_path": "result.json"}]),
        ])

    assert caught.value.first_invalid_node == "bad"
    assert caught.value.reason == "malformed_output_artifacts"


def test_missing_dependency_and_input_references_are_rejected():
    with pytest.raises(WorkflowPreflightError, match="node 'child'.*unknown task"):
        compile_preflight_plan([_node("child", depends_on=["missing"])])

    with pytest.raises(WorkflowPreflightError, match="node 'child'.*unavailable artifact"):
        compile_preflight_plan([
            _node("root"),
            _node("child", depends_on=["root"],
                  input_contract={"required_artifacts": ["result.json"]}),
        ])


def test_overlapping_write_scopes_get_deterministic_conflict_group():
    plan = compile_preflight_plan([
        _node("first", write_scope=["src/shared/"]),
        _node("second", write_scope=["src/shared/config.py"]),
        _node("third", write_scope=["docs/"]),
    ])

    assert plan["conflict_groups"] == [{
        "group_id": "conflict-1",
        "task_ids": ["first", "second"],
        "reason": "overlapping_write_scopes",
        "write_scopes": ["src/shared/", "src/shared/config.py"],
    }]
    assert plan["serialization_recommendations"] == [{
        "group_id": "conflict-1",
        "action": "serialize",
        "ordered_task_ids": ["first", "second"],
        "reason": "overlapping_write_scopes",
        "write_scopes": ["src/shared/", "src/shared/config.py"],
    }]


def test_independent_scopes_have_no_conflict_recommendation():
    plan = compile_preflight_plan([
        _node("api", read_scope=["src/api/"], write_scope=["src/api/routes.py"]),
        _node("ui", read_scope=["src/ui/"], write_scope=["src/ui/app.ts"]),
    ])

    assert plan["conflict_groups"] == []
    assert plan["serialization_recommendations"] == []
    assert plan["tasks"][0]["read_scopes"] == ["src/api/"]


def test_critical_path_and_relative_verification_cost_are_estimated():
    plan = compile_preflight_plan([
        _node("root", verify_cmd="pytest tests/root.py",
              output_artifacts=["root.json"]),
        _node("branch", depends_on=["root"], node_verify_cmd="pytest tests/branch.py",
              output_artifacts=["branch.json"]),
        _node("leaf", depends_on=["branch"],
              input_contract={"required_artifacts": ["branch.json"]}),
        _node("wide", depends_on=["root"]),
    ])

    assert plan["task_order"] == ["root", "branch", "leaf", "wide"]
    assert plan["critical_path"] == ["root", "branch", "leaf"]
    assert plan["critical_path_depth"] == 3
    assert plan["verification_cost"]["critical_path"] > 0
    assert plan["verification_cost"]["total"] >= plan["verification_cost"]["critical_path"]


def test_plan_is_manifest_ready_and_contains_normalized_contracts():
    plan = compile_preflight_plan([
        _node("producer", output_artifacts={"build/result.json": {"required": True}}),
        _node("consumer", depends_on=["producer"],
              input_contract={"requires": ["build/result.json"]},
              output_artifacts=["build/summary.json"],
              output_schema={"required": ["build/summary.json"]}),
    ], repo_root="/workspace/repo")

    encoded = json.dumps(plan)
    decoded = json.loads(encoded)
    assert decoded["repository_root"] == "/workspace/repo"
    assert decoded["validation"] == {"status": "passed", "first_invalid_node": None}
    assert decoded["tasks"][0]["output_artifacts"][0]["path"] == "build/result.json"
    assert decoded["tasks"][1]["input_contract"] == {
        "required_artifacts": ["build/result.json"]
    }


def test_target_reachable_fault_requires_a_root_task():
    assert validate_fault_target(
        [_node("target")],
        {"task_id": "target", "target_reachable": True},
    )["status"] == "validated"
    with pytest.raises(WorkflowPreflightError, match="must be a root"):
        validate_fault_target(
            [_node("root"), _node("target", depends_on=["root"])],
            {"task_id": "target", "target_reachable": True},
        )


def test_harbor_runs_preflight_before_inserting_graph_tasks(tmp_path):
    repo = init_repo(tmp_path)
    db_path = tmp_path / "preflight.db"
    with pytest.raises(WorkflowPreflightError, match="node 'bad'.*unavailable artifact"):
        asyncio.run(harbor.run_instruction(
            instruction="invalid graph", repo_root=repo, db_path=db_path,
            worktree_root=tmp_path / "worktrees", fake_worker=True, fake_supervisor=True,
            task_specs=[
                _node("root"),
                _node("bad", depends_on=["root"],
                      input_contract={"required_artifacts": ["not-produced.json"]}),
            ],
        ))

    from dagent.store import connect
    conn = connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()["count"] == 0
    finally:
        conn.close()
