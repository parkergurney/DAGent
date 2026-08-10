#!/bin/sh
set -eu
printf 'ready\n' > /app/output.txt
git -C /app add output.txt
git -C /app commit -qm 'solve canary'
