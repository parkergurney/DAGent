# Dependency-aware orchestrator benchmark

This Harbor task is a five-node task graph. Three root tasks can be scheduled
independently. `integration` depends on both `schema` and `implementation`,
and `release-check` depends on both `integration` and `documentation`.

The orchestrator assigns each graph node to a worker. Follow the task-specific
brief exactly, work only in the repository checkout, run the visible verifier
for the assigned node, and commit the change. Do not inspect or recreate
anything under `/tests`; hidden evaluation belongs to Harbor's separate
verifier. Do not report success unless the requested file is present and the
visible command passed. End every completed task response with `DONE_CLAIM`.

