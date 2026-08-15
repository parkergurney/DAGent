# Next three milestones: delegated implementation plan

Status: three Codex subagents deployed in parallel on 2026-08-11; scheduler
integration completed and validated on 2026-08-11.

The improvements backlog is in [improvements.md](improvements.md). This
document is the implementation plan for its first three milestones.

## Work allocation

| Workstream | Agent | Write scope | Integration owner |
|---|---|---|---|
| Execution leases and fencing | Nietzsche | `execution_lease.py`, store schema/migration/event APIs, focused tests | main agent: scheduler lifecycle hooks |
| Workflow preflight and conflict-aware planning | Ohm | `workflow_preflight.py`, Harbor entrypoint, focused tests | main agent: scheduler admission/manifest hooks |
| Targeted verification evidence ladder | Volta | `verify/evidence.py`, verify gate, metrics, focused tests | main agent: scheduler verification hooks |

Agents must not modify one another's write scopes. Each workstream left
the existing public APIs working unless an additive compatibility layer is
provided. Existing sequential and naive-parallel policies remain baselines.

Delivered: Nietzsche's leases are acquired and fenced by scheduler attempts;
Ohm's preflight conflict groups are consumed during admission; Volta's ladder
is enabled by default for the orchestrator policy and emits durable stage
events. Sequential and naive-parallel retain their baseline behavior.

## Integration sequence

1. Review each agent's focused tests and public API before merging its patch. **Complete.**
2. Integrate lease ownership into attempt creation, worker spawn, watchdog,
   process exit, reconciliation, and recovery dispatch.
3. Run preflight before task insertion/worker spawn and persist its plan in the
   run manifest; use its conflict recommendations in scheduler admission.
4. Run the evidence ladder from scheduler verification while keeping the full
   visible check as the final public gate.
5. Add cross-workstream tests for stale worker output during targeted
   verification, preflight rejection before lease acquisition, and replay of
   all new events.
6. Run focused suites, full regression, Ruff, compile checks, and the
   design/skill synchronization test. **Complete: focused 58 passed; full
   suite 209 passed and 4 skipped; Ruff, compile, and diff checks pass.**

## Acceptance bar

- A stale execution generation cannot mutate current task state, attempts, or
  candidate pointers.
- Duplicate process-exit, watchdog, or recovery signals are idempotent.
- Invalid DAGs, contracts, and unsafe write conflicts fail before worker spawn.
- Conflict-free independent work remains parallelizable.
- Preflight decisions and evidence-stage results are durable and manifest-ready.
- Cheap deterministic verification failures avoid unnecessary expensive checks.
- Targeted checks never replace the final full visible verification gate.
- Event replay, task state, attempt lineage, manifests, and metrics agree.
- All existing tests remain green.

## Risks and decisions

- Lease fencing must not make a valid late `ResultMessage` disappear after a
  process has already exited; the scheduler will accept only the current
  generation and record rejected events for diagnosis.
- Conflict analysis is advisory unless a write-scope overlap is explicit; the
  first implementation should serialize conservatively rather than infer
  semantic independence from natural-language briefs.
- Targeted verification is an early evidence stage, not a success criterion.
  Harbor hidden evaluation remains outside the worker-visible environment.
