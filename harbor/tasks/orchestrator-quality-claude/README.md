# Claude semantic-quality Harbor benchmark

This is a separate quality track from the file-artifact execution canary. The
launcher materializes this template with pinned Arrow, JSONSchema, and TinyDB
source fixtures. The worker image receives only those sources and public tests;
the historical hidden tests are copied only into the separate verifier image.

The historical suite defaults to the latest pre-Harbor version. Use
`ORCH_QUALITY_SUITE=original` to exercise the original 20-file snapshot.

The recommended first run is a task-level canary, where one real software task
is compared across the three policies. The runner supports `task`, `serial`,
`wide`, `diamond`, and `mixed` graph shapes, but multi-task shapes should only
be used after task-level calibration and write-scope review.

The verifier writes a fractional `verifier/quality_metrics.json` and uses its
`quality_score` as `verifier/reward.txt`. A task that passes only some hidden
checks is therefore distinguishable from a complete solution.
