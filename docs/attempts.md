# Durable attempt state

An attempt is the durable unit of one worker execution. The `attempts` table
records its identity, task/run lineage, starting commit, candidate commit and
branch, public failure information, supervisor guidance, phase timestamps, and
final disposition. It also records worker dirty status captured before a
disposable checkout is released. `attempt/<attempt-id>` is a persistent Git
ref; pooled worktree directories are disposable checkouts of that ref.

The first attempt starts at the task's configured base branch. When a restart is
allowed, the next attempt records the previous attempt as `parent_attempt_id`
and checks out its `candidate_sha`. Supervisor feedback is stored on the parent
and copied into the child execution contract. A retry only returns to the
original base when the parent has no retained candidate (for example, before
any commit existed).

At startup, reconciliation marks dead workers interrupted, resolves the last
candidate ref where possible, and leaves the task in ordinary `triage`. If a
durably recorded `supervisor.acted` restart exists after that triage trigger,
the scheduler dispatches that action without invoking the supervisor again.
This closes the interruption window between triage and retry launch.

The execution contract contains only the normal brief, working directory,
visible verification command, commit requirement, delivery expectation, and
recovery guidance. Evaluator commands, paths, output, and assertions are never
included.

Public verification failures receive a deterministic SHA-256 signature of the
cause, exit code, and normalized failure line. ANSI escapes, absolute paths,
addresses, line numbers, and whitespace are removed.

`verification.recovered` is a separate metric event. It is emitted once when a
delivered attempt has an earlier failed verification ancestor. It is not
derived from an external evaluator, and a failed retry does not count.

## Event-triggered supervision (v2 phase two)

The normal path is worker → visible verification → delivery. A passing first
attempt makes no supervisor call. A model intervention is reserved for an
explicit event such as a public verification failure, worker ask, unexpected
exit, watchdog stall, or delivery failure.

The established wire actions remain compatible with the CLI (`restart`,
`wait`, `escalate`, `abandon`, and live-session `nudge`). Intervention reports
also persist canonical names: `RETRY`, `WAIT`, `ESCALATE_HUMAN`, `TERMINATE`,
and `NUDGE`. Worker-facing retry text is an explicit bounded instruction or
feedback field; supervisor reasoning is not copied into the worker contract.

Each model call has a `supervisor_interventions` row with source attempt and
candidate/signature, action, diagnosis, instruction, child attempt, token and
cost usage, and start/end timestamps. Child verification and final delivery
fill the observed outcome. `improved` means the child delivered, while
`no_improvement` means its public signature was unchanged;
`regressed_observed` means it failed with a different public signature. These
labels describe stored outcomes, not causal proof.

For recovery policy, `repeated_failure_threshold` defaults to one equivalent
descendant failure. Equivalence requires the same normalized public signature,
the same task attempt lineage, no material public evidence, and no material
committed-tree change. Material change is a non-empty `git diff` between the
parent and child candidate trees; a different commit with the same tree is not
material. Repeated equivalent failures escalate deterministically without a
second model call. A changed candidate or changed signature may receive a new
decision while retry budget remains. The maximum recovery retries is still
the task's explicit `max_retries`; exhausted budget records a deterministic
`retry_budget_exhausted` disposition.

Harbor evaluator failures never enter the worker retry loop. Other human
dispositions distinguish actionable public failure, missing information,
environment/tool limitation, worker terminal failure, and exhausted budget.
