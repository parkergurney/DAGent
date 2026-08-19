# Semantic-quality benchmark inputs

This directory defines the quality track for the Claude Harbor benchmark. It
does not place evaluation tests in the worker-visible repository. The launcher
materializes a temporary Harbor package from these manifests, pinned source
repositories, and the historical hidden-test commit selected by
`ORCH_QUALITY_SUITE`.

The recovered suites are:

- `original`: the 20-file suite from `86b5be6cb1a80b38cbfe7d449176097a456d5936`;
- `latest`: the 19-file pre-Harbor suite from
  `016ae283acac1beb2281d65e3243880af10ae0e2`, which incorporates the later
  TinyDB task revisions and `remove(doc_ids=...)` task.

The source fixtures are pinned to the local `bench-dirs` snapshots recorded in
`source-lock.json`. The worker image receives only those source trees and
public tests. Hidden tests are copied into the separate Harbor verifier image.

The quality runner supports one-task calibration cells and multi-task graph
cells. Start with one task per cell; only use a graph shape after the selected
tasks have passed the task-level calibration and their write scopes have been
reviewed.
