"""Compile and validate the public workflow before workers are spawned.

This module deliberately has no scheduler or database dependency.  Its output
is a plain JSON-compatible dictionary that can be copied into a run manifest.
Conflict recommendations are observations for the orchestrator policy; this
milestone does not change scheduling behavior.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
from pathlib import PurePosixPath
from typing import Any


class WorkflowPreflightError(ValueError):
    """A task graph is invalid before execution can safely begin."""

    def __init__(self, message: str, *, node_id: str | None = None,
                 reason: str | None = None):
        self.node_id = node_id
        self.reason = reason
        self.first_invalid_node = node_id
        prefix = f"workflow preflight failed at node {node_id!r}: " if node_id else \
            "workflow preflight failed: "
        super().__init__(prefix + message)


_READ_KEYS = ("read_scopes", "read_scope", "reads", "read_files", "files_read")
_WRITE_KEYS = ("write_scopes", "write_scope", "writes", "write_files", "files_write")
_MISSING = object()


def _json(value: Any, *, node_id: str, field: str) -> Any:
    if value in (None, "", {}):
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise WorkflowPreflightError(
            f"{field} must contain valid JSON", node_id=node_id,
            reason=f"malformed_{field}",
        ) from exc


def _path(value: Any, *, node_id: str, field: str, allow_glob: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowPreflightError(
            f"{field} must contain non-empty relative paths", node_id=node_id,
            reason=f"malformed_{field}",
        )
    value = value.strip().replace("\\", "/")
    if "\x00" in value or value.startswith("/") or value.startswith("//"):
        raise WorkflowPreflightError(
            f"{field} path {value!r} must be relative to the repository",
            node_id=node_id, reason=f"unsafe_{field}",
        )
    directory = value.endswith("/")
    candidate = value.rstrip("/") or "."
    parts = PurePosixPath(candidate).parts
    if ".." in parts or candidate in ("", "."):
        raise WorkflowPreflightError(
            f"{field} path {value!r} must stay inside the repository",
            node_id=node_id, reason=f"unsafe_{field}",
        )
    if not allow_glob and any(char in candidate for char in "*?["):
        raise WorkflowPreflightError(
            f"{field} artifact path {value!r} cannot be a glob",
            node_id=node_id, reason=f"ambiguous_{field}",
        )
    normalized = posixpath.normpath(candidate)
    return normalized + "/" if directory else normalized


def _artifact_specs(value: Any, *, node_id: str) -> list[dict[str, Any]]:
    value = _json(value, node_id=node_id, field="output_artifacts")
    if value is None:
        return []
    if isinstance(value, dict) and "artifacts" in value:
        value = value["artifacts"]
    if isinstance(value, str):
        value = [value]
    if isinstance(value, dict):
        value = [
            {"path": key, **(item if isinstance(item, dict) else {})}
            for key, item in value.items()
        ]
    if not isinstance(value, list):
        raise WorkflowPreflightError(
            "output_artifacts must be a list or path mapping", node_id=node_id,
            reason="malformed_output_artifacts",
        )
    specs = []
    seen = set()
    for item in value:
        if isinstance(item, str):
            item = {"path": item, "required": True}
        elif isinstance(item, dict):
            item = {"required": True, **item}
        else:
            raise WorkflowPreflightError(
                "each output artifact must declare a path", node_id=node_id,
                reason="malformed_output_artifacts",
            )
        artifact_path = _path(item.get("path"), node_id=node_id,
                              field="output_artifacts")
        if artifact_path in seen:
            raise WorkflowPreflightError(
                f"output artifact {artifact_path!r} is declared more than once",
                node_id=node_id, reason="ambiguous_output_artifacts",
            )
        seen.add(artifact_path)
        normalized = {key: item[key] for key in sorted(item)}
        normalized["path"] = artifact_path
        specs.append(normalized)
    return specs


def _required_inputs(value: Any, *, node_id: str) -> list[str]:
    value = _json(value, node_id=node_id, field="input_contract")
    if value is None:
        return []
    if isinstance(value, dict):
        values = value.get("required_artifacts", value.get("requires", _MISSING))
        if values is _MISSING:
            values = value.get("inputs", _MISSING)
        if values is _MISSING:
            raise WorkflowPreflightError(
                "input_contract must name required_artifacts or requires",
                node_id=node_id, reason="ambiguous_input_contract",
            )
    elif isinstance(value, list):
        values = value
    else:
        raise WorkflowPreflightError(
            "input_contract must be a list or object", node_id=node_id,
            reason="malformed_input_contract",
        )
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise WorkflowPreflightError(
            "input contract requirements must be a list", node_id=node_id,
            reason="malformed_input_contract",
        )
    paths = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("path", item.get("artifact", item.get("name")))
        paths.append(_path(item, node_id=node_id, field="input_contract"))
    if len(paths) != len(set(paths)):
        raise WorkflowPreflightError(
            "input contract names an artifact more than once", node_id=node_id,
            reason="ambiguous_input_contract",
        )
    return paths


def _scopes(raw: dict[str, Any], keys: tuple[str, ...], *, node_id: str,
            access: str) -> list[str]:
    values = []
    present = [key for key in keys if key in raw and raw[key] is not None]
    file_scopes = raw.get("file_scopes")
    if file_scopes is not None:
        if not isinstance(file_scopes, dict):
            raise WorkflowPreflightError(
                "file_scopes must be an object with read and write lists",
                node_id=node_id, reason="malformed_file_scopes",
            )
        scoped = file_scopes.get(access)
        if scoped is not None:
            if present:
                raise WorkflowPreflightError(
                    f"{access} scope is declared in both file_scopes and {present[0]}",
                    node_id=node_id, reason="ambiguous_file_scopes",
                )
            values.append(scoped)
    if len(present) > 1:
        raise WorkflowPreflightError(
            f"{access} scope is declared using multiple aliases: {present}",
            node_id=node_id, reason="ambiguous_file_scopes",
        )
    values.extend(raw[key] for key in present)
    if not values:
        return []
    value = values[0]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise WorkflowPreflightError(
            f"{access} scope must be a path or list of paths", node_id=node_id,
            reason="malformed_file_scopes",
        )
    normalized = sorted({_path(item, node_id=node_id, field=f"{access}_scope",
                               allow_glob=True) for item in value})
    return normalized


def _schema(value: Any, *, node_id: str, outputs: list[dict[str, Any]]) -> Any:
    value = _json(value, node_id=node_id, field="output_schema")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise WorkflowPreflightError(
            "output_schema must be an object", node_id=node_id,
            reason="malformed_output_schema",
        )
    required = value.get("required")
    if required is not None:
        if isinstance(required, str):
            required = [required]
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise WorkflowPreflightError(
                "output_schema.required must be a list of artifact paths", node_id=node_id,
                reason="malformed_output_schema",
            )
        declared = {item["path"] for item in outputs}
        missing = sorted(set(required) - declared)
        if missing:
            raise WorkflowPreflightError(
                f"output_schema.required references undeclared artifact(s): {missing}",
                node_id=node_id, reason="missing_output_reference",
            )
    return value


def _topological_order(nodes: dict[str, dict[str, Any]]) -> list[str]:
    remaining = {node_id: set(node["depends_on"]) for node_id, node in nodes.items()}
    order = []
    ready = sorted(node_id for node_id, deps in remaining.items() if not deps)
    while ready:
        node_id = ready.pop(0)
        order.append(node_id)
        for other in sorted(remaining):
            if node_id in remaining[other]:
                remaining[other].remove(node_id)
                if not remaining[other] and other not in order and other not in ready:
                    ready.append(other)
        ready.sort()
    if len(order) != len(nodes):
        cycle = sorted(node_id for node_id, deps in remaining.items() if deps)
        first = next((node_id for node_id in nodes if node_id in cycle), cycle[0])
        raise WorkflowPreflightError(
            f"task graph contains a cycle involving {cycle}", node_id=first,
            reason="cycle",
        )
    return order


def _scope_overlaps(left: str, right: str) -> bool:
    if left == right:
        return True
    left_dir = left.endswith("/")
    right_dir = right.endswith("/")
    left_base = left.rstrip("/")
    right_base = right.rstrip("/")
    if left_dir and (right_base == left_base or right_base.startswith(left_base + "/")):
        return True
    if right_dir and (left_base == right_base or left_base.startswith(right_base + "/")):
        return True
    left_glob = any(char in left for char in "*?[")
    right_glob = any(char in right for char in "*?[")
    if left_glob and fnmatch.fnmatchcase(right_base, left):
        return True
    if right_glob and fnmatch.fnmatchcase(left_base, right):
        return True
    if left_glob or right_glob:
        left_prefix = left.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
        right_prefix = right.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
        return left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)
    return False


def _conflicts(nodes: dict[str, dict[str, Any]], order: list[str]) -> tuple[list[dict], list[dict]]:
    rank = {node_id: index for index, node_id in enumerate(order)}
    pairs = []
    for index, left_id in enumerate(order):
        for right_id in order[index + 1:]:
            shared = sorted({left for left in nodes[left_id]["write_scopes"]
                             for right in nodes[right_id]["write_scopes"]
                             if _scope_overlaps(left, right)})
            if shared:
                # Include the right-hand spelling as well when it differs; it
                # makes the recommendation useful without changing semantics.
                shared = sorted(set(shared) | {
                    right for right in nodes[right_id]["write_scopes"]
                    if any(_scope_overlaps(left, right)
                           for left in nodes[left_id]["write_scopes"])
                })
                pairs.append((left_id, right_id, shared))

    parent = {node_id: node_id for node_id in nodes}

    def find(node_id: str) -> str:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    def union(left_id: str, right_id: str) -> None:
        left_root, right_root = find(left_id), find(right_id)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_id, right_id, _shared in pairs:
        union(left_id, right_id)
    groups: dict[str, list[str]] = {}
    for node_id in nodes:
        root = find(node_id)
        groups.setdefault(root, []).append(node_id)

    conflicts = []
    recommendations = []
    group_number = 0
    for group in sorted(groups.values(), key=lambda items: min(rank[item] for item in items)):
        if len(group) < 2:
            continue
        group_number += 1
        task_ids = sorted(group, key=lambda item: (rank[item], item))
        shared = sorted({scope for left_id, right_id, scopes in pairs
                         if left_id in group and right_id in group
                         for scope in scopes})
        group_id = f"conflict-{group_number}"
        conflicts.append({
            "group_id": group_id,
            "task_ids": task_ids,
            "reason": "overlapping_write_scopes",
            "write_scopes": shared,
        })
        recommendations.append({
            "group_id": group_id,
            "action": "serialize",
            "ordered_task_ids": task_ids,
            "reason": "overlapping_write_scopes",
            "write_scopes": shared,
        })
    return conflicts, recommendations


def _verification_cost(spec: dict[str, Any]) -> int:
    # Relative units: protocol/git evidence is 1, local node verification is
    # 2, full visible verification is 3, and contract checks are 1 each.
    cost = 1
    if spec.get("output_artifacts") or spec.get("output_schema") or spec.get("input_contract"):
        cost += 1
    if spec.get("node_verify_cmd"):
        cost += 2
    if spec.get("verify_cmd"):
        cost += 3
    return cost


def compile_preflight_plan(task_specs: list[dict], *, repo_root: str | None = None) -> dict:
    """Validate and compile a JSON-serializable public workflow plan.

    The returned ``tasks`` are normalized enough for Harbor's insertion
    adapter.  No worker, scheduler, database, or repository file is touched.
    """
    if not isinstance(task_specs, list) or not task_specs:
        raise WorkflowPreflightError("task_specs must be a non-empty list", reason="empty_graph")

    nodes: dict[str, dict[str, Any]] = {}
    for raw in task_specs:
        if not isinstance(raw, dict):
            raise WorkflowPreflightError("each task spec must be an object", reason="malformed_node")
        node_id = str(raw.get("id") or "").strip()
        if not node_id:
            raise WorkflowPreflightError("task spec requires a non-empty id", reason="missing_id")
        if node_id in nodes:
            raise WorkflowPreflightError("task id is declared more than once", node_id=node_id,
                                         reason="duplicate_id")
        brief = str(raw.get("brief") or "").strip()
        if not brief:
            raise WorkflowPreflightError("task spec requires a non-empty brief", node_id=node_id,
                                         reason="missing_brief")
        depends_on = raw.get("depends_on") or []
        if not isinstance(depends_on, list) or any(not isinstance(dep, str) or not dep.strip()
                                                   for dep in depends_on):
            raise WorkflowPreflightError("depends_on must be a list of task ids", node_id=node_id,
                                         reason="malformed_dependencies")
        depends_on = [dep.strip() for dep in depends_on]
        if len(depends_on) != len(set(depends_on)):
            raise WorkflowPreflightError("depends_on contains a duplicate prerequisite", node_id=node_id,
                                         reason="ambiguous_dependencies")
        if node_id in depends_on:
            raise WorkflowPreflightError("task cannot depend on itself", node_id=node_id,
                                         reason="self_dependency")
        try:
            max_retries = int(raw.get("max_retries", 2))
        except (TypeError, ValueError) as exc:
            raise WorkflowPreflightError("max_retries must be an integer", node_id=node_id,
                                         reason="malformed_retry_policy") from exc
        if max_retries < 0:
            raise WorkflowPreflightError("max_retries cannot be negative", node_id=node_id,
                                         reason="malformed_retry_policy")
        delivery_mode = str(raw.get("delivery_mode") or "local")
        if delivery_mode not in {"pr", "local", "scout"}:
            raise WorkflowPreflightError(
                f"unsupported delivery_mode {delivery_mode!r}", node_id=node_id,
                reason="malformed_delivery_mode",
            )
        for command_field in ("verify_cmd", "node_verify_cmd"):
            command = raw.get(command_field)
            if command is not None and not isinstance(command, str):
                raise WorkflowPreflightError(
                    f"{command_field} must be a string", node_id=node_id,
                    reason=f"malformed_{command_field}",
                )
        outputs = _artifact_specs(raw.get("output_artifacts"), node_id=node_id)
        schema = _schema(raw.get("output_schema"), node_id=node_id, outputs=outputs)
        inputs = _required_inputs(
            raw.get("input_contract", raw.get("dependency_input_contract")), node_id=node_id
        )
        read_scopes = _scopes(raw, _READ_KEYS, node_id=node_id, access="read")
        write_scopes = _scopes(raw, _WRITE_KEYS, node_id=node_id, access="write")
        if set(read_scopes) & set(write_scopes):
            # A task may intentionally read and rewrite a file, but requiring
            # an explicit opt-in makes the public plan unambiguous.
            if not raw.get("allow_read_write_overlap", False):
                overlap = sorted(set(read_scopes) & set(write_scopes))
                raise WorkflowPreflightError(
                    f"read and write scopes overlap without allow_read_write_overlap: {overlap}",
                    node_id=node_id, reason="ambiguous_file_scopes",
                )
        nodes[node_id] = {
            "id": node_id,
            "title": str(raw.get("title") or node_id),
            "brief": brief,
            "depends_on": depends_on,
            "delivery_mode": delivery_mode,
            "verify_cmd": raw.get("verify_cmd"),
            "output_artifacts": outputs or None,
            "output_schema": schema,
            "input_contract": ({"required_artifacts": inputs} if inputs else None),
            "node_verify_cmd": raw.get("node_verify_cmd"),
            "repair_policy": raw.get("repair_policy"),
            "max_retries": max_retries,
            "read_scopes": read_scopes,
            "write_scopes": write_scopes,
            "_inputs": inputs,
        }

    for node_id, node in nodes.items():
        missing = sorted(set(node["depends_on"]) - nodes.keys())
        if missing:
            raise WorkflowPreflightError(
                f"depends on unknown task(s): {missing}", node_id=node_id,
                reason="missing_dependency_reference",
            )

    order = _topological_order(nodes)
    depths: dict[str, int] = {}
    parents: dict[str, str | None] = {}
    for node_id in order:
        dependencies = nodes[node_id]["depends_on"]
        if not dependencies:
            depths[node_id] = 1
            parents[node_id] = None
        else:
            parent = min(dependencies, key=lambda dep: (-depths[dep], dep))
            depths[node_id] = depths[parent] + 1
            parents[node_id] = parent
        if nodes[node_id]["_inputs"]:
            direct_outputs = {
                artifact["path"]
                for dependency in dependencies
                for artifact in nodes[dependency]["output_artifacts"] or []
            }
            missing = sorted(set(nodes[node_id]["_inputs"]) - direct_outputs)
            if missing:
                raise WorkflowPreflightError(
                    f"input contract references unavailable artifact(s): {missing}",
                    node_id=node_id, reason="missing_input_reference",
                )
            ambiguous = sorted(path for path in nodes[node_id]["_inputs"]
                               if len([dep for dep in dependencies
                                       if path in {item["path"] for item in
                                                   nodes[dep]["output_artifacts"] or []}]) > 1)
            if ambiguous:
                raise WorkflowPreflightError(
                    f"input contract has multiple direct producers for artifact(s): {ambiguous}",
                    node_id=node_id, reason="ambiguous_input_reference",
                )

    leaf = max(order, key=lambda node_id: (depths[node_id], -order.index(node_id)))
    critical_path = []
    current = leaf
    while current is not None:
        critical_path.append(current)
        current = parents[current]
    critical_path.reverse()
    conflicts, recommendations = _conflicts(nodes, order)
    task_entries = []
    total_cost = 0
    for node_id in nodes:
        node = dict(nodes[node_id])
        node.pop("_inputs")
        node["critical_path_depth"] = depths[node_id]
        node["verification_cost"] = _verification_cost(node)
        total_cost += node["verification_cost"]
        task_entries.append(node)
    critical_cost = sum(next(task["verification_cost"] for task in task_entries
                             if task["id"] == node_id) for node_id in critical_path)
    return {
        "schema_version": 1,
        "repository_root": str(repo_root) if repo_root is not None else None,
        "task_count": len(task_entries),
        "task_order": order,
        "tasks": task_entries,
        "critical_path": critical_path,
        "critical_path_depth": len(critical_path),
        "verification_cost": {
            "unit": "relative",
            "total": total_cost,
            "critical_path": critical_cost,
        },
        "conflict_groups": conflicts,
        "serialization_recommendations": recommendations,
        "validation": {"status": "passed", "first_invalid_node": None},
    }


def validate_fault_target(task_specs: list[dict], fault_injection: dict | None) -> dict | None:
    """Validate the target-reachable fault experiment contract.

    A target-reachable experiment intentionally restricts the injected node to
    a graph root. That makes launch reachability observable and prevents a
    failed ancestor from silently censoring the intended fault.
    """
    if fault_injection is None:
        return None
    if not isinstance(fault_injection, dict):
        raise WorkflowPreflightError("fault_injection must be an object", reason="malformed_fault_injection")
    if not fault_injection.get("target_reachable"):
        return None
    target = str(fault_injection.get("task_id") or "").strip()
    if not target:
        raise WorkflowPreflightError(
            "target-reachable fault injection requires task_id",
            reason="missing_fault_target",
        )
    plan = compile_preflight_plan(task_specs)
    nodes = {task["id"]: task for task in plan["tasks"]}
    if target not in nodes:
        raise WorkflowPreflightError(
            f"fault target {target!r} is not in the workflow",
            node_id=target, reason="missing_fault_target",
        )
    dependencies = nodes[target]["depends_on"]
    if dependencies:
        raise WorkflowPreflightError(
            f"target-reachable fault target must be a root; depends on {dependencies}",
            node_id=target, reason="fault_target_not_root",
        )
    return {
        "target": target,
        "mode": str(fault_injection.get("mode") or "worker_exit"),
        "root": True,
        "status": "validated",
    }


# A descriptive alias keeps call sites readable and provides a stable public
# name for callers that think in terms of workflow rather than implementation.
compile_workflow_preflight = compile_preflight_plan
