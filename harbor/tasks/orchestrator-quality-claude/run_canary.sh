#!/bin/sh
set -eu

task_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$task_dir/../../.." && pwd)
builder="$repo_root/bench/quality/build_package.py"
policy=${1:-orchestrator}
seed=${2:-0}
model=${3:-claude-sonnet-4-6}
graph_shape=${ORCH_QUALITY_GRAPH_SHAPE:-${4:-task}}
task_id=${ORCH_QUALITY_TASK:-${5:-arrow-shift-check-imaginary}}
suite=${ORCH_QUALITY_SUITE:-latest}
source_root=${ORCH_QUALITY_SOURCE_ROOT:-$repo_root/../bench-dirs}
backend=${ORCH_BACKEND:-anthropic}
max_concurrency=${ORCH_MAX_CONCURRENCY:-4}
context_length=${ORCH_CONTEXT_LENGTH:-32768}
auth_env_file=${ORCH_AUTH_ENV_FILE:-}
auth_mechanism=${ORCH_AUTH_MECHANISM:-subscription-oauth}

case "$graph_shape" in
  task) tasks="$task_id" ;;
  serial|wide|diamond|mixed) tasks=${ORCH_QUALITY_TASKS:-} ;;
  *) echo "unsupported quality graph shape: $graph_shape" >&2; exit 2 ;;
esac
if [ "$graph_shape" != task ] && [ -z "$tasks" ]; then
  echo "set ORCH_QUALITY_TASKS for a multi-task quality graph" >&2
  exit 2
fi

case "$tasks" in
  all)
    tasks=$("$repo_root/.venv/bin/python" - "$repo_root/bench/quality/task-manifest.json" "$suite" <<'PY'
import json
import sys
manifest = json.loads(open(sys.argv[1]).read())
print(" ".join(manifest["suite_tasks"][sys.argv[2]]))
PY
) ;;
esac

[ -x "$repo_root/.venv/bin/python" ] || { echo "missing $repo_root/.venv/bin/python" >&2; exit 2; }
[ -f "$builder" ] || { echo "missing quality package builder: $builder" >&2; exit 2; }
# Harbor imports the installed agent in its host process before launching the
# task container. Make the local src layout importable just like the working
# execution benchmark launchers do.
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/orchestrator-quality.XXXXXX")
package_dir="$work_dir/package"
cleanup() {
  if [ "${ORCH_QUALITY_KEEP_PACKAGE:-0}" != 1 ]; then
    rm -rf "$work_dir"
  else
    echo "prepared package: $package_dir" >&2
  fi
}
trap cleanup EXIT HUP INT TERM

set -- --repo-root "$repo_root" --source-root "$source_root" --output "$package_dir" \
  --suite "$suite" --graph-shape "$graph_shape"
for task in $tasks; do
  set -- "$@" --task "$task"
done
"$repo_root/.venv/bin/python" "$builder" "$@" >/dev/null

if [ "$graph_shape" = wide ] && [ "${ORCH_QUALITY_ALLOW_UNSAFE_WIDE:-0}" != 1 ]; then
  "$repo_root/.venv/bin/python" - "$package_dir/graph.json" <<'PY'
import json
import sys
graph = json.load(open(sys.argv[1]))
unsafe = [node["id"] for node in graph if not node.get("parallel_safe", False)]
if unsafe:
    raise SystemExit(
        "wide quality graph contains overlapping write scopes; review tasks or "
        "set ORCH_QUALITY_ALLOW_UNSAFE_WIDE=1: " + ", ".join(unsafe)
    )
PY
fi

graph_json=$("$repo_root/.venv/bin/python" - "$package_dir/graph.json" <<'PY'
import json, sys
print(json.dumps(json.load(open(sys.argv[1])), separators=(",", ":")))
PY
)
task_hash=$("$repo_root/.venv/bin/python" - "$package_dir/package-manifest.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["task_package_sha256"])
PY
)
harbor_version=$(harbor --version 2>/dev/null || printf 'unknown')
config=$("$repo_root/.venv/bin/python" - "$policy" "$seed" "$task_hash" "$harbor_version" "$backend" "$model" "$max_concurrency" "$context_length" "$auth_mechanism" "$suite" "$graph_shape" "$graph_json" <<'PY'
import json
import sys

policy, seed, task_hash, harbor_version, backend, model, max_concurrency, context, auth_mechanism, suite, graph_shape, graph = sys.argv[1:]
payload = {
    "orchestrator": {
        "policy": policy,
        "seed": seed,
        "task_definition_sha256": task_hash,
        "harbor_version": harbor_version,
        "backend": backend,
        "backend_track": "quality-claude",
        "graph_id": f"quality-{suite}-{graph_shape}",
        "graph_shape": graph_shape,
        "quality_suite": suite,
        "worker_model": model,
        "supervisor_model": model,
        "max_concurrency": int(max_concurrency),
        "context_length": int(context),
        "authentication_mechanism": auth_mechanism,
        "deterministic_crash_recovery": True,
        "tasks": json.loads(graph),
        "verify_cmd": "true",
        "quality_track": True,
    }
}
print(json.dumps(payload, separators=(",", ":")))
PY
)

if [ "${ORCH_DRY_RUN:-0}" = 1 ]; then
  printf '%s\n' "$config"
  exit 0
fi

if [ -n "$auth_env_file" ]; then
  [ -f "$auth_env_file" ] || { echo "OAuth env file does not exist: $auth_env_file" >&2; exit 2; }
  auth_mounts=$("$repo_root/.venv/bin/python" -c 'import json, sys; print(json.dumps([{"type":"bind","source":sys.argv[1],"target":"/run/secrets/dagent-claude-auth.env","read_only":True}]))' "$auth_env_file")
  unset CLAUDE_CODE_OAUTH_TOKEN
  exec harbor run -y --mounts "$auth_mounts" -p "$package_dir" \
    -a dagent.harbor_agent:HarborOrchestratorAgent -m "$model" --ak "config=$config"
fi

exec harbor run -y -p "$package_dir" \
  -a dagent.harbor_agent:HarborOrchestratorAgent -m "$model" --ak "config=$config"
