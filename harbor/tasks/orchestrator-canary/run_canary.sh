#!/bin/sh
set -eu

# Compute the task definition hash on the host. The agent container must not
# compute this itself because the complete task package includes verifier-only
# files. README and this launcher are operator documentation, not task inputs.
task_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$task_dir/../../.." && pwd)
policy=${1:-orchestrator}
seed=${2:-0}
model=${3:-qwen3-coder:30b}
backend=${ORCH_BACKEND:-ollama}
context_length=${ORCH_CONTEXT_LENGTH:-32768}

# Harbor imports custom agents in the controller process. This repository uses
# a src-layout package, so expose it explicitly for the import-path agent.
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
harbor_version=$(harbor --version 2>/dev/null || printf 'unknown')
config=$(printf '{"orchestrator":{"policy":"%s","seed":"%s","task_definition_sha256":"%s","harbor_version":"%s","backend":"%s","worker_model":"%s","supervisor_model":"%s","max_concurrency":2,"context_length":%s}}' \
    "$policy" "$seed" "$task_hash" "$harbor_version" "$backend" "$model" "$model" "$context_length")

exec harbor run \
  -y \
  -p "$task_dir" \
  -a orchestrator.harbor_agent:HarborOrchestratorAgent \
  -m "$model" \
  --ak "config=$config" \
  --allow-environment-host host.docker.internal
