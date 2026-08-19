#!/bin/sh
# Run task-level semantic-quality cells. The default is deliberately small;
# expand ORCH_QUALITY_TASKS only after the representative canary is valid.
set -eu

task_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$task_dir/../../.." && pwd)
launcher="$task_dir/run_canary.sh"
suite=${ORCH_QUALITY_SUITE:-latest}
seeds=${ORCH_QUALITY_SEEDS:-${1:-0}}
model=${ORCH_QUALITY_MODEL:-claude-sonnet-4-6}
tasks=${ORCH_QUALITY_TASKS:-"arrow-shift-check-imaginary jsonschema-hostname-single-label tinydb-lru-cache-set-update"}
policies=${ORCH_QUALITY_POLICIES:-"sequential naive-parallel orchestrator"}

die() { echo "quality benchmark aborted: $*" >&2; exit 1; }
command -v harbor >/dev/null 2>&1 || die "harbor is not on PATH"
[ -x "$repo_root/.venv/bin/pytest" ] || die "missing $repo_root/.venv/bin/pytest"

cd "$repo_root"
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
echo "== validating local snapshot =="
echo "source HEAD: $(git rev-parse HEAD)"
if [ "${ALLOW_DIRTY:-0}" != 1 ] && [ -n "$(git status --short)" ]; then
  die "worktree is dirty; commit the benchmark snapshot or set ALLOW_DIRTY=1"
fi
.venv/bin/pytest -q
git diff --check

latest_job() {
  .venv/bin/python - "$repo_root/jobs" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
prefix = "orchestrator-quality-claude__"
candidates = [path for path in root.rglob("result.json") if any(part.startswith(prefix) for part in path.parts)]
if not candidates:
    raise SystemExit(1)
print(max(candidates, key=lambda path: path.stat().st_mtime).parent)
PY
}

run_cell() {
  policy=$1; seed=$2; task=$3
  echo
  echo "== quality / $task / $policy / seed $seed =="
  ORCH_QUALITY_SUITE="$suite" ORCH_QUALITY_GRAPH_SHAPE=task ORCH_QUALITY_TASK="$task" \
    ORCH_QUALITY_SOURCE_ROOT="${ORCH_QUALITY_SOURCE_ROOT:-$repo_root/../bench-dirs}" \
    ORCH_QUALITY_MODEL="$model" ORCH_MAX_CONCURRENCY="${ORCH_MAX_CONCURRENCY:-4}" \
    "$launcher" "$policy" "$seed" "$model" task
  job=$(latest_job) || die "Harbor completed but no quality artifact was found"
  echo "artifacts: $job"
  .venv/bin/python - "$job" "$task" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
task = sys.argv[2]
result = json.loads((root / "artifacts/logs/artifacts/result.json").read_text())
quality_path = root / "verifier/quality_metrics.json"
quality = json.loads(quality_path.read_text()) if quality_path.exists() else {}
print(
    f"quality valid: task={task} state={result.get('state')} "
    f"score={quality.get('quality_score', 0.0)} "
    f"passed={quality.get('tasks_passed', 0)}/{quality.get('tasks_total', 0)}"
)
PY
}

for seed in $seeds; do
  case "$seed" in ''|*[!0-9]*) die "seeds must be space-separated non-negative integers: $seeds" ;; esac
  for task in $tasks; do
    for policy in $policies; do
      run_cell "$policy" "$seed" "$task"
    done
  done
done

echo
echo "quality benchmark complete: suite=$suite tasks=$tasks seeds=$seeds model=$model"
echo "Review each job's quality_metrics.json, run_manifest.json, result.json, metrics.json, task_summary.json, and verifier/reward.txt."
