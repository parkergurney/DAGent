"""Core control loop (design.md sections 2, 4, 6, 8, 11 / M2-M5).

Deterministic asyncio scheduler: promotes blocked -> queued -> running,
watches each spawned worker's event stream, runs the verify gate on a
done-claim, and funnels every stall/question/crash/failed-verify/
failed-delivery through triage -- one shape of problem, per section 4's
design note. Triage is resolved by a pluggable `supervisor` callable
(packet in, SupervisorResult out): the default is a deterministic
always-escalate stand-in so the FakeWorker regression suite stays free and
reproducible; a live manager wires up supervisor.llm.invoke_supervisor
instead -- bind its `model` kwarg via functools.partial/a closure, the
callable Scheduler holds takes only a packet. Same dependency-injection shape
as `spawn_worker`.

State-machine coordination for the async supervisor call (which can take
real wall-clock time) relies on nothing but the state column itself: triage
handling's first move is always the running/stalled -> triage transition,
committed synchronously before anything is awaited, so any other coroutine
(the watchdog, in particular) that checks "is this task still running?"
before acting sees the answer atomically and backs off on its own -- no
separate lock needed.
"""
import asyncio
from collections import defaultdict, deque
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from orchestrator import delivery
from orchestrator import execution_lease
from orchestrator.interfaces import (
    run_node_verification, validate_dependency_interfaces, validate_output_artifacts,
)
from orchestrator.recovery import (
    FailureClass, RecoveryAction, choose_recovery, classify_failure, recovery_payload,
)
from orchestrator.scheduler.reconcile import reconcile
from orchestrator.scheduler.adaptive import choose_task, effective_limit
from orchestrator.store import (
    append_event, create_attempt, create_intervention, interventions_for_target, latest_attempt,
    transition, ulid, update_attempt, update_intervention,
)
from orchestrator.supervisor import (
    ACTION_MODELS, always_escalate, build_packet, canonical_action_type,
)
from orchestrator.supervisor.llm import SupervisorResult
from orchestrator.verify.gate import VerifyRequest, run_verify
from orchestrator.worker import (
    WorktreePool, build_execution_contract, spawn_fake_worker,
)

# Team states in which nothing is left for the scheduler to drive; the team
# is "settled" once every task sits in one of these.
_SETTLED_STATES = ("needs_human", "delivered", "failed", "cancelled", "dependency_blocked")
_DEPENDENCY_BLOCKING_STATES = frozenset({"failed", "cancelled", "dependency_blocked"})


class WorkerStartupFailure(RuntimeError):
    """A worker failed before entering genuine model-backed task execution."""

    def __init__(self, task_id: str, category: str, reason: str):
        self.task_id = task_id
        self.category = category
        self.reason = reason
        super().__init__(f"worker startup failed for {task_id}: {category}: {reason}")


class SchedulerCleanupFailure(RuntimeError):
    """A cleanup operation failed after the scheduler took ownership of it."""

    def __init__(self, label: str, cause: BaseException):
        self.label = label
        self.cause = cause
        super().__init__(f"{label}: {cause}")
        self.__cause__ = cause


def _combine_cleanup_failures(title: str, failures: list[tuple[str, BaseException]]):
    """Return one deterministic exception while retaining every cause."""
    ordered = sorted(failures, key=lambda item: item[0])
    if len(ordered) == 1:
        label, cause = ordered[0]
        if isinstance(cause, SchedulerCleanupFailure) and cause.label != label:
            return SchedulerCleanupFailure(label, cause)
        return cause
    return BaseExceptionGroup(
        title,
        [SchedulerCleanupFailure(label, cause) for label, cause in ordered],
    )


def _append_unique_failure(
    failures: list[tuple[str, BaseException]], label: str, cause: BaseException,
) -> None:
    if not any(existing is cause for _label, existing in failures):
        failures.append((label, cause))


