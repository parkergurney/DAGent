#!/bin/sh
# Run the dependency-aware clean and target-reachable fault benchmark.
#
# Usage:
#   ./run_benchmark.sh [seed]
#   ORCH_BENCHMARK_SEEDS="0 1 2" ./run_benchmark.sh
#   ORCH_BENCHMARK_GRAPHS="serial wide diamond mixed" ./run_benchmark.sh
#
# The runner is deliberately fail-fast. A clean cell must deliver successfully;
# a fault cell may legitimately fail for a baseline policy, but it must prove
# that the injected target was reached. Cells without that evidence are
# invalid and stop the matrix.
set -eu

task_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$task_dir/../../.." && pwd)
launcher="$task_dir/run_canary.sh"
seed_arg=${1:-}
seeds=${ORCH_BENCHMARK_SEEDS:-${seed_arg:-0}}
model=${ORCH_BENCHMARK_MODEL:-qwen3-coder:30b}
backend=${ORCH_BACKEND:-ollama}
graphs=${ORCH_BENCHMARK_GRAPHS:-"serial wide diamond"}
fault_task_override=${ORCH_BENCHMARK_FAULT_TASK:-}

die() {
  echo "benchmark aborted: $*" >&2
  exit 1
}

command -v harbor >/dev/null 2>&1 || die "harbor is not on PATH"
[ -x "$repo_root/.venv/bin/pytest" ] || die "missing $repo_root/.venv/bin/pytest"
[ -x "$repo_root/.venv/bin/python" ] || die "missing $repo_root/.venv/bin/python"

cd "$repo_root"
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"

echo "== validating local snapshot =="
echo "source HEAD: $(git rev-parse HEAD)"
if [ "${ALLOW_DIRTY:-0}" != "1" ] && [ -n "$(git status --short)" ]; then
  die "worktree is dirty; commit the benchmark snapshot or set ALLOW_DIRTY=1"
fi
.venv/bin/pytest -q
git diff --check

if [ "$backend" = "ollama" ]; then
  command -v ollama >/dev/null 2>&1 || die "ollama is not on PATH"
  ollama show "$model" >/dev/null 2>&1 || die "ollama model is unavailable: $model"
fi

latest_job() {
  .venv/bin/python - "$repo_root/jobs" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
candidates = [
    path for path in root.rglob("result.json")
    if "orchestrator-dag-canary__" in str(path)
]
if not candidates:
    raise SystemExit(1)
print(max(candidates, key=lambda path: path.stat().st_mtime).parent)
PY
}

validate_clean() {
  job=$1
  .venv/bin/python - "$job" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
artifacts = root / "artifacts" / "logs" / "artifacts"
result = json.loads((artifacts / "result.json").read_text())
reward = float((root / "verifier/reward.txt").read_text().strip())
if result.get("state") != "delivered" or reward != 1.0:
    raise SystemExit(
        f"clean cell did not succeed: state={result.get('state')!r}, reward={reward}"
    )
print(f"clean valid: state={result.get('state')} reward={reward}")
PY
}

validate_fault() {
  job=$1
  target=$2
  .venv/bin/python - "$job" "$target" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
target = sys.argv[2]
artifacts = root / "artifacts" / "logs" / "artifacts"
manifest = json.loads((artifacts / "run_manifest.json").read_text())
metrics = json.loads((artifacts / "metrics.json").read_text())
fault = manifest.get("fault_target_reachability", {})
if not fault.get("enabled") or fault.get("target") != target:
    raise SystemExit("fault cell is missing the target-reachability manifest contract")
if metrics.get("fault_target_reached") is not True or metrics.get("fault_target") != target:
    raise SystemExit(
        "fault target was not reached: "
        f"reached={metrics.get('fault_target_reached')!r}, "
        f"target={metrics.get('fault_target')!r}"
    )
result = json.loads((artifacts / "result.json").read_text())
reward_path = root / "verifier/reward.txt"
reward = float(reward_path.read_text().strip()) if reward_path.exists() else None
print(
    f"fault valid: target_reached=true state={result.get('state')} "
    f"reward={reward}"
)
PY
}

run_cell() {
  kind=$1
  policy=$2
  seed=$3
  graph=$4
  target=$5
  echo
  echo "== $kind / $graph / $policy / seed $seed =="
  if [ "$kind" = "clean" ]; then
    ORCH_FAULT_TASK= ORCH_TARGET_REACHABLE=0 \
      ORCH_BENCHMARK_MODEL="$model" \
      ORCH_GRAPH_SHAPE="$graph" \
      "$launcher" "$policy" "$seed" "$model" "$graph"
  else
    ORCH_FAULT_TASK="$target" \
      ORCH_TARGET_REACHABLE=1 \
      ORCH_FAULT_MODE=worker_exit \
      ORCH_FAULT_DELAY_S="${ORCH_BENCHMARK_FAULT_DELAY_S:-1.0}" \
      ORCH_FAST_CRASH_RECOVERY=1 \
      ORCH_BENCHMARK_MODEL="$model" \
      ORCH_GRAPH_SHAPE="$graph" \
      "$launcher" "$policy" "$seed" "$model" "$graph"
  fi
  job=$(latest_job) || die "Harbor completed but no benchmark artifact was found"
  echo "artifacts: $job"
  if [ "$kind" = "clean" ]; then
    validate_clean "$job"
  else
    validate_fault "$job" "$target"
  fi
}

for seed in $seeds; do
  case "$seed" in
    ''|*[!0-9]*) die "seeds must be space-separated non-negative integers: $seeds" ;;
  esac
  for graph in $graphs; do
    case "$graph" in
      dag) default_fault_task=schema ;;
      serial|wide|diamond|mixed) default_fault_task="${graph}-00" ;;
      *) die "unsupported graph shape: $graph" ;;
    esac
    fault_task=${fault_task_override:-$default_fault_task}
    for policy in sequential naive-parallel orchestrator; do
      run_cell clean "$policy" "$seed" "$graph" "$fault_task"
    done
    for policy in sequential naive-parallel orchestrator; do
      run_cell fault "$policy" "$seed" "$graph" "$fault_task"
    done
  done
done

echo
echo "benchmark complete: graphs=$graphs seeds=$seeds model=$model"
echo "Review each job's run_manifest.json, result.json, metrics.json, task_summary.json, and verifier/reward.txt."
