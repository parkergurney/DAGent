#!/bin/sh
set -eu

task_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$task_dir/../../.." && pwd)
policy=${1:-orchestrator}
seed=${2:-0}
model=${3:-claude-sonnet-4-6}
graph_shape=${ORCH_GRAPH_SHAPE:-${4:-dag}}
backend=${ORCH_BACKEND:-anthropic}
max_concurrency=${ORCH_MAX_CONCURRENCY:-4}
context_length=${ORCH_CONTEXT_LENGTH:-32768}
auth_env_file=${ORCH_AUTH_ENV_FILE:-}
auth_mechanism=${ORCH_AUTH_MECHANISM:-subscription-oauth}
fault_task=${ORCH_FAULT_TASK:-}
fault_mode=${ORCH_FAULT_MODE:-worker_exit}
fault_delay=${ORCH_FAULT_DELAY_S:-1.0}
fast_crash_recovery=${ORCH_FAST_CRASH_RECOVERY:-1}
target_reachable=${ORCH_TARGET_REACHABLE:-0}

case "$graph_shape" in
  dag) graph_source="$task_dir/graph.json" ;;
  serial|wide|diamond|mixed) graph_source="$repo_root/benchmarks/package/graphs/$graph_shape.json" ;;
  *) echo "unsupported graph shape: $graph_shape" >&2; exit 2 ;;
esac
[ -f "$graph_source" ] || { echo "missing graph source: $graph_source" >&2; exit 2; }

# The controller computes this over the complete package, including verifier
# sources. The digest is metadata only; verifier sources are not copied to the
# agent environment.
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
task_hash=$(python3 - "$task_dir" "$graph_source" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
graph = pathlib.Path(sys.argv[2])
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
if graph != root / "graph.json":
    digest.update(f"selected-graph/{graph.name}".encode())
    digest.update(b"\0")
    digest.update(graph.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
PY
)
graph_json=$(python3 - "$graph_shape" "$graph_source" <<'PY'
import json
import pathlib
import sys

shape, source = sys.argv[1], pathlib.Path(sys.argv[2])
tasks = json.loads(source.read_text())
if shape != "dag":
    for task in tasks:
        node_id = str(task["id"])
        output = f"outputs/{node_id}.txt"
        task["title"] = f"Create {output}"
        task["brief"] = (
            f"Create {output} containing exactly {node_id} followed by a newline. "
            f"Run test -f {output}, then git add {output} and commit the change. "
            "Do not modify other files."
        )
        task["delivery_mode"] = "local"
        task["verify_cmd"] = f"test \"$(cat {output})\" = \"{node_id}\""
        task["max_retries"] = 1
print(json.dumps(tasks, separators=(",", ":")))
PY
)
harbor_version=$(harbor --version 2>/dev/null || printf 'unknown')
config=$(python3 - "$policy" "$seed" "$task_hash" "$harbor_version" "$backend" "$model" "$max_concurrency" "$context_length" "$auth_mechanism" "$graph_shape" "$graph_json" "$fault_task" "$fault_mode" "$fault_delay" "$fast_crash_recovery" "$target_reachable" <<'PY'
import json
import sys

policy, seed, task_hash, harbor_version, backend, model, max_concurrency, context, auth_mechanism, graph_shape, graph, fault_task, fault_mode, fault_delay, fast_crash_recovery, target_reachable = sys.argv[1:]
payload = {
    "orchestrator": {
        "policy": policy,
        "seed": seed,
        "task_definition_sha256": task_hash,
        "harbor_version": harbor_version,
        "backend": backend,
        "graph_id": graph_shape,
        "graph_shape": graph_shape,
        "worker_model": model,
        "supervisor_model": model,
        "max_concurrency": int(max_concurrency),
        "context_length": int(context),
        "authentication_mechanism": auth_mechanism,
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

if [ "${ORCH_DRY_RUN:-0}" = "1" ]; then
  printf '%s\n' "$config"
  exit 0
fi

if [ -n "$auth_env_file" ]; then
  if [ ! -f "$auth_env_file" ]; then
    printf '%s\n' "OAuth env file does not exist: $auth_env_file" >&2
    exit 2
  fi
  auth_mounts=$(python3 -c 'import json, sys; print(json.dumps([{"type":"bind","source":sys.argv[1],"target":"/run/secrets/dagent-claude-auth.env","read_only":True}]))' "$auth_env_file")
  # The credential is supplied through the read-only bind mount. Do not let a
  # previously exported token leak into Harbor's own environment either.
  unset CLAUDE_CODE_OAUTH_TOKEN
  exec harbor run \
    -y \
    --mounts "$auth_mounts" \
    -p "$task_dir" \
    -a dagent.harbor_agent:HarborOrchestratorAgent \
    -m "$model" \
    --ak "config=$config"
fi

exec harbor run \
  -y \
  -p "$task_dir" \
  -a dagent.harbor_agent:HarborOrchestratorAgent \
  -m "$model" \
  --ak "config=$config"