def validate_dependency_graph(conn) -> None:
    """Fail closed for missing prerequisites and cyclic task graphs.

    Task creation normally enforces missing references through SQLite foreign
    keys and task batches require dependencies to point backward. This
    check also protects resumed/manual databases and makes the scheduler's
    startup behavior deterministic before any worker is launched.
    """
    task_ids = {
        row["id"] for row in conn.execute("SELECT id FROM tasks")
    }
    edges = conn.execute(
        "SELECT task_id, depends_on FROM task_deps ORDER BY task_id, depends_on"
    ).fetchall()
    missing = sorted({edge["depends_on"] for edge in edges} - task_ids)
    if missing:
        raise ValueError(
            "dependency graph references missing prerequisite(s): " + ", ".join(missing)
        )

    indegree = {task_id: 0 for task_id in task_ids}
    dependents = defaultdict(list)
    for edge in edges:
        indegree[edge["task_id"]] += 1
        dependents[edge["depends_on"]].append(edge["task_id"])
    ready = deque(sorted(task_id for task_id, degree in indegree.items() if degree == 0))
    visited = 0
    while ready:
        task_id = ready.popleft()
        visited += 1
        for dependent in sorted(dependents[task_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if visited != len(task_ids):
        cycle_nodes = sorted(task_id for task_id, degree in indegree.items() if degree)
        raise ValueError(
            "dependency graph contains a cycle involving: " + ", ".join(cycle_nodes)
        )


def advance_dependency_states(conn, *, run_id: str, block_needs_human: bool = True) -> bool:
    """Settle blocked tasks to queued or dependency_blocked to a fixpoint.

    A fixpoint pass makes transitive propagation deterministic even when task
    insertion order does not happen to be topological. Only the scheduler
    state transition path is used; blocked tasks never create attempts or
    invoke workers/supervisors.
    """
    changed = False
    while True:
        pass_changed = False
        blocked = conn.execute(
            "SELECT id FROM tasks WHERE state = 'blocked' ORDER BY created_at, id"
        ).fetchall()
        for row in blocked:
            task_id = row["id"]
            deps = [r["depends_on"] for r in conn.execute(
                "SELECT depends_on FROM task_deps WHERE task_id = ? ORDER BY depends_on",
                (task_id,),
            )]
            if not deps:
                cause = append_event(
                    conn, source="scheduler", type="dep.satisfied", task_id=task_id,
                    payload={"run_id": run_id},
                )
                transition(conn, task_id, "queued", cause_seq=cause)
                pass_changed = changed = True
                continue

            dep_rows = []
            for dep in deps:
                dep_row = conn.execute(
                    "SELECT state FROM tasks WHERE id = ?", (dep,)
                ).fetchone()
                if dep_row is None:
                    # Graph validation should catch this before startup, but
                    # fail closed if a database is modified while a run lives.
                    raise ValueError(f"missing prerequisite {dep!r} for task {task_id!r}")
                dep_rows.append((dep, dep_row["state"]))

            blocking = [
                {"task_id": dep, "state": state}
                for dep, state in dep_rows
                if state in _DEPENDENCY_BLOCKING_STATES
                or (block_needs_human and state == "needs_human")
            ]
            if blocking:
                reason = "required prerequisite cannot succeed in this run"
                cause = append_event(
                    conn, source="scheduler", type="dep.blocked", task_id=task_id,
                    payload={
                        "run_id": run_id,
                        "blocked_task_id": task_id,
                        "blocking_prerequisites": blocking,
                        "reason": reason,
                    },
                )
                transition(conn, task_id, "dependency_blocked", cause_seq=cause)
                pass_changed = changed = True
            elif all(state == "delivered" for _, state in dep_rows):
                interface_ok, interface_detail = validate_dependency_interfaces(
                    conn, task_id, deps,
                )
                for dep in deps:
                    upstream = conn.execute("SELECT * FROM tasks WHERE id = ?", (dep,)).fetchone()
                    if upstream and interface_ok:
                        node_ok, node_detail = run_node_verification(dict(upstream))
                        interface_detail.setdefault("node_verification", {})[dep] = node_detail
                        if not node_ok:
                            interface_ok = False
                            interface_detail["reason"] = "node_verification_failed"
                            interface_detail["failed_task_id"] = dep
                            break
                validation_type = "interface.validation_passed" if interface_ok else "interface.validation_failed"
                validation_seq = append_event(
                    conn, source="verifier", type=validation_type, task_id=task_id,
                    payload={"run_id": run_id, "task_id": task_id,
                             "dependencies": deps, **interface_detail},
                )
                if not interface_ok:
                    cause = append_event(
                        conn, source="scheduler", type="dep.blocked", task_id=task_id,
                        payload={"run_id": run_id, "blocked_task_id": task_id,
                                 "blocking_prerequisites": [{"task_id": dep, "state": "interface_failed"}
                                                             for dep in deps],
                                 "reason": interface_detail.get("reason", "dependency interface failed"),
                                 "validation_seq": validation_seq},
                    )
                    transition(conn, task_id, "dependency_blocked", cause_seq=cause)
                    pass_changed = changed = True
                    continue
                cause = append_event(
                    conn, source="scheduler", type="dep.satisfied", task_id=task_id,
                    payload={"run_id": run_id, "interface_validation_seq": validation_seq},
                )
                transition(conn, task_id, "queued", cause_seq=cause)
                pass_changed = changed = True
        if not pass_changed:
            return changed


async def _terminate_and_reap(proc, *, terminate: bool = True) -> None:
    """Stop a worker process group and wait until it is fully gone."""
    if terminate:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        if not terminate:
            await proc.wait()
            return
        # The caller cannot safely enter verification if the worker was not
        # reaped.  Give the direct process object one final kill attempt; the
        # scheduler's outer teardown will record the same failure if this
        # unusual platform/process state persists.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


class Scheduler:
    def __init__(self, conn, repo_root, worktree_root, *,
                max_concurrency=4, stall_threshold_s=300, watchdog_interval_s=5,
                verify_timeout_s=600, spawn_worker=spawn_fake_worker, worker_model=None,
                supervisor=always_escalate,
                max_nudges=2, wait_ceiling_s=1800, transcript_tail_tokens=3000, yolo=False,
                base_branch="main", artifact_root=None, run_id=None,
                repeated_failure_threshold=1, deterministic_crash_recovery=False,
                adaptive_scheduling=False, protocol_recovery_v2=False,
                deterministic_recovery=False, evidence_ladder=False,
                preflight_plan=None):
        self.conn = conn
        self.repo_root = repo_root
        self.worktree_root = worktree_root
        self.max_concurrency = max_concurrency
        self.stall_threshold_s = stall_threshold_s
        self.watchdog_interval_s = watchdog_interval_s
        self.verify_timeout_s = verify_timeout_s
        self.spawn_worker = spawn_worker
        self.worker_model = worker_model
        self.supervisor = supervisor
        self.max_nudges = max_nudges
        self.wait_ceiling_s = wait_ceiling_s
        self.transcript_tail_tokens = transcript_tail_tokens
        self.yolo = yolo
        self.base_branch = base_branch
        self.run_id = run_id or ulid()
        self.repeated_failure_threshold = max(1, repeated_failure_threshold)
        # The orchestrator policy may handle an unambiguous non-zero worker
        # crash with one bounded retry without buying an LLM triage call.
        # Baseline policies leave this disabled so the policy comparison
        # changes only the intended recovery behavior.
        self.deterministic_crash_recovery = deterministic_crash_recovery
        self.adaptive_scheduling = adaptive_scheduling
        self.protocol_recovery_v2 = protocol_recovery_v2
        self.deterministic_recovery = deterministic_recovery
        self.evidence_ladder = evidence_ladder
        self.preflight_plan = preflight_plan or {}
        self._conflict_groups = {
            task_id: group
            for group in self.preflight_plan.get("conflicts", [])
            for task_id in group.get("task_ids", [])
        }
        self.artifact_root = Path(artifact_root).resolve() if artifact_root else None

        # Pool size == max_concurrency: never a reason for more slots than
        # tasks that can be running at once (design.md section 8 / M5).
        self._pool = WorktreePool(repo_root, worktree_root, max_concurrency)
        # `_procs` is the live-process registry used by the watchdog and
        # operator tooling. `_worker_slots` is the capacity lease registry.
        # They normally have the same keys, but keeping the lease explicit
        # closes the spawn/teardown boundary and makes double release
        # detectable instead of silently corrupting the limit.
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._worker_slots: dict[str, Path] = {}
        self._reaped_tasks: set[str] = set()
        self._reap_locks: dict[str, asyncio.Lock] = {}
        self._exit_watchers: dict[str, asyncio.Task] = {}
        self._watchers: dict[str, asyncio.Task] = {}
        # Teardown owns the process after it leaves _procs.  Keeping the
        # in-flight cleanup task durable in memory prevents scheduler shutdown
        # from cancelling a watchdog teardown after it has removed ownership
        # but before it has killed/reaped the process group and released the
        # slot.
        self._teardown_tasks: dict[str, asyncio.Task] = {}
        # A teardown can be initiated by a worker/watchdog task rather than
        # by run_until_settled().  Keep its exception until the scheduler can
        # surface it; otherwise a failed background task could leave a task
        # stuck in verifying while the outer loop continues normally.
        self._teardown_failures: dict[tuple[str, int], BaseException] = {}
        self._worktrees: dict[str, object] = {}
        self._last_event_ts: dict[str, float] = {}
        self._wait_grace: dict[str, float] = {}  # task_id -> seconds, set by a "wait" decision
        self._infrastructure_failure: WorkerStartupFailure | None = None
        self._leases: dict[str, execution_lease.ExecutionLease] = {}
        self._lease_heartbeat: dict[str, float] = {}
        self._triage_locks: dict[str, asyncio.Lock] = {}

    # -- public entry point -------------------------------------------------

    async def run_until_settled(self, *, forever: bool = False, poll_interval_s: float = 1.0) -> None:
        """Drive every currently blocked/queued/running/triage task to a
        resting state (needs_human or a terminal state), then return -- a
        fixed batch run to completion.

        With forever=True this never returns on its own: once the team
        settles it keeps polling (every poll_interval_s) for newly added
        tasks instead of exiting, so a separate `orchestrator add-task`
        process writing to the same SQLite file gets picked up without
        restarting this one. That's the whole of daemon mode; cancel the
        awaiting task (e.g. on Ctrl-C) to stop it -- the `finally` below
        still runs, tearing down any live worker cleanly.
        """
        reconcile(self.conn)
        validate_dependency_graph(self.conn)
        self._pool.open()
        for row in self.conn.execute("SELECT id FROM tasks WHERE state = 'triage'").fetchall():
            sc = self.conn.execute(
                "SELECT payload FROM events WHERE task_id = ? AND type = 'task.state_changed' "
                "ORDER BY seq DESC LIMIT 1", (row["id"],)).fetchone()
            trigger_seq = json.loads(sc["payload"])["cause_seq"]
            await self._handle_triage(row["id"], trigger_seq, live_proc=None)
        # A daemon crash after the worker-to-verifying transition must not
        # strand the task. Prefer durable terminal verification evidence when
        # it exists; only an incomplete verification is rerun.
        for row in self.conn.execute("SELECT id FROM tasks WHERE state = 'verifying'").fetchall():
            await self._resume_verifying(row["id"])
        # Delivery is also a resumable checkpoint. A successful delivery fact
        # is projected directly to delivered; an incomplete delivery runs
        # through the normal idempotent delivery path.
        for row in self.conn.execute("SELECT id FROM tasks WHERE state = 'delivering'").fetchall():
            await self._resume_delivering(row["id"])

        watchdog = asyncio.create_task(self._watchdog_loop())
        try:
            while forever or not self._team_settled():
                self._raise_recorded_teardown_failures()
                if self._infrastructure_failure:
                    raise self._infrastructure_failure
                self._advance_deps(block_needs_human=not forever)
                await self._launch_ready()
                self._raise_recorded_teardown_failures()
                if self._infrastructure_failure:
                    raise self._infrastructure_failure
                await asyncio.sleep(poll_interval_s if (forever and self._team_settled()) else 0.05)
        finally:
            pending_exception = sys.exc_info()[1]
            shutdown_failures: list[tuple[str, BaseException]] = []
            watchdog.cancel()
            watchdog_result = await asyncio.gather(watchdog, return_exceptions=True)
            for result in watchdog_result:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    _append_unique_failure(shutdown_failures, "watchdog", result)

            teardown_failures, teardown_cancelled = await self._await_teardowns_shielded()
            for label, failure in teardown_failures:
                _append_unique_failure(shutdown_failures, label, failure)
            for label, failure in self._pop_teardown_failures():
                _append_unique_failure(shutdown_failures, label, failure)
            try:
                self._pool.close()
            except BaseException as exc:
                _append_unique_failure(shutdown_failures, "worktree pool close", exc)

            if shutdown_failures:
                if pending_exception is not None:
                    pending_label = (
                        pending_exception.label
                        if isinstance(pending_exception, SchedulerCleanupFailure)
                        else "scheduler operation"
                    )
                    if not any(existing is pending_exception
                               for _label, existing in shutdown_failures):
                        shutdown_failures.insert(0, (pending_label, pending_exception))
                raise _combine_cleanup_failures(
                    "scheduler operation and cleanup failed" if pending_exception is not None
                    else "scheduler cleanup failed",
                    shutdown_failures,
                )
            if teardown_cancelled and pending_exception is None:
                raise asyncio.CancelledError

    async def _drain_teardowns(self) -> list[tuple[str, BaseException]]:
        """Await every cleanup owner currently known to the scheduler."""
        task_ids = sorted(set(self._worker_slots) | set(self._teardown_tasks))
        if not task_ids:
            return []
        results = await asyncio.gather(
            *(self._teardown(task_id) for task_id in task_ids),
            return_exceptions=True,
        )
        failures = []
        for task_id, result in zip(task_ids, results):
            if isinstance(result, BaseException):
                failures.append((f"teardown task {task_id}", result))
        return failures

    async def _await_teardowns_shielded(self) -> tuple[list[tuple[str, BaseException]], bool]:
        """Drain teardowns even when shutdown itself is being cancelled."""
        drain = asyncio.create_task(self._drain_teardowns())
        try:
            return await asyncio.shield(drain), False
        except asyncio.CancelledError:
            try:
                return await asyncio.shield(drain), True
            except BaseException as exc:
                return [("teardown drain", exc)], True

    def _pop_teardown_failures(self) -> list[tuple[str, BaseException]]:
        failures = [
            (f"teardown task {task_id}", failure)
            for (task_id, _cleanup_id), failure in self._teardown_failures.items()
        ]
        self._teardown_failures.clear()
        return failures

    def _record_teardown_failure(self, task_id: str, cleanup: asyncio.Task,
                                 failure: BaseException) -> None:
        self._teardown_failures.setdefault((task_id, id(cleanup)), failure)

    def _raise_recorded_teardown_failures(self) -> None:
        failures = self._pop_teardown_failures()
        if failures:
            raise _combine_cleanup_failures("scheduler teardown failed", failures)

    def _team_settled(self) -> bool:
        placeholders = ",".join("?" * len(_SETTLED_STATES))
        row = self.conn.execute(
            f"SELECT COUNT(*) c FROM tasks WHERE state NOT IN ({placeholders})",
            _SETTLED_STATES,
        ).fetchone()
        return row["c"] == 0

    # -- blocked -> queued ----------------------------------------------------

    def _advance_deps(self, *, block_needs_human: bool = True) -> bool:
        return advance_dependency_states(
            self.conn, run_id=self.run_id, block_needs_human=block_needs_human,
        )

    # -- queued -> running ----------------------------------------------------

    def _conflict_blocked(self, task_id: str) -> bool:
        """Keep preflight-detected overlapping writers out of the same slot."""
        group = self._conflict_groups.get(task_id)
        if not group:
            return False
        active = set(self._worker_slots)
        return any(other in active for other in group.get("task_ids", ()) if other != task_id)

    async def _launch_ready(self) -> None:
        while True:
            if self.adaptive_scheduling:
                limit, inputs = effective_limit(self.conn, self.max_concurrency,
                                                len(self._worker_slots))
            else:
                limit, inputs = self.max_concurrency, {
                    "base_limit": self.max_concurrency,
                    "effective_limit": self.max_concurrency,
                    "active_workers": len(self._worker_slots),
                }
            if len(self._worker_slots) >= limit:
                return
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE state = 'queued' ORDER BY created_at, id"
            ).fetchall()
            if not rows:
                return
            rows = [row for row in rows if not self._conflict_blocked(row["id"])]
            if not rows:
                return
            if self.adaptive_scheduling:
                row, scores = choose_task(self.conn, [dict(item) for item in rows])
                self._append_timing_event(
                    row["id"], "scheduler.decision",
                    payload={"policy": "adaptive", "selected_task": row["id"],
                             "inputs": inputs, "candidate_scores": scores},
                )
            else:
                row = rows[0]
            await self._launch(dict(row))

    async def _launch(self, task: dict, *, retries: int | None = None,
                      intervention_id: str | None = None) -> str:
        """(queued|triage) -> running. `retries`, when given, is a restart's
        new count -- transition() folds it into the same state-change event
        as session_id/worktree/base_sha, no separate write."""
        task_id = task["id"]
        intervention_id = intervention_id or task.get("_intervention_id")
        parent = latest_attempt(self.conn, task_id)
        resume = None
        if intervention_id:
            target = self.conn.execute(
                "SELECT target_attempt_id FROM supervisor_interventions WHERE id = ?",
                (intervention_id,),
            ).fetchone()
            if target and target["target_attempt_id"]:
                resume = self.conn.execute(
                    "SELECT * FROM attempts WHERE id = ? AND task_id = ?",
                    (target["target_attempt_id"], task_id),
                ).fetchone()
                if resume and resume["disposition"] != "created":
                    resume = None
            if resume is None:
                intervention = self.conn.execute(
                    "SELECT source_attempt_id FROM supervisor_interventions WHERE id = ?",
                    (intervention_id,),
                ).fetchone()
                candidate = latest_attempt(self.conn, task_id)
                if (intervention and candidate and candidate["disposition"] == "created"
                        and candidate["parent_attempt_id"] == intervention["source_attempt_id"]):
                    resume = candidate

        # A crash after child-attempt creation but before capacity acquisition
        # leaves the child durable and the task in triage. Reuse that identity
        # rather than creating a second child when reconciliation dispatches
        # the already-persisted RETRY decision.
        if resume:
            parent = self.conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (resume["parent_attempt_id"],)
            ).fetchone()
            attempt_id = resume["id"]
            branch = resume["candidate_branch"]
            starting_sha = resume["base_sha"]
            attempt_no = resume["attempt_no"]
            run_id = resume["run_id"]
        else:
            parent_candidate = parent["candidate_sha"] if parent else None
            starting_sha = parent_candidate or self._pool.resolve_ref(self.base_branch)
            attempt_no = (parent["attempt_no"] + 1) if parent else 1
            run_id = parent["run_id"] if parent else (task.get("run_id") or self.run_id)
            attempt_id = ulid()
            branch = f"attempt/{attempt_id}"
        recovery_feedback = task.get("recovery_feedback")

        # The contract is generated before the worker starts and persisted with
        # the attempt, so a restart can reconstruct exactly what was public.
        # It intentionally has no external evaluator fields.
        wt_hint = str(self.worktree_root / "pending")
        contract = build_execution_contract(task, wt_hint, recovery_feedback=recovery_feedback)
        if not resume:
            create_attempt(
                self.conn, attempt_id=attempt_id, task_id=task_id, run_id=run_id,
                attempt_no=attempt_no, parent_attempt_id=parent["id"] if parent else None,
                base_sha=starting_sha, candidate_branch=branch,
                execution_contract=contract, supervisor_feedback=recovery_feedback,
            )
        if intervention_id and not resume:
            update_intervention(self.conn, intervention_id, target_attempt_id=attempt_id)
        owner_id = f"scheduler:{self.run_id}:{attempt_id}"
        lease = execution_lease.acquire(
            self.conn, attempt_id, owner_id, source="scheduler",
        )
        self._leases[task_id] = lease
        self._lease_heartbeat[task_id] = time.monotonic()
        try:
            wt, base_sha = await self._pool.acquire(
                task_id, base_branch=self.base_branch, base_sha=starting_sha, branch=branch,
            )
        except BaseException:
            execution_lease.recover(
                self.conn, lease, reason="worktree_acquire_failed", source="scheduler",
            )
            self._leases.pop(task_id, None)
            self._lease_heartbeat.pop(task_id, None)
            raise
        self._worker_slots[task_id] = wt
        self._append_timing_event(
            task_id, "worker.slot_acquired", attempt_id=attempt_id,
            payload={"slot": str(wt), "occupancy": len(self._worker_slots),
                     "limit": self.max_concurrency},
        )
        contract = build_execution_contract(task, str(wt), recovery_feedback=recovery_feedback)
        update_attempt(self.conn, attempt_id, execution_contract=contract)
        worker_task = {**task, "execution_contract": contract,
                       "_fake_scenario": task.get("_fake_scenario", task["brief"])}
        try:
            proc = await self.spawn_worker(worker_task, wt, model=self.worker_model)
        except BaseException:
            execution_lease.recover(
                self.conn, lease, reason="worker_spawn_failed", source="scheduler",
            )
            self._leases.pop(task_id, None)
            self._lease_heartbeat.pop(task_id, None)
            raise
        if worker_task.get("_fault_injection_reached"):
            append_event(
                self.conn, source="system", type="fault_injection.target_reached",
                task_id=task_id, session_id=str(proc.pid),
                payload={"attempt_id": attempt_id,
                         "target": worker_task.get("_fault_injection_target", task_id),
                         "attempt": worker_task.get("_fault_injection_attempt", 1),
                         "mode": worker_task.get("_fault_injection_mode", "worker_exit")},
            )
        session_id = str(proc.pid)

        update_attempt(self.conn, attempt_id, worker_started_at=self._timestamp(),
                       disposition="running")

        s = append_event(self.conn, source="scheduler", type="worker.spawned",
                         task_id=task_id, session_id=session_id,
                         payload={"attempt_id": attempt_id, "base_sha": base_sha,
                                  "candidate_branch": branch})
        fields = dict(session_id=session_id, worktree=str(wt), base_sha=base_sha,
                      run_id=run_id, current_attempt_id=attempt_id,
                      candidate_sha=base_sha, candidate_branch=branch)
        if retries is not None:
            fields["retries"] = retries
        transition(self.conn, task_id, "running", cause_seq=s, **fields)

        self._procs[task_id] = proc
        self._reap_locks[task_id] = asyncio.Lock()
        self._exit_watchers[task_id] = asyncio.create_task(
            self._watch_process_exit(task_id, proc)
        )
        self._worktrees[task_id] = wt
        self._last_event_ts[task_id] = time.monotonic()
        self._watchers[task_id] = asyncio.create_task(
            self._watch(task_id, proc, attempt_id=attempt_id, lease=lease)
        )
        self._append_timing_event(
            task_id, "worker.started", attempt_id=attempt_id,
            session_id=session_id, payload={"slot": str(wt)},
        )
        return attempt_id

    @staticmethod
    def _timestamp() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def _append_timing_event(self, task_id: str, event_type: str, *, attempt_id=None,
                             session_id=None, payload=None) -> int:
        attempt = (self.conn.execute("SELECT run_id FROM attempts WHERE id = ?", (attempt_id,))
                   .fetchone() if attempt_id else None)
        details = {"attempt_id": attempt_id, "run_id": attempt["run_id"] if attempt else self.run_id}
        details.update(payload or {})
        return append_event(self.conn, source="scheduler", type=event_type, task_id=task_id,
                             session_id=session_id, payload=details)

    def _reject_worker_event(self, task_id: str, *, attempt_id: str | None,
                             lease: execution_lease.ExecutionLease | None,
                             event_type: str | None, reason: str,
                             session_id: str | None = None) -> None:
        """Record a stale worker signal without projecting it into task state."""
        current = self.conn.execute(
            "SELECT current_attempt_id, session_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        append_event(
            self.conn, source="watchdog", type="worker.event_rejected", task_id=task_id,
            session_id=session_id,
            payload={
                "attempt_id": attempt_id,
                "lease_id": lease.lease_id if lease else None,
                "generation": lease.generation if lease else None,
                "event_type": event_type,
                "reason": reason,
                "current_attempt_id": current["current_attempt_id"] if current else None,
                "current_session_id": current["session_id"] if current else None,
            },
        )

    def _validate_worker_lease(self, task_id: str, *, expected_attempt_id: str | None = None,
                               expected_lease: execution_lease.ExecutionLease | None = None,
                               event_type: str | None = None,
                               session_id: str | None = None) -> bool:
        """Fence output against the lease captured when this watcher started.

        Looking up only ``self._leases[task_id]`` is insufficient: after a
        restart that lookup returns the replacement attempt's lease, allowing
        a late line from the old process to masquerade as current output.
        """
        lease = self._leases.get(task_id)
        attempt = latest_attempt(self.conn, task_id)
        if expected_attempt_id is not None and (
            expected_lease is None
            or expected_lease.attempt_id != expected_attempt_id
            or lease is None
            or lease.lease_id != expected_lease.lease_id
            or lease.generation != expected_lease.generation
        ):
            self._reject_worker_event(
                task_id, attempt_id=expected_attempt_id, lease=expected_lease,
                event_type=event_type, reason="worker attempt is no longer current",
                session_id=session_id,
            )
            return False
        if lease is None or attempt is None or attempt["id"] != lease.attempt_id:
            self._reject_worker_event(
                task_id, attempt_id=expected_attempt_id or (attempt["id"] if attempt else None),
                lease=expected_lease or lease, event_type=event_type,
                reason="task has no current worker lease", session_id=session_id,
            )
            return False
        try:
            execution_lease.validate(self.conn, lease)
            now = time.monotonic()
            if now - self._lease_heartbeat.get(task_id, 0.0) >= max(1.0, self.stall_threshold_s / 3):
                lease = execution_lease.renew(self.conn, lease, source="scheduler")
                self._leases[task_id] = lease
                self._lease_heartbeat[task_id] = now
            return True
        except execution_lease.ExecutionLeaseError as exc:
            self._reject_worker_event(
                task_id, attempt_id=expected_attempt_id or attempt["id"], lease=lease,
                event_type=event_type, reason=str(exc), session_id=session_id,
            )
            return False

    # -- running: read the worker's event stream -----------------------------

    def _capture_candidate(self, task_id: str, *, disposition="worker_ended",
                           failure_cause=None) -> str | None:
        task = self.conn.execute(
            "SELECT current_attempt_id, worktree FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not task or not task["current_attempt_id"]:
            return None
        candidate = None
        worker_dirty = None
        if task["worktree"]:
            try:
                worktree = Path(task["worktree"])
                candidate = self._pool.head(worktree)
                status = subprocess.run(["git", "status", "--porcelain"], cwd=worktree,
                                        capture_output=True, text=True)
                worker_dirty = status.stdout if status.stdout.strip() else None
            except (OSError, RuntimeError):
                pass
        fields = {"disposition": disposition, "worker_ended_at": self._timestamp()}
        if candidate:
            fields["candidate_sha"] = candidate
        if worker_dirty:
            fields["worker_dirty"] = worker_dirty
        if failure_cause:
            fields["failure_cause"] = failure_cause
        update_attempt(self.conn, task["current_attempt_id"], **fields)
        if candidate:
            # Candidate metadata is ordinary task projection state, but is
            # folded through the same state transition that records the cause.
            return candidate
        return None

    async def _watch(self, task_id: str, proc: asyncio.subprocess.Process, *,
                     attempt_id: str, lease: execution_lease.ExecutionLease) -> None:
        claimed_or_triaged = False
        sdk_result_ok = False
        last_result_usage = {}
        protocol_incomplete = False
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype, payload = rec.get("type"), rec.get("payload", {})
                if not self._validate_worker_lease(
                    task_id, expected_attempt_id=attempt_id, expected_lease=lease,
                    event_type=etype, session_id=str(proc.pid),
                ):
                    # This watcher belongs to an old or fenced attempt.  Its
                    # process is cleaned up by the finally block, but none of
                    # its output can mutate current task state.
                    break
                self._last_event_ts[task_id] = time.monotonic()
                # ResultMessage is the canonical aggregate for a worker
                # session. AssistantMessage usage is retained in its payload
                # for diagnostics, but is not put in event accounting columns;
                # otherwise a session is double-counted.
                event_usage = {}
                if etype == "result":
                    sdk_result_ok = not bool(payload.get("is_error"))
                    last_result_usage = dict(
                        tokens_in=payload.get("tokens_in"),
                        tokens_out=payload.get("tokens_out"),
                        cost_usd=payload.get("cost_usd"),
                    )
                    event_usage = last_result_usage
                attempt = latest_attempt(self.conn, task_id)
                event_payload = {**payload, "attempt_id": attempt["id"] if attempt else None}

                if etype == "done_claimed":
                    if sdk_result_ok:
                        event_payload["protocol_result"] = {
                            "sdk_success": True,
                            "worker_exited": True,
                            "terminal_metadata_ok": True,
                        }
                    s = append_event(self.conn, source="worker", type="worker.done_claimed",
                                     task_id=task_id, session_id=str(proc.pid), payload=event_payload,
                                     **last_result_usage)
                    claimed_or_triaged = True
                    # A done claim is a stream message, not proof that the
                    # SDK/Claude process has exited.  Reap it before the
                    # verification is allowed to inspect the committed candidate.
                    await self._reap_process(task_id, proc)
                    candidate = self._capture_candidate(task_id)
                    self._enter_verifying(task_id, s, candidate_sha=candidate)
                    # Candidate SHA/attempt facts are durable before this
                    # releases the pooled checkout. Verification uses that
                    # SHA, not the now-reusable worker path.
                    await self._teardown(task_id, expect_proc=proc)
                    await self._run_verify(task_id)
                    break
                elif etype == "asked":
                    s = append_event(self.conn, source="worker", type="worker.asked",
                                     task_id=task_id, session_id=str(proc.pid), payload=event_payload,
                                     **last_result_usage)
                    keep_watching = await self._handle_triage(task_id, s, live_proc=proc)
                    if keep_watching:
                        self._last_event_ts[task_id] = time.monotonic()
                        continue  # a nudge landed on proc.stdin; keep reading this same session
                    claimed_or_triaged = True
                    self._append_terminal_classification(task_id, "asked", cause_seq=s)
                    break
                elif etype == "no_change":
                    if sdk_result_ok:
                        event_payload["protocol_result"] = {
                            "sdk_success": True,
                            "worker_exited": True,
                            "terminal_metadata_ok": True,
                        }
                    s = append_event(self.conn, source="worker", type="worker.no_change",
                                     task_id=task_id, session_id=str(proc.pid), payload=event_payload,
                                     **last_result_usage)
                    claimed_or_triaged = True
                    await self._reap_process(task_id, proc)
                    candidate = self._capture_candidate(task_id, disposition="no_change")
                    self._enter_verifying(task_id, s, candidate_sha=candidate)
                    await self._teardown(task_id, expect_proc=proc)
                    await self._run_verify(task_id)
                    break
                elif etype == "startup_failed":
                    category = str(payload.get("category") or "other_infrastructure_startup_failure")
                    reason = str(payload.get("reason") or payload.get("error") or category)[:500]
                    s = append_event(self.conn, source="worker", type="worker.startup_failed",
                                     task_id=task_id, session_id=str(proc.pid),
                                     payload={**event_payload, "category": category,
                                              "reason": reason})
                    claimed_or_triaged = True
                    self._append_terminal_classification(task_id, "startup_failure", cause_seq=s,
                                                         category=category)
                    self._capture_candidate(task_id, disposition="startup_failed",
                                            failure_cause=category)
                    self._infrastructure_failure = WorkerStartupFailure(task_id, category, reason)
                    # This is an infrastructure abort, not a worker/task
                    # failure. It must never enter the supervisor policy.
                    await self._teardown(task_id, expect_proc=proc)
                    break
                elif etype == "sdk_failed":
                    s = append_event(self.conn, source="worker", type="worker.sdk_failure",
                                     task_id=task_id, session_id=str(proc.pid),
                                     payload={**event_payload, "failure_class": "sdk_failure"})
                    claimed_or_triaged = True
                    self._capture_candidate(task_id, disposition="sdk_failure",
                                            failure_cause="sdk_failure")
                    self._append_terminal_classification(task_id, "sdk_failure", cause_seq=s)
                    await self._teardown(task_id, expect_proc=proc)
                    break
                elif etype == "unclaimed":
                    protocol_incomplete = True
                    append_event(self.conn, source="worker", type="worker.protocol_incomplete",
                                 task_id=task_id, session_id=str(proc.pid),
                                 payload={**event_payload, "failure_class": "protocol_incomplete"})
                else:
                    append_event(self.conn, source="worker", type=f"worker.{etype}",
                                task_id=task_id, session_id=str(proc.pid), payload=event_payload,
                                **event_usage)

            if not claimed_or_triaged:
                code = await proc.wait()
                if sdk_result_ok and protocol_incomplete and code == 0:
                    s = self._mark_running_failure(
                        task_id, source="worker", event_type="worker.protocol_incomplete",
                        payload={"exit_code": code, "failure_class": "protocol_incomplete"},
                        session_id=str(proc.pid),
                    )
                    if s is not None:
                        candidate = self._capture_candidate(
                            task_id, disposition="protocol_incomplete",
                            failure_cause="protocol_incomplete",
                        )
                        self._append_terminal_classification(task_id, "protocol_incomplete", cause_seq=s)
                        await self._teardown(task_id, expect_proc=proc)
                        await self._handle_triage(task_id, s, live_proc=None,
                                                  candidate_sha=candidate)
                    return
                s = self._mark_running_failure(task_id, source="worker", event_type="worker.exited",
                                               payload={"exit_code": code}, session_id=str(proc.pid))
                if s is not None:
                    self._append_terminal_classification(task_id, "worker_crash", cause_seq=s,
                                                         exit_code=code)
                    candidate = self._capture_candidate(task_id, disposition="worker_failed",
                                                        failure_cause="worker.exited")
                    # No live worker remains. Release capacity before any
                    # supervisor await so independent queued work can launch.
                    await self._teardown(task_id, expect_proc=proc)
                    await self._handle_triage(task_id, s, live_proc=None,
                                              candidate_sha=candidate)
        except (SchedulerCleanupFailure, BaseExceptionGroup):
            # The authoritative teardown task has recorded the failure for
            # the scheduler loop.  Do not leave an unobserved exception on
            # this background watcher task.
            pass
        finally:
            try:
                await self._teardown(task_id, expect_proc=proc)
            except (SchedulerCleanupFailure, BaseExceptionGroup):
                pass

    def _mark_running_failure(self, task_id: str, *, source, event_type, payload=None,
                              session_id=None) -> int | None:
        """Append a failure event iff the task is still 'running' -- a no-op
        guard, not a lock: nothing here awaits between the read and the
        write, so no other coroutine can interleave. Returns the new event's
        seq, or None if something else already moved the task on."""
        row = self.conn.execute(
            "SELECT state, current_attempt_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["state"] != "running":
            return None
        attempt_id = row["current_attempt_id"]
        if attempt_id and event_type in {"worker.exited", "worker.stalled"}:
            duplicate = self.conn.execute(
                "SELECT 1 FROM events WHERE task_id = ? "
                "AND type IN ('worker.exited', 'worker.stalled') "
                "AND json_extract(payload, '$.attempt_id') = ? LIMIT 1",
                (task_id, attempt_id),
            ).fetchone()
            # A supervisor ``wait`` deliberately starts a new silence window
            # for the same attempt. Its later stall is a new signal, while a
            # second observer reporting the original stall is a duplicate.
            new_wait_window = event_type == "worker.stalled" and task_id in self._wait_grace
            if duplicate and not new_wait_window:
                return None
        event_payload = {**(payload or {})}
        if attempt_id:
            event_payload.setdefault("attempt_id", attempt_id)
        return append_event(self.conn, source=source, type=event_type, task_id=task_id,
                            payload=event_payload, session_id=session_id)

    def _append_terminal_classification(self, task_id: str, classification: str, *,
                                        cause_seq: int | None = None, **extra) -> int:
        """Persist the terminal interpretation separately from task state."""
        payload = {"classification": classification}
        if cause_seq is not None:
            payload["cause_seq"] = cause_seq
        payload.update(extra)
        return append_event(self.conn, source="system", type="task.terminal_classified",
                            task_id=task_id, payload=payload)

    async def _apply_protocol_recovery(self, task_id: str, cause_seq: int) -> bool:
        """Perform exactly one metadata-repair retry for a clean SDK result."""
        if not self.protocol_recovery_v2:
            return False
        cause = self.conn.execute("SELECT type FROM events WHERE seq = ?", (cause_seq,)).fetchone()
        if not cause or cause["type"] != "worker.protocol_incomplete":
            return False
        task_row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        attempt = latest_attempt(self.conn, task_id)
        if not task_row or not attempt or task_row["retries"] >= task_row["max_retries"]:
            return False
        prior = self.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE task_id = ? AND type = 'recovery.policy_applied' "
            "AND json_extract(payload, '$.diagnosis_code') = 'protocol_incomplete_repair'",
            (task_id,),
        ).fetchone()["c"]
        decision = choose_recovery(FailureClass.PROTOCOL_INCOMPLETE,
                                   retries=task_row["retries"], max_retries=task_row["max_retries"],
                                   protocol_retries=prior)
        if decision.action is not RecoveryAction.REPAIR:
            return False
        policy_seq = append_event(
            self.conn, source="system", type="recovery.policy_applied", task_id=task_id,
            payload=recovery_payload(decision, cause_seq=cause_seq, attempt_id=attempt["id"],
                                     diagnosis_code="protocol_incomplete_repair",
                                     recovery_class="protocol_incomplete"),
        )
        feedback = (
            "The previous SDK session completed without the optional terminal metadata. "
            "Inspect the candidate, run visible verification, commit the result, and end "
            "with exactly one DONE_CLAIM line."
        )
        update_attempt(self.conn, attempt["id"], disposition="retry_requested",
                       failure_cause="protocol_incomplete", failure_signature="protocol_incomplete",
                       supervisor_feedback=feedback)
        await self._teardown(task_id)
        await self._launch({**dict(task_row), "brief": task_row["brief"],
                            "recovery_feedback": feedback},
                           retries=task_row["retries"] + 1)
        append_event(self.conn, source="system", type="recovery.attempted", task_id=task_id,
                     payload={"action": decision.action.value, "cause_seq": cause_seq,
                              "policy_seq": policy_seq, "attempt_id": attempt["id"]})
        return True

    async def _apply_public_recovery(self, task_id: str, cause_seq: int) -> bool:
        """Repair unambiguous public candidate failures without supervision."""
        if not self.deterministic_recovery:
            return False
        cause = self.conn.execute(
            "SELECT type, payload FROM events WHERE seq = ?", (cause_seq,)
        ).fetchone()
        if not cause or cause["type"] not in {
            "verify.failed", "artifact.validation_failed", "interface.validation_failed",
        }:
            return False
        payload = json.loads(cause["payload"] or "{}")
        failure_class = classify_failure(cause["type"], payload)
        task = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        attempt = latest_attempt(self.conn, task_id)
        if not task or not attempt:
            return False
        decision = choose_recovery(failure_class, retries=task["retries"],
                                   max_retries=task["max_retries"])
        if decision.action not in {RecoveryAction.REPAIR, RecoveryAction.RETRY_RETAINED_CANDIDATE}:
            return False
        feedback = payload.get("output_tail") or payload.get("reason") or (
            "Repair the public candidate failure and rerun the visible verification."
        )
        feedback = str(feedback)[:2000]
        policy_seq = append_event(
            self.conn, source="system", type="recovery.policy_applied", task_id=task_id,
            payload=recovery_payload(decision, cause_seq=cause_seq, attempt_id=attempt["id"],
                                     diagnosis_code=failure_class.value),
        )
        update_attempt(self.conn, attempt["id"], disposition="retry_requested",
                       supervisor_feedback=feedback)
        await self._teardown(task_id)
        await self._launch({**dict(task), "recovery_feedback": feedback},
                           retries=task["retries"] + 1)
        append_event(self.conn, source="system", type="recovery.attempted", task_id=task_id,
                     payload={"action": decision.action.value, "failure_class": failure_class.value,
                              "cause_seq": cause_seq, "policy_seq": policy_seq,
                              "attempt_id": attempt["id"]})
        return True

    # -- triage: build a packet, ask the supervisor, dispatch its decision ---

    @staticmethod
    def _worker_instruction(action) -> str | None:
        """Return bounded, explicit worker guidance from a legacy action."""
        instruction = getattr(action, "worker_instruction", None)
        if instruction is None:
            instruction = getattr(action, "feedback", None)
        if instruction is None and action.action == "nudge":
            instruction = getattr(action, "message", None)
        return instruction[:2000] if instruction else None

    @staticmethod
    def _diagnosis_code(action, packet) -> str:
        explicit = getattr(action, "diagnosis_code", None)
        if explicit and explicit != "human_review":
            return explicit
        if action.action == "escalate":
            if packet.retries_remaining <= 0:
                return "retry_budget_exhausted"
            if packet.trigger.type == "verify.failed":
                return "public_failure_actionable"
            if packet.trigger.type == "worker.asked":
                return "missing_information_requires_person"
            if packet.trigger.type == "worker.exited":
                return "worker_terminal_failure"
            if packet.trigger.type == "worker.stalled":
                return "environment_tool_limit"
        return explicit or "human_review"

    def _finish_interventions(self, task_id: str, delivery_outcome: str,
                              recovery_outcome: str | None = None) -> None:
        rows = self.conn.execute(
            "SELECT id FROM supervisor_interventions WHERE task_id = ? "
            "AND eventual_delivery_outcome IS NULL", (task_id,)
        ).fetchall()
        for row in rows:
            fields = {"eventual_delivery_outcome": delivery_outcome}
            if recovery_outcome:
                fields["verification_recovery_outcome"] = recovery_outcome
            update_intervention(self.conn, row["id"], **fields)

    def _update_child_interventions(self, task_id: str, attempt, *, passed: bool) -> None:
        """Attach a descendant result to the intervention that created it.

        These labels describe observed outcome, not proof that a model caused
        it. A changed failure is called ``regressed_observed`` only to make the
        visible result distinguishable from an unchanged failure.
        """
        for row in interventions_for_target(self.conn, attempt["id"]):
            fields = {
                "child_candidate_sha": attempt["candidate_sha"],
                "child_failure_signature": attempt["failure_signature"],
            }
            if passed:
                fields["verification_recovery_outcome"] = "improved"
            elif attempt["failure_signature"] == row["source_failure_signature"]:
                fields["verification_recovery_outcome"] = "no_improvement"
            elif row["source_failure_signature"] and attempt["failure_signature"]:
                fields["verification_recovery_outcome"] = "regressed_observed"
            else:
                fields["verification_recovery_outcome"] = "cannot_yet_evaluate"
            update_intervention(self.conn, row["id"], **fields)

    def _candidate_materially_changed(self, task_id: str, previous, current) -> bool:
        """Compare committed trees, not commit IDs, for recovery evidence.

        A retry that creates a new commit with the same tree is unchanged. A
        non-empty Git diff between the previous failed candidate and the new
        candidate is material. Missing refs are treated as changed only when
        the candidate identity itself changed; this keeps the policy safe
        during partial/crashed writes without consulting external evaluation.
        """
        if not previous or not current:
            return False
        before = previous["candidate_sha"] or previous["base_sha"]
        after = current["candidate_sha"] or current["base_sha"]
        if not before or not after or before == after:
            return False
        task = self.conn.execute("SELECT repo FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return True
        result = subprocess.run(
            ["git", "diff", "--quiet", before, after], cwd=task["repo"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return False
        if result.returncode == 1:
            return True
        # An unavailable object is not evidence that the candidate is the
        # same. Let a bounded supervisor decision handle that uncertainty.
        return True

    def _failure_evaluation(self, task_id: str, attempt):
        """Return structured public evidence for a failed retry, if any."""
        if not attempt or not attempt["failure_signature"]:
            return None
        task = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        lineage = []
        parent_id = attempt["parent_attempt_id"]
        while parent_id:
            parent = self.conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (parent_id,)
            ).fetchone()
            if parent is None:
                break
            lineage.append(parent)
            parent_id = parent["parent_attempt_id"]
        same_signature = [a for a in lineage
                          if a["failure_signature"] == attempt["failure_signature"]]
        # The direct parent is the evidence relationship even when the
        # signature changed; equivalent-signature history is counted
        # separately for deterministic repetition policy.
        previous = lineage[0] if lineage else None
        candidate_changed = self._candidate_materially_changed(task_id, previous, attempt)
        return {
            "attempt_id": attempt["id"],
            "previous_attempt_id": previous["id"] if previous else None,
            "previous_failure_signature": previous["failure_signature"] if previous else None,
            "failure_signature": attempt["failure_signature"],
            "candidate_changed": candidate_changed,
            "same_signature_count": len(same_signature),
            "retry_count": task["retries"],
            "retry_budget": task["max_retries"],
        }

    def _persist_failure_evaluation(self, task_id: str, evaluation) -> int:
        return append_event(self.conn, source="scheduler", type="recovery.evaluated",
                            task_id=task_id, payload=evaluation)

    async def _apply_deterministic_policy(self, task_id: str, cause_seq: int) -> bool:
        """Resolve cases where another model call cannot add public evidence.

        Returns True when triage was fully resolved. This path deliberately
        creates no supervisor intervention row and no supervisor model event.
        """
        attempt = latest_attempt(self.conn, task_id)
        evaluation = self._failure_evaluation(task_id, attempt)
        if evaluation is None:
            return False
        self._persist_failure_evaluation(task_id, evaluation)
        repeated = (
            evaluation["same_signature_count"] >= self.repeated_failure_threshold
            and not evaluation["candidate_changed"]
        )
        budget_exhausted = (
            evaluation["retry_count"] >= evaluation["retry_budget"]
            and attempt["attempt_no"] > 1
        )
        if not repeated and not budget_exhausted:
            return False

        code = "repeated_identical_failure" if repeated else "retry_budget_exhausted"
        reason = (
            "the retry produced the same normalized public failure and no material candidate change"
            if repeated else "the configured recovery retry budget is exhausted"
        )
        policy_seq = append_event(
            self.conn, source="system", type="recovery.policy_applied", task_id=task_id,
            payload={"action_type": "ESCALATE_HUMAN", "diagnosis_code": code,
                     "attempt_id": attempt["id"], "previous_attempt_id": evaluation["previous_attempt_id"],
                     "previous_failure_signature": evaluation["previous_failure_signature"],
                     "failure_signature": evaluation["failure_signature"],
                     "candidate_changed": evaluation["candidate_changed"], "reason": reason},
        )
        update_attempt(self.conn, attempt["id"], disposition=code)
        self._finish_interventions(task_id, "needs_human", "no_improvement" if repeated else "cannot_yet_evaluate")
        transition(self.conn, task_id, "needs_human", cause_seq=policy_seq)
        await self._teardown(task_id)
        return True

    async def _apply_deterministic_crash_recovery(self, task_id: str,
                                                  cause_seq: int) -> bool:
        """Retry one explicit worker crash without an avoidable model call.

        A non-zero ``worker.exited`` is an unambiguous process failure: the
        worker produced no completion claim and startup failures have already
        been classified separately. The retry still uses the normal launch
        path, candidate lineage, worktree pool, verification gate, and retry
        cap. Ambiguous failures continue through the supervisor.
        """
        if not self.deterministic_crash_recovery:
            return False
        cause = self.conn.execute(
            "SELECT type, payload FROM events WHERE seq = ?", (cause_seq,)
        ).fetchone()
        if not cause or cause["type"] != "worker.exited":
            return False
        try:
            payload = json.loads(cause["payload"])
        except (TypeError, json.JSONDecodeError):
            return False
        if int(payload.get("exit_code", 0)) == 0:
            return False
        attempt = latest_attempt(self.conn, task_id)
        task_row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not attempt or not task_row or attempt["failure_cause"] != "worker.exited":
            return False
        if task_row["retries"] >= task_row["max_retries"]:
            return False

        task = dict(task_row)
        append_event(
            self.conn, source="system", type="recovery.policy_applied", task_id=task_id,
            payload={
                "action_type": "RESTART",
                "action": RecoveryAction.RETRY_RETAINED_CANDIDATE.value,
                "failure_class": FailureClass.WORKER_CRASH.value,
                "diagnosis_code": "worker_crash_retry",
                "cause_seq": cause_seq,
                "attempt_id": attempt["id"],
                "previous_candidate_sha": attempt["candidate_sha"] or attempt["base_sha"],
                "reason": "non-zero worker exit has no completion claim; retry within budget",
            },
        )
        update_attempt(
            self.conn, attempt["id"],
            disposition="retry_requested",
            supervisor_feedback="The previous worker process exited before completion. Continue from the retained candidate and finish the task.",
        )
        await self._teardown(task_id)
        feedback = (
            "The previous worker process exited before completion. Continue from "
            "the retained candidate and finish the task."
        )
        brief = f"{task['brief']}\n\nRecovery feedback:\n{feedback}"
        await self._launch(
            {**task, "brief": brief, "_fake_scenario": task.get("_fake_scenario") or task["brief"],
             "recovery_feedback": feedback},
            retries=task["retries"] + 1,
        )
        append_event(self.conn, source="system", type="recovery.attempted", task_id=task_id,
                     payload={"action": RecoveryAction.RETRY_RETAINED_CANDIDATE.value,
                              "failure_class": FailureClass.WORKER_CRASH.value,
                              "cause_seq": cause_seq, "attempt_id": attempt["id"]})
        # The immediate state transition is caused by worker.spawned; the
        # policy event remains in the durable causality chain.
        return True

    async def _handle_triage(self, task_id: str, cause_seq: int, *, live_proc,
                             candidate_sha=None) -> bool:
        lock = self._triage_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            # A signal can arrive from both the worker reader and the
            # watchdog, or be replayed after a restart. Once this exact cause
            # has produced a recovery action, it must not consume another
            # retry, supervisor call, or state transition.
            row = self.conn.execute(
                "SELECT state FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not row or row["state"] in _SETTLED_STATES:
                return False
            handled = self.conn.execute(
                "SELECT type FROM events WHERE task_id = ? AND type IN "
                "('recovery.attempted', 'supervisor.acted') "
                "AND (json_extract(payload, '$.cause_seq') = ? OR "
                "(json_extract(payload, '$.cause_seq') IS NULL AND seq > ?)) LIMIT 1",
                (task_id, cause_seq, cause_seq),
            ).fetchone()
            # A persisted supervisor action with a task still in triage is an
            # intentional restart-recovery window: dispatch it without
            # invoking the model again. Any other state already consumed the
            # signal and must remain untouched.
            if handled and not (handled["type"] == "supervisor.acted" and row["state"] == "triage"):
                return False
            if row["state"] != "triage":
                transition(self.conn, task_id, "triage", cause_seq=cause_seq,
                           **({"candidate_sha": candidate_sha} if candidate_sha else {}))
            attempt = latest_attempt(self.conn, task_id)
            started = self.conn.execute(
                "SELECT seq FROM events WHERE task_id = ? AND type = 'triage.started' "
                "AND json_extract(payload, '$.cause_seq') = ? LIMIT 1",
                (task_id, cause_seq),
            ).fetchone()
            if not started:
                self._append_timing_event(
                    task_id, "triage.started", attempt_id=attempt["id"] if attempt else None,
                    payload={"cause_seq": cause_seq},
                )
            try:
                return await self._handle_triage_inner(task_id, cause_seq, live_proc=live_proc)
            finally:
                # The event is deliberately emitted even when the process crashes
                # during model transport; reconciliation can then close the
                # interrupted triage deterministically.
                self._append_timing_event(
                    task_id, "triage.finished", attempt_id=attempt["id"] if attempt else None,
                    payload={"cause_seq": cause_seq},
                )

    async def _handle_triage_inner(self, task_id: str, cause_seq: int, *, live_proc) -> bool:
        """Returns True iff the caller should keep watching `live_proc` (only
        possible for nudge/wait); False means this attempt is over -- either
        genuinely finished (escalate/abandon) or replaced by a freshly
        launched attempt the caller no longer owns (restart)."""
        if await self._apply_deterministic_crash_recovery(task_id, cause_seq):
            return False
        if await self._apply_protocol_recovery(task_id, cause_seq):
            return False
        if await self._apply_public_recovery(task_id, cause_seq):
            return False
        if await self._apply_deterministic_policy(task_id, cause_seq):
            return False

        # A process can die after reserving an intervention but before its
        # action event. The model result is then unknowable; calling again
        # would duplicate paid supervision. Escalate this explicit transport
        # gap rather than guessing or restarting from the base state.
        unfinished = self.conn.execute(
            "SELECT id FROM supervisor_interventions WHERE task_id = ? "
            "AND ended_at IS NULL ORDER BY created_at DESC LIMIT 1", (task_id,)
        ).fetchone()
        if unfinished:
            current = latest_attempt(self.conn, task_id)
            policy_seq = append_event(
                self.conn, source="system", type="recovery.policy_applied", task_id=task_id,
                payload={"action_type": "ESCALATE_HUMAN", "diagnosis_code": "environment_tool_limit",
                         "attempt_id": current["id"] if current else None,
                         "intervention_id": unfinished["id"],
                         "reason": "supervisor intervention ended before its decision was persisted"},
            )
            update_intervention(
                self.conn, unfinished["id"], action_type="ESCALATE_HUMAN",
                diagnosis_code="environment_tool_limit", ended_at=self._timestamp(),
                eventual_delivery_outcome="needs_human",
                verification_recovery_outcome="cannot_yet_evaluate",
            )
            if current:
                update_attempt(self.conn, current["id"], disposition="supervisor_interrupted")
            transition(self.conn, task_id, "needs_human", cause_seq=policy_seq)
            await self._teardown(task_id)
            return False

        packet = build_packet(self.conn, task_id, cause_seq, yolo=self.yolo,
                              live_session=live_proc is not None, max_nudges=self.max_nudges,
                              transcript_tail_tokens=self.transcript_tail_tokens)
        persisted = self.conn.execute(
            "SELECT seq, payload FROM events WHERE task_id = ? AND type = 'supervisor.acted' "
            "AND (json_extract(payload, '$.cause_seq') = ? OR "
            "(json_extract(payload, '$.cause_seq') IS NULL AND seq > ?)) "
            "ORDER BY seq DESC LIMIT 1", (task_id, cause_seq, cause_seq)
        ).fetchone()
        persisted_payload = None
        if persisted:
            payload = json.loads(persisted["payload"])
            persisted_payload = payload
            try:
                action = ACTION_MODELS[payload["action"]].model_validate(payload)
            except (KeyError, ValueError):
                persisted = None
        if not persisted:
            attempt = latest_attempt(self.conn, task_id)
            intervention_id = create_intervention(
                self.conn, task_id=task_id,
                source_attempt_id=attempt["id"] if attempt else None,
                source_candidate_sha=((attempt["candidate_sha"] or attempt["base_sha"])
                                      if attempt else None),
                source_failure_signature=(attempt["failure_signature"] if attempt else None),
            )
            if attempt:
                update_attempt(self.conn, attempt["id"],
                               supervisor_started_at=self._timestamp(),
                               disposition="supervisor_running")
            append_event(self.conn, source="supervisor", type="supervisor.invoked", task_id=task_id,
                         payload={"trigger": packet.trigger.type,
                                  "cause_seq": cause_seq,
                                  "allowed_actions": packet.allowed_actions,
                                  "attempt_id": attempt["id"] if attempt else None,
                                  "intervention_id": intervention_id})
            try:
                result = await self.supervisor(packet)
            except Exception as exc:
                # The scheduler remains deterministic when a pluggable
                # supervisor transport fails: one structured human escalation
                # is recorded, with no fabricated worker guidance.
                action = ACTION_MODELS["escalate"](
                    summary="supervisor invocation failed",
                    question=f"Review task after {packet.trigger.type}; supervisor error: {exc}",
                    options=["review manually"], recommended=None,
                    reason="supervisor transport failure", diagnosis_code="environment_tool_limit",
                )
                result = SupervisorResult(action=action, ok=False, tokens_in=None,
                                          tokens_out=None, cost_usd=None, raw_text=None)
            if attempt:
                update_attempt(self.conn, attempt["id"], supervisor_ended_at=self._timestamp())
            action = result.action
        else:
            intervention_id = (persisted_payload or {}).get("intervention_id")
            result = SupervisorResult(action=action, ok=True, tokens_in=0, tokens_out=0,
                                      cost_usd=0, raw_text=None)
        if action.action not in packet.allowed_actions:
            # Enforcement is an orchestrator responsibility (design.md section
            # 6, "an out-of-menu response is rejected"), not something to
            # trust any one supervisor implementation -- real or a test
            # double -- to have already gotten right.
            action = ACTION_MODELS["escalate"](
                summary="supervisor returned an out-of-menu action",
                question=f"Task hit {packet.trigger.type}; supervisor chose "
                        f"{action.action!r}, not in {packet.allowed_actions}",
                options=["review manually"], recommended=None,
                reason=f"{action.action!r} not in allowed_actions",
            )
            result = SupervisorResult(action=action, ok=False, tokens_in=result.tokens_in,
                                      tokens_out=result.tokens_out, cost_usd=result.cost_usd,
                                      raw_text=result.raw_text)

        if not result.ok:
            append_event(self.conn, source="supervisor", type="supervisor.failed", task_id=task_id,
                        payload={"raw_text": result.raw_text}, tokens_in=result.tokens_in,
                        tokens_out=result.tokens_out, cost_usd=result.cost_usd)
        if persisted:
            s = persisted["seq"]
        else:
            action_type = canonical_action_type(action.action)
            worker_instruction = self._worker_instruction(action)
            diagnosis_code = self._diagnosis_code(action, packet)
            s = append_event(self.conn, source="supervisor", type="supervisor.acted", task_id=task_id,
                             payload={**action.model_dump(exclude={"action"}),
                                      "action": action.action, "action_type": action_type,
                                      "cause_seq": cause_seq,
                                      "intervention_id": intervention_id,
                                      "source_attempt_id": attempt["id"] if attempt else None,
                                      "worker_instruction": worker_instruction,
                                      "diagnosis_code": diagnosis_code},
                             tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                             cost_usd=result.cost_usd)

        attempt = latest_attempt(self.conn, task_id)
        if attempt:
            feedback = self._worker_instruction(action)
            if not feedback and action.action == "escalate":
                feedback = f"{action.summary}\n{action.question}"
            update_attempt(self.conn, attempt["id"], supervisor_feedback=feedback,
                           supervisor_ended_at=self._timestamp(),
                           disposition="retry_requested" if action.action == "restart"
                           else "supervisor_acted")
        if not persisted:
            update_intervention(
                self.conn, intervention_id,
                action_type=canonical_action_type(action.action),
                diagnosis_code=self._diagnosis_code(action, packet),
                worker_instruction=self._worker_instruction(action),
                tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                cost_usd=result.cost_usd, ended_at=self._timestamp(),
            )

        if action.action == "nudge":
            live_proc.stdin.write((action.message + "\n").encode())
            await live_proc.stdin.drain()
            transition(self.conn, task_id, "running", cause_seq=s)
            return True

        if action.action == "wait":
            self._wait_grace[task_id] = min(action.seconds, self.wait_ceiling_s)
            transition(self.conn, task_id, "running", cause_seq=s)
            return True

        if action.action == "restart":
            task = dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
            await self._teardown(task_id)
            brief = task["brief"]
            fake_scenario = task.get("_fake_scenario") or task["brief"]
            recovery_feedback = self._worker_instruction(action)
            if recovery_feedback:
                brief = f"{brief}\n\nFeedback from a previous attempt:\n{recovery_feedback}"
            await self._launch({**task, "brief": brief, "_fake_scenario": fake_scenario,
                                "recovery_feedback": recovery_feedback,
                                "_intervention_id": intervention_id},
                               retries=task["retries"] + 1)
            return False

        if action.action == "escalate":
            if attempt:
                update_attempt(self.conn, attempt["id"], disposition="escalated")
            self._finish_interventions(task_id, "needs_human", "cannot_yet_evaluate")
            transition(self.conn, task_id, "needs_human", cause_seq=s)
            await self._teardown(task_id)
            return False

        # abandon: allowed_actions only ever offers it in yolo mode.
        if attempt:
            update_attempt(self.conn, attempt["id"], disposition="abandoned")
        self._finish_interventions(task_id, "failed", "cannot_yet_evaluate")
        transition(self.conn, task_id, "failed", cause_seq=s)
        await self._teardown(task_id)
        return False

    # -- verifying ------------------------------------------------------------

    @staticmethod
    def _event_attempt_matches(event, attempt_id: str | None) -> bool:
        if not event:
            return False
        try:
            payload = json.loads(event["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        recorded = payload.get("attempt_id")
        # Older event records predate attempt_id in a few terminal payloads;
        # state recovery may safely use their latest task-scoped fact.
        return recorded in (None, attempt_id)

    def _latest_attempt_event(self, task_id: str, attempt_id: str | None, types: tuple[str, ...]):
        placeholders = ",".join("?" * len(types))
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE task_id = ? AND type IN ({placeholders}) "
            "ORDER BY seq DESC", (task_id, *types),
        ).fetchall()
        return next((row for row in rows if self._event_attempt_matches(row, attempt_id)), None)

    async def _resume_verifying(self, task_id: str) -> None:
        task = self.conn.execute(
            "SELECT current_attempt_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        attempt_id = task["current_attempt_id"] if task else None
        passed = self._latest_attempt_event(task_id, attempt_id, ("verify.passed",))
        failed = self._latest_attempt_event(task_id, attempt_id, ("verify.failed",))
        terminal = max((event for event in (passed, failed) if event),
                       key=lambda event: event["seq"], default=None)
        if terminal is None:
            await self._run_verify(task_id)
            return
        if terminal["type"] == "verify.passed":
            transition(self.conn, task_id, "delivering", cause_seq=passed["seq"])
            await self._resume_delivering(task_id)
            return
        if self.conn.execute(
            "SELECT state FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["state"] != "triage":
            transition(self.conn, task_id, "triage", cause_seq=failed["seq"])
        await self._handle_triage(task_id, failed["seq"], live_proc=None)

    async def _resume_delivering(self, task_id: str) -> None:
        task = self.conn.execute(
            "SELECT current_attempt_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        attempt_id = task["current_attempt_id"] if task else None
        delivered = self._latest_attempt_event(
            task_id, attempt_id,
            ("delivery.pr_opened", "delivery.merged_local", "delivery.report_written"),
        )
        if delivered:
            transition(self.conn, task_id, "delivered", cause_seq=delivered["seq"])
            if attempt_id:
                update_attempt(self.conn, attempt_id, disposition="delivered")
            self._finish_interventions(task_id, "delivered", "improved")
            self._record_verification_recovery(task_id)
            return
        failed = self._latest_attempt_event(
            task_id, attempt_id,
            ("delivery.failed", "artifact.validation_failed"),
        )
        if failed:
            if self.conn.execute(
                "SELECT state FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()["state"] != "triage":
                transition(self.conn, task_id, "triage", cause_seq=failed["seq"])
            await self._handle_triage(task_id, failed["seq"], live_proc=None)
            return
        await self._deliver(task_id)

    def _enter_verifying(self, task_id: str, cause_seq: int, *, candidate_sha=None) -> None:
        fields = {}
        if candidate_sha:
            fields["candidate_sha"] = candidate_sha
        transition(self.conn, task_id, "verifying", cause_seq=cause_seq, **fields)

    async def _run_verify(self, task_id: str) -> None:
        task = dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        origin = self.conn.execute(
            "SELECT base_sha FROM attempts WHERE task_id = ? ORDER BY attempt_no LIMIT 1",
            (task_id,),
        ).fetchone()
        verification_base_sha = origin["base_sha"] if origin else task["base_sha"]
        attempt = self.conn.execute("SELECT * FROM attempts WHERE id = ?", (task["current_attempt_id"],)).fetchone()
        candidate_sha = task["candidate_sha"] or (attempt["candidate_sha"] if attempt else None)
        no_change = bool(self.conn.execute(
            "SELECT 1 FROM events WHERE task_id = ? AND type = 'worker.no_change' "
            "AND json_extract(payload, '$.attempt_id') = ? ORDER BY seq DESC LIMIT 1",
            (task_id, task["current_attempt_id"]),
        ).fetchone())
        req = VerifyRequest(task_id=task_id, worktree=task["worktree"], base_sha=verification_base_sha,
                            verify_cmd=task["verify_cmd"] or "true", timeout_s=self.verify_timeout_s,
                            repo=task["repo"],
                            candidate_sha=candidate_sha,
                            worker_dirty=attempt["worker_dirty"] if attempt else None,
                            artifact_root=(str(self.artifact_root / task_id)
                                           if self.artifact_root else None))
        # Keep VerifyRequest's public schema stable; no-change is an internal
        # protocol fact, not an evaluator input.
        req.allow_empty_diff = no_change
        terminal = self.conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND type IN "
            "('worker.done_claimed', 'worker.no_change') "
            "ORDER BY seq DESC LIMIT 1", (task_id,),
        ).fetchone()
        protocol_result = None
        if terminal:
            try:
                protocol_result = json.loads(terminal["payload"]).get("protocol_result")
            except (TypeError, json.JSONDecodeError):
                protocol_result = None
        req.protocol_result = protocol_result
        req.output_artifacts = task.get("output_artifacts")
        req.output_schema = task.get("output_schema")
        req.targeted_commands = [task["node_verify_cmd"]] if task.get("node_verify_cmd") else None
        attempt_id = task["current_attempt_id"]
        update_attempt(self.conn, attempt_id, verification_started_at=self._timestamp(),
                       disposition="verifying")
        append_event(self.conn, source="verifier", type="verify.started", task_id=task_id,
                     payload={"attempt_id": attempt_id})
        # The gate owns subprocesses and filesystem work but has no scheduler
        # state access. Running it in a thread keeps the asyncio control loop
        # able to launch unrelated workers while verification is in progress.
        verify_kwargs = {}
        if self.evidence_ladder:
            verify_kwargs = {
                "evidence_ladder": True,
                "protocol_result": protocol_result,
                "artifact_specs": task.get("output_artifacts"),
                "output_schema": task.get("output_schema"),
                "targeted_commands": req.targeted_commands,
                "allow_empty_diff": no_change,
            }
        result = await asyncio.to_thread(run_verify, req, **verify_kwargs)
        if result.evidence:
            for stage in result.evidence.get("stages", []):
                append_event(
                    self.conn, source="verifier", type="verify.evidence_stage",
                    task_id=task_id, payload={"attempt_id": attempt_id, **stage},
                )
            append_event(
                self.conn, source="verifier", type="verify.evidence_completed",
                task_id=task_id, payload={"attempt_id": attempt_id,
                                          "passed": result.passed,
                                          "decisive_stage": result.evidence.get("decisive_stage"),
                                          "cause": result.cause},
            )
        payload = {"cause": result.cause, "exit_code": result.exit_code,
                  "duration_s": result.duration_s, "flaky": result.flaky,
                  "output_tail": result.output_tail, "diff_stat": result.diff_stat,
                  "tests_modified": result.tests_modified,
                  "output_path": result.output_path, "patch_path": result.patch_path,
                  "failure_signature": result.failure_signature,
                  "attempt_id": attempt_id}
        update_attempt(self.conn, attempt_id, verification_ended_at=self._timestamp(),
                       candidate_sha=candidate_sha,
                       failure_cause=None if result.passed else result.cause,
                       failure_signature=result.failure_signature,
                       disposition="verification_passed" if result.passed else "verification_failed")
        attempt = self.conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        self._update_child_interventions(task_id, attempt, passed=result.passed)
        if result.passed:
            s = append_event(self.conn, source="verifier", type="verify.passed",
                             task_id=task_id, payload=payload)
            self._append_terminal_classification(task_id, "completed", cause_seq=s,
                                                 attempt_id=attempt_id)
            if attempt and attempt["attempt_no"] > 1:
                append_event(self.conn, source="system", type="recovery.verified", task_id=task_id,
                             payload={"attempt_id": attempt_id, "cause_seq": s,
                                      "outcome": "verified"})
            transition(self.conn, task_id, "delivering", cause_seq=s)
            await self._deliver(task_id)
        else:
            s = append_event(self.conn, source="verifier", type="verify.failed",
                             task_id=task_id, payload=payload)
            if attempt and attempt["attempt_no"] > 1:
                append_event(self.conn, source="system", type="recovery.failed", task_id=task_id,
                             payload={"attempt_id": attempt_id, "cause_seq": s,
                                      "outcome": "verification_failed"})
            await self._handle_triage(task_id, s, live_proc=None)

    # -- delivering -------------------------------------------------------------

    async def _deliver(self, task_id: str) -> None:
        task = dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        artifact_ok, artifact_detail = validate_output_artifacts(task)
        artifact_seq = append_event(
            self.conn, source="verifier",
            type="artifact.validation_passed" if artifact_ok else "artifact.validation_failed",
            task_id=task_id, payload={"attempt_id": task.get("current_attempt_id"), **artifact_detail},
        )
        if not artifact_ok:
            attempt = latest_attempt(self.conn, task_id)
            if attempt:
                update_attempt(self.conn, attempt["id"], failure_cause="artifact_validation_failed",
                               failure_signature="artifact_validation_failed", disposition="artifact_failed")
            await self._handle_triage(task_id, artifact_seq, live_proc=None)
            return
        try:
            etype, payload = delivery.deliver(
                task,
                artifact_root=(str(self.artifact_root / task_id)
                               if self.artifact_root else None),
            )
        except delivery.DeliveryError as e:
            s = append_event(self.conn, source="delivery", type="delivery.failed",
                             task_id=task_id, payload={"attempt_id": task.get("current_attempt_id"),
                                                       "error": str(e)})
            await self._handle_triage(task_id, s, live_proc=None)
            return
        s = append_event(
            self.conn, source="delivery", type=etype, task_id=task_id,
            payload={**payload, "attempt_id": task.get("current_attempt_id")},
        )
        transition(self.conn, task_id, "delivered", cause_seq=s)
        task = self.conn.execute(
            "SELECT current_attempt_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task and task["current_attempt_id"]:
            update_attempt(self.conn, task["current_attempt_id"], disposition="delivered")
        self._finish_interventions(task_id, "delivered", "improved")
        self._record_verification_recovery(task_id)

    def _record_verification_recovery(self, task_id: str) -> None:
        """Record only verification-driven descendant recovery. A later
        delivered descendant counts iff an ancestor has a recorded verify
        failure, and the event is idempotent per task.
        """
        if self.conn.execute(
            "SELECT 1 FROM events WHERE task_id = ? AND type = 'verification.recovered'",
            (task_id,),
        ).fetchone():
            return
        current = latest_attempt(self.conn, task_id)
        if not current or current["attempt_no"] <= 1:
            return
        failed = self.conn.execute(
            "SELECT id FROM attempts WHERE task_id = ? AND attempt_no < ? "
            "AND EXISTS (SELECT 1 FROM events e WHERE e.task_id = attempts.task_id "
            "AND e.type = 'verify.failed' "
            "AND json_extract(e.payload, '$.attempt_id') = attempts.id) "
            "ORDER BY attempt_no LIMIT 1",
            (task_id, current["attempt_no"]),
        ).fetchone()
        if failed:
            append_event(self.conn, source="system", type="verification.recovered", task_id=task_id,
                         payload={"failed_attempt_id": failed["id"],
                                  "recovered_attempt_id": current["id"]})

    # -- watchdog: stall is never self-reported --------------------------------

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self.watchdog_interval_s)
            now = time.monotonic()
            for task_id, last in list(self._last_event_ts.items()):
                threshold = self._wait_grace.get(task_id, self.stall_threshold_s)
                if now - last < threshold:
                    continue
                proc = self._procs.get(task_id)
                s = self._mark_running_failure(
                    task_id, source="watchdog", event_type="worker.stalled",
                    payload={"silent_for_s": round(now - last, 1)})
                if s is None:
                    continue
                self._append_terminal_classification(task_id, "timeout", cause_seq=s)
                self._wait_grace.pop(task_id, None)
                keep_watching = await self._handle_triage(task_id, s, live_proc=proc)
                if keep_watching:
                    self._last_event_ts[task_id] = time.monotonic()
                else:
                    await self._teardown(task_id, expect_proc=proc)

    # -- teardown ---------------------------------------------------------------

    async def _watch_process_exit(self, task_id: str, proc) -> None:
        await proc.wait()
        await self._reap_process(task_id, proc)

    async def _reap_process(self, task_id: str, proc) -> None:
        async with self._reap_locks[task_id]:
            await _terminate_and_reap(proc, terminate=task_id not in self._reaped_tasks)
            self._reaped_tasks.add(task_id)

    async def _teardown(self, task_id: str, *, expect_proc=None) -> None:
        """Idempotent, and safe to call speculatively: if `expect_proc` is
        given and no longer matches what's tracked for task_id, a restart
        already replaced this attempt with a fresh one -- do nothing rather
        than tearing down the new attempt out from under it.

        The cleanup itself runs in a separate task and is shielded from
        cancellation.  A watchdog can be the task performing teardown when
        the scheduler begins shutdown; cancelling that watchdog must not
        strand a live process after its registry entry has been removed.
        """
        if expect_proc is not None and self._procs.get(task_id) is not expect_proc:
            return

        cleanup = self._teardown_tasks.get(task_id)
        if cleanup is None:
            owner = asyncio.current_task()
            cleanup = asyncio.create_task(
                self._teardown_owned(task_id, expect_proc=expect_proc, owner=owner)
            )
            self._teardown_tasks[task_id] = cleanup
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # The caller may be the watchdog being cancelled by
            # run_until_settled's finally block.  Finish the authoritative
            # cleanup before propagating cancellation to that caller.
            try:
                await asyncio.shield(cleanup)
            except BaseException as exc:
                self._record_teardown_failure(task_id, cleanup, exc)
                raise
            raise
        except BaseException as exc:
            self._record_teardown_failure(task_id, cleanup, exc)
            raise
        finally:
            if cleanup.done() and self._teardown_tasks.get(task_id) is cleanup:
                self._teardown_tasks.pop(task_id, None)

    async def _teardown_owned(self, task_id: str, *, expect_proc=None, owner=None) -> None:
        """Perform the single process/slot/worktree teardown operation."""
        if expect_proc is not None and self._procs.get(task_id) is not expect_proc:
            return

        failures: list[tuple[str, BaseException]] = []
        proc = self._procs.pop(task_id, None)
        exit_watcher = self._exit_watchers.pop(task_id, None)
        watcher = self._watchers.pop(task_id, None)
        self._last_event_ts.pop(task_id, None)
        self._wait_grace.pop(task_id, None)

        if exit_watcher is not None and exit_watcher not in (asyncio.current_task(), owner):
            exit_watcher.cancel()
            results = await asyncio.gather(exit_watcher, return_exceptions=True)
            failures.extend(
                ("exit watcher", result) for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            )

        if proc is not None:
            try:
                await self._reap_process(task_id, proc)
            except Exception as exc:
                failures.append(("process reap", exc))
                if getattr(proc, "returncode", None) is None:
                    try:
                        await _terminate_and_reap(proc, terminate=True)
                    except Exception as retry_exc:
                        failures.append(("process reap retry", retry_exc))
            finally:
                self._reaped_tasks.discard(task_id)
                self._reap_locks.pop(task_id, None)
            # asyncio doesn't close the subprocess transport just because the
            # process exited; leaving it for GC risks it firing after the
            # loop closes ("Exception ignored in: ...__del__ ... Event loop
            # is closed"). Harmless but noisy -- close it explicitly.
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                try:
                    transport.close()
                except Exception as exc:
                    failures.append(("process transport close", exc))

        if watcher is not None and watcher not in (asyncio.current_task(), owner):
            watcher.cancel()

        wt = self._worktrees.pop(task_id, None)
        slot = self._worker_slots.pop(task_id, None)
        lease = self._leases.pop(task_id, None)
        self._lease_heartbeat.pop(task_id, None)
        if lease is not None:
            try:
                execution_lease.release(
                    self.conn, lease, reason="scheduler_teardown", source="scheduler",
                )
            except execution_lease.StaleLeaseError:
                # A watchdog/reconciler may already have fenced it.  The
                # durable recovery event is the authoritative outcome.
                pass
            except BaseException as exc:
                failures.append(("execution lease release", exc))
        if wt is not None:
            # Attempt refs are durable candidates; only the pooled checkout
            # is disposable.
            released = False
            try:
                self._pool.release(wt, preserve_branch=True)
                released = True
            except Exception as exc:
                failures.append(("worktree release", exc))
            if slot is not None and released:
                try:
                    attempt = latest_attempt(self.conn, task_id)
                    self._append_timing_event(
                        task_id, "worker.slot_released",
                        attempt_id=attempt["id"] if attempt else None,
                        payload={"slot": str(slot), "occupancy": len(self._worker_slots),
                                 "limit": self.max_concurrency},
                    )
                except Exception as exc:
                    failures.append(("slot release event", exc))
        elif slot is not None:
            # A lease without a checkout is a scheduler bug. Refuse to hide a
            # negative/double-release accounting error behind a counter update,
            # but continue cleaning the other resources before surfacing it.
            failures.append((
                "slot release",
                RuntimeError(f"worker slot {task_id} has no worktree to release"),
            ))

        if failures:
            causes = [
                SchedulerCleanupFailure(f"teardown {task_id} / {resource}", cause)
                for resource, cause in failures
            ]
            if len(causes) == 1:
                raise causes[0]
            raise BaseExceptionGroup(f"teardown for task {task_id} failed", causes)
