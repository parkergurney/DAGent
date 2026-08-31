"""Experiment-cell validity and comparison metadata.

This module is deliberately independent of the scheduler.  A cell is an
observation of a completed (or interrupted) run, and its classification must
not change the state machine or turn an invalid benchmark into a failure by
editorial judgment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from statistics import mean


CELL_STATUSES = frozenset({"success", "failed", "censored", "inconclusive"})
_SETTLED_STATES = frozenset({"delivered", "failed", "cancelled", "dependency_blocked", "needs_human"})
_REQUIRED_MANIFEST_FIELDS = (
    "policy", "seed", "task_graph", "repository", "backend", "context_length",
    "limits", "authentication",
)
_PHASE5_POLICIES = ("sequential", "naive-parallel", "orchestrator")
_PHASE5_PROFILES = (
    "clean", "worker_crash", "worker_timeout", "no_candidate",
    "verification_failure", "dependency_failure", "worker_latency",
)
_PHASE5_TRACKS = ("cloud-claude", "local-ollama")


def _state_counts(result: dict, metrics: dict) -> dict[str, int]:
    states = result.get("task_states")
    if isinstance(states, dict) and states:
        counts: dict[str, int] = {}
        for state in states.values():
            counts[str(state)] = counts.get(str(state), 0) + 1
        return counts
    value = metrics.get("state_counts") or {}
    return {str(key): int(count) for key, count in value.items()}


def _manifest_error(manifest: dict) -> str | None:
    missing = [field for field in _REQUIRED_MANIFEST_FIELDS if field not in manifest]
    if missing:
        return "manifest_missing:" + ",".join(missing)
    repository = manifest.get("repository")
    if not isinstance(repository, dict) or not repository.get("base_sha"):
        return "manifest_missing:repository.base_sha"
    limits = manifest.get("limits")
    if not isinstance(limits, dict) or limits.get("max_concurrency") is None:
        return "manifest_missing:limits.max_concurrency"
    return None


def classify_cell(*, manifest: dict, metrics: dict, result: dict | None = None,
                  run_completed: bool = True) -> dict:
    """Classify one experiment cell and return report-ready metadata.

    ``failed`` is an outcome observation, but never a runtime comparison.  A
    run that did not settle is ``censored``; a target-reachable fault that did
    not launch is ``inconclusive``.  Those rules are encoded here so report
    generation cannot accidentally compare short failed runs with successes.
    """
    result = result or {}
    states = _state_counts(result, metrics)
    task_count = sum(states.values()) or int(metrics.get("tasks", 0))
    delivered = states.get("delivered", int(metrics.get("delivered", 0)))
    fault_reachability = manifest.get("fault_target_reachability") or {}
    target_required = bool(fault_reachability.get("enabled"))
    target_reached = bool(metrics.get("fault_target_reached"))
    status: str
    reason: str | None = None

    if (reason := _manifest_error(manifest)) is not None:
        status = "inconclusive"
    elif target_required and not target_reached:
        status = "inconclusive"
        reason = "fault_target_not_reached"
    else:
        settled = task_count > 0 and sum(
            count for state, count in states.items() if state in _SETTLED_STATES
        ) == task_count
        if task_count > 0 and delivered == task_count:
            status = "success"
        elif not run_completed or not settled:
            status = "censored"
            reason = "run_not_settled"
        else:
            status = "failed"
            reason = "terminal_task_failure"

    if status not in CELL_STATUSES:  # defensive: keep the serialized contract closed
        raise ValueError(f"unknown cell status {status!r}")
    completion_rate = round(delivered / task_count, 6) if task_count else 0.0
    return {
        "cell_status": status,
        "cell_status_reason": reason,
        "eligible_for_outcome": status in {"success", "failed"},
        "runtime_comparable": status == "success",
        "task_count": task_count,
        "verified_completion_rate": completion_rate,
        "state_counts": states,
        "failure_class": metrics.get("first_failure_class"),
        "fault_profile": manifest.get("fault_profile") or manifest.get("fault_injection"),
        "fault_target": metrics.get("fault_target") or fault_reachability.get("target"),
        "fault_target_reached": target_reached,
    }


def _cell_paths(path: str | Path) -> tuple[Path, Path, Path]:
    root = Path(path)
    if root.is_file():
        if root.name == "metrics.json":
            return root.with_name("run_manifest.json"), root, root.with_name("result.json")
        return root.with_name("run_manifest.json"), root, root.with_name("result.json")
    nested = root / "artifacts" / "logs" / "artifacts"
    if (nested / "run_manifest.json").is_file():
        root = nested
    return root / "run_manifest.json", root / "metrics.json", root / "result.json"


def _quality_metrics_path(cell_root: Path) -> Path | None:
    """Find verifier quality evidence next to a Harbor artifact directory."""
    candidates = [cell_root / "quality_metrics.json"]
    for parent in [cell_root, *cell_root.parents]:
        if (parent / "verifier").is_dir():
            candidates.append(parent / "verifier" / "quality_metrics.json")
            break
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def load_cell(path: str | Path) -> dict:
    """Load a cell artifact directory or its ``metrics.json`` path."""
    manifest_path, metrics_path, result_path = _cell_paths(path)
    manifest = json.loads(manifest_path.read_text())
    metrics = json.loads(metrics_path.read_text())
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    cell = dict(manifest)
    cell.update(metrics)
    cell.update({key: value for key, value in result.items() if key != "metrics"})
    quality_path = _quality_metrics_path(metrics_path.parent)
    if quality_path is not None:
        quality = json.loads(quality_path.read_text())
        if isinstance(quality, dict):
            cell["quality"] = quality
            cell["quality_score"] = float(quality.get("quality_score", 0.0))
            cell["quality_tasks_passed"] = int(quality.get("tasks_passed", 0))
            cell["quality_tasks_total"] = int(quality.get("tasks_total", 0))
    cell.setdefault("cell_status", metrics.get("cell_status"))
    return cell


def summarize_cells(paths: list[str | Path]) -> dict:
    """Build a reproducible, validity-aware summary from saved cell artifacts."""
    cells = [load_cell(path) for path in paths]
    comparison_violations = validate_comparison_cells(cells)
    outcome_cells = [cell for cell in cells if cell.get("eligible_for_outcome")]
    runtime_cells = [cell for cell in cells if cell.get("runtime_comparable")]

    by_policy: dict[str, list[dict]] = {}
    for cell in outcome_cells:
        by_policy.setdefault(str(cell.get("policy", "unknown")), []).append(cell)
    policy_summary = {}
    for policy, rows in sorted(by_policy.items()):
        policy_summary[policy] = {
            "cells": len(rows),
            "successes": sum(row.get("cell_status") == "success" for row in rows),
            "verified_completion_probability": round(
                sum(row.get("cell_status") == "success" for row in rows) / len(rows), 6
            ),
            "mean_verified_completion_rate": round(
                mean(row.get("verified_completion_rate", 0.0) for row in rows), 6
            ),
        }

    overhead = {}
    for policy in sorted({str(cell.get("policy", "unknown")) for cell in runtime_cells}):
        rows = [cell for cell in runtime_cells if str(cell.get("policy", "unknown")) == policy]
        overhead[policy] = {
            "cells": len(rows),
            "mean_wall_time_s": round(mean(row.get("wall_time_s", 0.0) for row in rows), 6),
            "mean_cost_usd": round(mean(row.get("cost_usd", 0.0) for row in rows), 6),
            "mean_queue_wait_s": round(mean(row.get("queue_wait_s", 0.0) for row in rows), 6),
            "mean_worker_execution_s": round(
                mean(row.get("worker_execution_s", 0.0) for row in rows), 6
            ),
            "mean_verification_s": round(mean(row.get("verification_s", 0.0) for row in rows), 6),
            "mean_supervisor_s": round(mean(row.get("supervisor_s", 0.0) for row in rows), 6),
        }

    quality_cells = [cell for cell in cells if "quality_score" in cell]
    quality_by_policy: dict[str, list[dict]] = {}
    for cell in quality_cells:
        quality_by_policy.setdefault(str(cell.get("policy", "unknown")), []).append(cell)
    quality_summary = {}
    for policy, rows in sorted(quality_by_policy.items()):
        quality_summary[policy] = {
            "cells": len(rows),
            "complete_scores": sum(row.get("quality_score", 0.0) >= 1.0 for row in rows),
            "mean_quality_score": round(mean(row.get("quality_score", 0.0) for row in rows), 6),
            "tasks_passed": sum(row.get("quality_tasks_passed", 0) for row in rows),
            "tasks_total": sum(row.get("quality_tasks_total", 0) for row in rows),
        }

    return {
        "schema_version": 1,
        "cells": len(cells),
        "included_for_outcome": len(outcome_cells),
        "included_for_runtime": len(runtime_cells),
        "excluded_cells": [
            {
                "policy": cell.get("policy"),
                "seed": cell.get("seed"),
                "status": cell.get("cell_status"),
                "reason": cell.get("cell_status_reason"),
            }
            for cell in cells
            if not cell.get("eligible_for_outcome")
        ],
        "outcome_quality": {"by_policy": policy_summary},
        "semantic_quality": {"by_policy": quality_summary},
        "orchestration_overhead": {"successful_cells_by_policy": overhead},
        "comparison_violations": comparison_violations,
        "cells_detail": cells,
    }


def _comparison_signature(cell: dict) -> tuple:
    """Return fields that must remain fixed within a comparison group."""
    model = cell.get("model") or {}
    limits = cell.get("limits") or {}
    verifier = cell.get("verifier") or {}
    authentication = cell.get("authentication") or {}
    return (
        cell.get("backend"),
        model.get("worker"), model.get("supervisor"),
        cell.get("context_length"),
        cell.get("task_package_sha256") or cell.get("task_definition_sha256"),
        verifier.get("mode"), verifier.get("visible_command"), verifier.get("identity"),
        json.dumps(limits, sort_keys=True),
        authentication.get("mechanism"),
        json.dumps(cell.get("resource_config") or {}, sort_keys=True),
    )


def validate_comparison_cells(cells: list[dict]) -> list[dict]:
    """Find mixed inputs before a report turns them into a comparison.

    Graph, fault profile, and backend track are explicit factors.  Within one
    such group, model/context/task package/verifier/limits/auth/resource
    configuration must be identical across policies and seeds.
    """
    groups: dict[tuple, list[dict]] = {}
    for cell in cells:
        key = (
            cell.get("backend_track", "unspecified"),
            cell.get("graph_id", "unspecified"),
            cell.get("fault_profile")
            or (cell.get("fault_injection") or {}).get("mode", "clean"),
        )
        groups.setdefault(key, []).append(cell)
    violations = []
    for key, rows in sorted(groups.items(), key=lambda item: repr(item[0])):
        signatures = {_comparison_signature(row) for row in rows}
        if len(signatures) > 1:
            violations.append({
                "backend_track": key[0], "graph_id": key[1],
                "fault_profile": key[2],
                "cells": [
                    {"policy": row.get("policy"), "seed": row.get("seed"),
                     "task_package_sha256": row.get("task_package_sha256")}
                    for row in rows
                ],
                "reason": "comparison_inputs_changed",
            })
    return violations


def render_markdown(summary: dict) -> str:
    """Render the two Phase 3 report views without mixing their populations."""
    lines = [
        "# Experiment summary", "",
        f"Cells: {summary['cells']}  ",
        f"Outcome-eligible: {summary['included_for_outcome']}  ",
        f"Runtime-comparable: {summary['included_for_runtime']}", "",
        "## Outcome quality", "",
        "| Policy | Cells | Successes | Verified completion probability | Mean completion rate |",
        "|---|---:|---:|---:|---:|",
    ]
    for policy, row in summary["outcome_quality"]["by_policy"].items():
        lines.append(
            f"| {policy} | {row['cells']} | {row['successes']} | "
            f"{row['verified_completion_probability']:.6f} | "
            f"{row['mean_verified_completion_rate']:.6f} |"
        )
    quality_rows = summary.get("semantic_quality", {}).get("by_policy", {})
    if quality_rows:
        lines += [
            "", "## Semantic quality (verifier hidden tests)", "",
            "| Policy | Cells | Complete scores | Mean quality score | Hidden tests passed |",
            "|---|---:|---:|---:|---:|",
        ]
        for policy, row in quality_rows.items():
            lines.append(
                f"| {policy} | {row['cells']} | {row['complete_scores']} | "
                f"{row['mean_quality_score']:.6f} | "
                f"{row['tasks_passed']}/{row['tasks_total']} |"
            )
    lines += [
        "", "## Orchestration overhead (successful cells only)", "",
        "| Policy | Cells | Mean wall s | Mean cost USD | Mean queue s | Mean worker s | Mean verify s | Mean supervisor s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for policy, row in summary["orchestration_overhead"]["successful_cells_by_policy"].items():
        lines.append(
            f"| {policy} | {row['cells']} | {row['mean_wall_time_s']:.3f} | "
            f"{row['mean_cost_usd']:.6f} | {row['mean_queue_wait_s']:.3f} | "
            f"{row['mean_worker_execution_s']:.3f} | {row['mean_verification_s']:.3f} | "
            f"{row['mean_supervisor_s']:.3f} |"
        )
    lines += ["", "## Excluded cells", ""]
    if summary["excluded_cells"]:
        for row in summary["excluded_cells"]:
            lines.append(
                f"- `{row.get('policy')}` seed `{row.get('seed')}`: "
                f"{row.get('status')} ({row.get('reason')})"
            )
    else:
        lines.append("None.")
    lines += ["", "## Comparison input violations", ""]
    if summary.get("comparison_violations"):
        for violation in summary["comparison_violations"]:
            lines.append(
                f"- `{violation['backend_track']}` / `{violation['graph_id']}`: "
                f"{violation['reason']}"
            )
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def _read_json(path: Path) -> dict | list:
    value = json.loads(path.read_text())
    if not isinstance(value, (dict, list)):
        raise ValueError(f"{path} must contain a JSON object or array")
    return value


def phase5_package_dir(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else Path(__file__).resolve().parents[2] / "benchmarks" / "phase5"


def _phase5_files(package_dir: Path) -> dict:
    package = _read_json(package_dir / "task-package.json")
    if not isinstance(package, dict):
        raise ValueError("task-package.json must contain an object")
    graphs = {}
    for path in sorted((package_dir / "graphs").glob("*.json")):
        value = _read_json(path)
        if not isinstance(value, list):
            raise ValueError(f"graph {path} must contain a task array")
        graphs[path.stem] = value
    profiles = _read_json(package_dir / "profiles.json")
    tracks = _read_json(package_dir / "tracks.json")
    if not isinstance(profiles, dict) or not isinstance(tracks, dict):
        raise ValueError("profiles.json and tracks.json must contain objects")
    return {"package": package, "graphs": graphs, "profiles": profiles, "tracks": tracks}


def validate_phase5_package(package_dir: str | Path | None = None) -> dict:
    """Validate the committed Phase 5 inputs without touching a repository."""
    root = phase5_package_dir(package_dir)
    data = _phase5_files(root)
    expected_graphs = {"serial", "wide", "diamond", "mixed"}
    if set(data["graphs"]) != expected_graphs:
        raise ValueError(f"Phase 5 graphs must be {sorted(expected_graphs)}")
    for name, graph in data["graphs"].items():
        if len(graph) != 10:
            raise ValueError(f"Phase 5 graph {name!r} must contain ten tasks")
    if set(data["profiles"]) != set(_PHASE5_PROFILES):
        raise ValueError("Phase 5 profile set is incomplete")
    if set(data["tracks"]) != set(_PHASE5_TRACKS):
        raise ValueError("Phase 5 backend tracks are incomplete")
    for track, value in data["tracks"].items():
        if value.get("comparison_group") != track:
            raise ValueError(f"backend track {track!r} needs its own comparison_group")
    return data


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _root_task_id(tasks: list[dict]) -> str:
    roots = [str(task["id"]) for task in tasks if not task.get("depends_on")]
    if not roots:
        raise ValueError("benchmark graph has no root task")
    return sorted(roots)[0]


def build_phase5_cell(*, package_dir: str | Path | None = None, graph: str,
                      policy: str, seed: int, profile: str = "clean",
                      backend_track: str = "cloud-claude",
                      repo_root: str | Path, artifact_root: str | Path,
                      fake_worker: bool = True) -> dict:
    """Build one reproducible Harbor-runtime config from committed inputs."""
    data = validate_phase5_package(package_dir)
    if policy not in _PHASE5_POLICIES:
        raise ValueError(f"unsupported policy {policy!r}")
    if graph not in data["graphs"]:
        raise ValueError(f"unknown graph {graph!r}")
    if profile not in data["profiles"]:
        raise ValueError(f"unknown profile {profile!r}")
    if backend_track not in data["tracks"]:
        raise ValueError(f"unknown backend track {backend_track!r}")
    track = dict(data["tracks"][backend_track])
    profile_data = dict(data["profiles"][profile])
    tasks = [dict(task) for task in data["graphs"][graph]]
    target = str(profile_data.get("target") or _root_task_id(tasks))
    task_ids = {str(task.get("id")) for task in tasks}
    if target not in task_ids:
        raise ValueError(f"profile {profile!r} target {target!r} is not in graph {graph!r}")
    if fake_worker:
        # FakeWorker treats the brief as a scenario name. Keep every
        # non-target node on the successful path so a profile isolates one
        # controlled cause instead of creating accidental parser failures.
        for task in tasks:
            task["brief"] = "clean"
    if profile != "clean":
        # FakeWorker scenario names are intentionally confined to this
        # benchmark adapter; real backend cells retain the task brief.
        if fake_worker:
            for task in tasks:
                if task["id"] == target:
                    task["brief"] = str(profile_data["fake_scenario"])
                    if profile_data.get("verify_cmd") is not None:
                        task["verify_cmd"] = profile_data["verify_cmd"]
        else:
            raise ValueError("fault profiles require --fake-worker in the local cell runner")

    fault = None
    if profile != "clean":
        fault = {
            "task_id": target,
            "mode": profile_data["mode"],
            "attempt": 1,
            "delay_s": profile_data.get("delay_s", 0.1),
            "target_reachable": True,
            "seed": seed,
        }
        if profile == "worker_latency":
            delays = profile_data.get("delays_s", [0.05, 0.1, 0.2])
            fault["delay_s"] = delays[int(seed) % len(delays)]

    package_hash = _canonical_hash({
        "package": data["package"], "graph": tasks, "profile": profile_data,
    })
    config = {
        "repo_root": str(Path(repo_root).resolve()),
        "artifact_root": str(Path(artifact_root).resolve()),
        "run_artifact_root": str(Path(artifact_root).resolve() / ".run"),
        "db_path": str(Path(artifact_root).resolve() / ".state.db"),
        "worktree_root": str(Path(artifact_root).resolve() / ".worktrees"),
        "policy": policy, "seed": seed, "graph_id": graph,
        "graph_shape": graph, "backend_track": backend_track,
        "fault_profile": profile,
        "backend": track["backend"], "worker_model": track["worker_model"],
        "supervisor_model": track["supervisor_model"],
        "context_length": track["context_length"],
        "resource_config": track["resource_config"],
        "authentication_mechanism": track["authentication_mechanism"],
        "verifier_identity": data["package"]["verifier_identity"],
        "verify_cmd": data["package"]["verify_cmd"],
        "task_definition_sha256": package_hash,
        "tasks": tasks, "fault_injection": fault,
        "fake_worker": fake_worker, "fake_supervisor": fake_worker,
        # This flag is an explicit caller declaration, matching Harbor's
        # existing boundary contract.  The CLI only sets it behind --live.
        "external_isolation": not fake_worker,
        "max_concurrency": track["max_concurrency"],
        "max_retries": 0 if profile in {"worker_timeout", "no_candidate", "verification_failure", "dependency_failure"} else 2,
        "stall_threshold_s": 1 if profile == "worker_timeout" else track["stall_threshold_s"],
        "worker_timeout_s": 1 if profile == "worker_timeout" else track["worker_timeout_s"],
        "sdk_timeout_s": track["sdk_timeout_s"],
        "supervisor_timeout_s": track["supervisor_timeout_s"],
        "wait_ceiling_s": 1 if profile == "worker_timeout" else track["wait_ceiling_s"],
    }
    return config


async def run_phase5_cell(*, package_dir: str | Path | None = None, graph: str,
                          policy: str, seed: int, profile: str = "clean",
                          backend_track: str = "cloud-claude",
                          repo_root: str | Path, artifact_root: str | Path,
                          fake_worker: bool = True) -> dict:
    """Run one cell and return its saved report metadata."""
    from dagent.harbor_runtime import run_from_files

    output = Path(artifact_root)
    output.mkdir(parents=True, exist_ok=True)
    config = build_phase5_cell(
        package_dir=package_dir, graph=graph, policy=policy, seed=seed,
        profile=profile, backend_track=backend_track, repo_root=repo_root,
        artifact_root=output, fake_worker=fake_worker,
    )
    instruction = output / ".instruction.md"
    instruction.write_text("Execute the fixed Phase 5 benchmark task graph.\n")
    config_path = output / ".config.json"
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    await run_from_files(instruction, config_path)
    return load_cell(output)


def _phase5_main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="dagent-experiment")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="validate inputs and write the Phase 5 matrix")
    prepare.add_argument("--package", dest="package_dir")
    prepare.add_argument("--output-dir", required=True)
    prepare.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2])
    prepare.add_argument("--profiles", nargs="+", choices=_PHASE5_PROFILES,
                         default=list(_PHASE5_PROFILES))
    prepare.add_argument("--tracks", nargs="+", choices=_PHASE5_TRACKS,
                         default=list(_PHASE5_TRACKS))

    run = subparsers.add_parser("run", help="run one Phase 5 cell")
    run.add_argument("--package", dest="package_dir")
    run.add_argument("--graph", required=True, choices=("serial", "wide", "diamond", "mixed"))
    run.add_argument("--policy", required=True, choices=_PHASE5_POLICIES)
    run.add_argument("--seed", required=True, type=int)
    run.add_argument("--profile", choices=_PHASE5_PROFILES, default="clean")
    run.add_argument("--backend-track", choices=_PHASE5_TRACKS, default="cloud-claude")
    run.add_argument("--repo-root", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument("--live", action="store_true",
                     help="allow a real SDK backend; requires a trusted outer boundary")

    report = subparsers.add_parser("report", help="summarize saved cell artifacts")
    report.add_argument("cells", nargs="+")
    report.add_argument("--output-dir", required=True)

    args = parser.parse_args(argv)
    if args.command == "prepare":
        data = validate_phase5_package(args.package_dir)
        cells = []
        for track in args.tracks:
            for graph in sorted(data["graphs"]):
                for profile in args.profiles:
                    for policy in _PHASE5_POLICIES:
                        for seed in args.seeds:
                            cells.append({
                                "backend_track": track, "graph": graph,
                                "profile": profile, "policy": policy, "seed": seed,
                            })
        output = Path(args.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1, "package": str(phase5_package_dir(args.package_dir)),
            "graphs": sorted(data["graphs"]), "profiles": args.profiles,
            "tracks": args.tracks, "policies": list(_PHASE5_POLICIES),
            "seeds": args.seeds, "cells": cells,
        }
        (output / "matrix.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(output / "matrix.json")
        return 0
    if args.command == "run":
        if not args.live:
            fake_worker = True
        else:
            fake_worker = False
        result = asyncio.run(run_phase5_cell(
            package_dir=args.package_dir, graph=args.graph, policy=args.policy,
            seed=args.seed, profile=args.profile, backend_track=args.backend_track,
            repo_root=args.repo_root, artifact_root=args.output_dir,
            fake_worker=fake_worker,
        ))
        print(json.dumps({
            "cell_status": result.get("cell_status"),
            "policy": result.get("policy"), "seed": result.get("seed"),
            "output_dir": str(Path(args.output_dir).resolve()),
        }, sort_keys=True))
        return 0
    summary = summarize_cells(args.cells)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (output / "report.md").write_text(render_markdown(summary))
    print(output / "report.json")
    print(output / "report.md")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    if argv is None:
        import sys
        argv = sys.argv[1:]
    if argv and argv[0] in {"prepare", "run", "report"}:
        return _phase5_main(argv)

    parser = argparse.ArgumentParser(prog="dagent-report")
    parser.add_argument("cells", nargs="+", help="cell artifact directories or metrics.json files")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    summary = summarize_cells(args.cells)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (output / "report.md").write_text(render_markdown(summary))
    print(output / "report.json")
    print(output / "report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
