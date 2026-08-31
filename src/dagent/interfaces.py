"""Public intermediate-artifact and dependency-interface gates."""
import hashlib
import json
import subprocess
from pathlib import Path


def _json(value):
    if value in (None, "", {}):
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("contract must contain valid JSON") from exc
    return value


def _artifact_specs(value) -> list[dict]:
    value = _json(value)
    if value is None:
        return []
    if isinstance(value, dict) and "artifacts" in value:
        value = value["artifacts"]
    if isinstance(value, str):
        value = [value]
    if isinstance(value, dict):
        value = [{"path": key, **(item if isinstance(item, dict) else {})}
                 for key, item in value.items()]
    if not isinstance(value, list):
        raise ValueError("output_artifacts must be a list or path mapping")
    specs = []
    for item in value:
        if isinstance(item, str):
            specs.append({"path": item, "required": True})
        elif isinstance(item, dict) and item.get("path"):
            specs.append({"required": True, **item})
        else:
            raise ValueError("each output artifact must declare a path")
    return specs


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def validate_output_artifacts(task: dict) -> tuple[bool, dict]:
    """Validate declared outputs against the public repository checkout."""
    try:
        specs = _artifact_specs(task.get("output_artifacts"))
    except ValueError as exc:
        return False, {"reason": "malformed_output_artifacts", "error": str(exc)}
    if not specs and not task.get("output_schema"):
        return True, {"checked": []}
    root = Path(task["repo"]).resolve()
    checked = []
    for spec in specs:
        path = (root / str(spec["path"])).resolve()
        if not _inside(path, root):
            return False, {"reason": "artifact_escapes_repository", "path": str(spec["path"])}
        exists = path.exists()
        checked.append(str(path.relative_to(root)))
        if spec.get("required", True) and not exists:
            return False, {"reason": "missing_output_artifact", "path": str(spec["path"]),
                           "checked": checked}
        if exists and spec.get("sha256"):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != spec["sha256"]:
                return False, {"reason": "artifact_digest_mismatch", "path": str(spec["path"]),
                               "expected": spec["sha256"], "actual": digest, "checked": checked}
    schema = _json(task.get("output_schema"))
    if schema and isinstance(schema, dict) and schema.get("required"):
        names = set(checked)
        missing = [name for name in schema["required"] if name not in names]
        if missing:
            return False, {"reason": "output_schema_missing", "missing": missing, "checked": checked}
    return True, {"checked": checked}


def _required_inputs(value) -> list[str]:
    value = _json(value)
    if not value:
        return []
    if isinstance(value, list):
        return [str(item.get("path") if isinstance(item, dict) else item) for item in value]
    if isinstance(value, dict):
        return [str(item) for item in (value.get("required_artifacts") or value.get("requires") or [])]
    raise ValueError("input contract must be a list or object")


def validate_dependency_interfaces(conn, task_id: str, dep_ids: list[str]) -> tuple[bool, dict]:
    """Check upstream declarations and the dependent's required inputs."""
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        return False, {"reason": "unknown_task"}
    task = dict(task)
    available = set()
    upstream = []
    for dep_id in dep_ids:
        dep = conn.execute("SELECT * FROM tasks WHERE id = ?", (dep_id,)).fetchone()
        if dep is None:
            return False, {"reason": "missing_prerequisite", "task_id": dep_id}
        ok, detail = validate_output_artifacts(dict(dep))
        upstream.append({"task_id": dep_id, "ok": ok, **detail})
        if not ok:
            return False, {"reason": "upstream_artifact_invalid", "upstream": upstream}
        try:
            available.update(spec["path"] for spec in _artifact_specs(dep["output_artifacts"]))
        except ValueError as exc:
            return False, {"reason": "malformed_output_artifacts", "task_id": dep_id,
                           "error": str(exc), "upstream": upstream}
    try:
        required = _required_inputs(task.get("input_contract"))
    except ValueError as exc:
        return False, {"reason": "malformed_input_contract", "error": str(exc),
                       "upstream": upstream}
    missing = sorted(set(required) - available)
    if missing:
        return False, {"reason": "missing_dependency_inputs", "missing": missing,
                       "available": sorted(available), "upstream": upstream}
    return True, {"upstream": upstream, "required": required, "available": sorted(available)}


def run_node_verification(task: dict, *, timeout_s: int = 60) -> tuple[bool, dict]:
    command = task.get("node_verify_cmd")
    if not command:
        return True, {"skipped": True}
    try:
        result = subprocess.run(command, cwd=task["repo"], shell=True, capture_output=True,
                                text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False, {"reason": "node_verification_timeout"}
    return result.returncode == 0, {"exit_code": result.returncode,
                                   "output_tail": (result.stdout + result.stderr)[-2000:]}
