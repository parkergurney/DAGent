#!/bin/sh
set -u

mkdir -p /logs/verifier
quality_file=/logs/verifier/quality_metrics.json
ok=1
expected_sha="$(git -C /app rev-parse HEAD)"
actual_sha="$(cat /logs/artifacts/base_sha.txt 2>/dev/null || true)"

if [ "$expected_sha" != "$actual_sha" ]; then
  ok=0
  python3 - "$quality_file" <<'PY'
import json
import sys
path = sys.argv[1]
json.dump({"schema_version": 1, "quality_score": 0.0,
          "error": "base_sha_mismatch", "tasks": []}, open(path, "w"), indent=2)
PY
fi

if [ "$ok" -eq 1 ]; then
  if [ -s /logs/artifacts/candidate.patch ]; then
    git -C /app apply --binary /logs/artifacts/candidate.patch || ok=0
  fi
fi
if [ "$ok" -eq 1 ]; then
  python3 /tests/grader.py --output "$quality_file" || ok=0
fi

if [ ! -f "$quality_file" ]; then
  python3 - "$quality_file" <<'PY'
import json
import sys
json.dump({"schema_version": 1, "quality_score": 0.0,
          "error": "candidate_patch_apply_failed", "tasks": []},
          open(sys.argv[1], "w"), indent=2)
PY
fi

score=0
if [ -f "$quality_file" ]; then
  score="$(python3 - "$quality_file" <<'PY'
import json
import sys
try:
    value = float(json.load(open(sys.argv[1])).get("quality_score", 0.0))
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    value = 0.0
print(max(0.0, min(1.0, value)))
PY
)"
fi
printf '%s\n' "$score" > /logs/verifier/reward.txt
[ "$ok" -eq 1 ]
