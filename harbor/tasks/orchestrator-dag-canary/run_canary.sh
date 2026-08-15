#!/bin/sh
set -eu

task_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$task_dir/../../.." && pwd)
policy=${1:-orchestrator}
seed=${2:-0}
model=${3:-qwen3-coder:30b}
backend=${ORCH_BACKEND:-ollama}
context_length=${ORCH_CONTEXT_LENGTH:-32768}
fault_task=${ORCH_FAULT_TASK:-}
fault_mode=${ORCH_FAULT_MODE:-worker_exit}
fault_delay=${ORCH_FAULT_DELAY_S:-1.0}
fast_crash_recovery=${ORCH_FAST_CRASH_RECOVERY:-1}
target_reachable=${ORCH_TARGET_REACHABLE:-0}

# The controller computes this over the complete package, including verifier
# sources. The digest is metadata only; verifier sources are not copied to the
# agent environment.
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
task_hash=$(python3 - "$task_dir" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
files = sorted(
    path for path in root.rglob("*")
    if path.is_file() and path.name not in {"README.md", "run_canary.sh"}
)
digest = hashlib.sha256()
for path in files:
    digest.update(path.relative_to(root).as_posix().encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
PY
)
graph_json=$(cat "$task_dir/graph.json")
harbor_version=$(harbor --version 2>/dev/null || printf 'unknown')
config=$(python3 - "$policy" "$seed" "$task_hash" "$harbor_version" "$backend" "$model" "$context_length" "$graph_json" "$fault_task" "$fault_mode" "$fault_delay" "$fast_crash_recovery" "$target_reachable" <<'PY'
import json
import sys

policy, seed, task_hash, harbor_version, backend, model, context, graph, fault_task, fault_mode, fault_delay, fast_crash_recovery, target_reachable = sys.argv[1:]
payload = {
    "orchestrator": {
        "policy": policy,
        "seed": seed,
        "task_definition_sha256": task_hash,
        "harbor_version": harbor_version,
        "backend": backend,
        "worker_model": model,
        "supervisor_model": model,
        "max_concurrency": 2,
        "context_length": int(context),
        "deterministic_crash_recovery": fast_crash_recovery.strip().lower() in {"1", "true", "yes", "on"},
        "tasks": json.loads(graph),
    }
}
if fault_task:
    payload["orchestrator"]["fault_injection"] = {
        "task_id": fault_task,
        "mode": fault_mode,
        "delay_s": float(fault_delay),
        "target_reachable": target_reachable.strip().lower() in {"1", "true", "yes", "on"},
    }
print(json.dumps(payload, separators=(",", ":")))
PY
)

exec harbor run \
  -y \
  -p "$task_dir" \
  -a orchestrator.harbor_agent:HarborOrchestratorAgent \
  -m "$model" \
  --ak "config=$config" \
  --allow-environment-host host.docker.internal
