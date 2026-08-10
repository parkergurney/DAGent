#!/bin/sh
set -u

mkdir -p /logs/verifier
ok=1
expected_sha="$(git -C /app rev-parse HEAD)"
actual_sha="$(cat /logs/artifacts/base_sha.txt 2>/dev/null || true)"
[ "$expected_sha" = "$actual_sha" ] || ok=0

if [ "$ok" -eq 1 ]; then
  git -C /app apply --binary /logs/artifacts/candidate.patch || ok=0
fi
if [ "$ok" -eq 1 ]; then
  python3 /tests/grader.py || ok=0
fi

if [ "$ok" -eq 1 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
