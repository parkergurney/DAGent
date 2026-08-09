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
import time
from pathlib import Path

from orchestrator import delivery
from orchestrator.scheduler.reconcile import reconcile
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
    WorktreePool, build_execution_contract, cleanup_worker_sandbox, spawn_fake_worker,
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


def validate_dependency_graph(conn) -> None:
    """Fail closed for missing prerequisites and cyclic task graphs.

    Task creation normally enforces missing references through SQLite foreign
    keys and benchmark suites require dependencies to point backward. This
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
                cause = append_event(
                    conn, source="scheduler", type="dep.satisfied", task_id=task_id,
                    payload={"run_id": run_id},
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
                repeated_failure_threshold=1):
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
        self._worktrees: dict[str, object] = {}
        self._last_event_ts: dict[str, float] = {}
        self._wait_grace: dict[str, float] = {}  # task_id -> seconds, set by a "wait" decision
        self._infrastructure_failure: WorkerStartupFailure | None = None

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
        # strand the task. The candidate ref is durable and verification is
        # safe to rerun; the verifier itself is idempotent from the scheduler
        # perspective because the resulting events are append-only evidence.
        for row in self.conn.execute("SELECT id FROM tasks WHERE state = 'verifying'").fetchall():
            await self._run_verify(row["id"])

        watchdog = asyncio.create_task(self._watchdog_loop())
        try:
            while forever or not self._team_settled():
                if self._infrastructure_failure:
                    raise self._infrastructure_failure
                self._advance_deps(block_needs_human=not forever)
                await self._launch_ready()
                if self._infrastructure_failure:
                    raise self._infrastructure_failure
                await asyncio.sleep(poll_interval_s if (forever and self._team_settled()) else 0.05)
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            await asyncio.gather(*(self._teardown(tid) for tid in list(self._worker_slots)),
                                 return_exceptions=True)
            self._pool.close()

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

    async def _launch_ready(self) -> None:
        while len(self._worker_slots) < self.max_concurrency:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE state = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return
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
        # It intentionally has no hidden verifier fields.
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
        wt, base_sha = await self._pool.acquire(
            task_id, base_branch=self.base_branch, base_sha=starting_sha, branch=branch,
        )
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
        proc = await self.spawn_worker(worker_task, wt, model=self.worker_model)
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
        self._watchers[task_id] = asyncio.create_task(self._watch(task_id, proc))
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

    async def _watch(self, task_id: str, proc: asyncio.subprocess.Process) -> None:
        claimed_or_triaged = False
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                self._last_event_ts[task_id] = time.monotonic()
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype, payload = rec.get("type"), rec.get("payload", {})
                # ResultMessage is the canonical aggregate for a worker
                # session. AssistantMessage usage is retained in its payload
                # for diagnostics, but is not put in event accounting columns;
                # otherwise a session is double-counted.
                usage_kwargs = {}
                if etype == "result":
                    usage_kwargs = dict(tokens_in=payload.get("tokens_in"),
                                        tokens_out=payload.get("tokens_out"),
                                        cost_usd=payload.get("cost_usd"))
                attempt = latest_attempt(self.conn, task_id)
                event_payload = {**payload, "attempt_id": attempt["id"] if attempt else None}

                if etype == "done_claimed":
                    s = append_event(self.conn, source="worker", type="worker.done_claimed",
                                     task_id=task_id, session_id=str(proc.pid), payload=event_payload,
                                     **usage_kwargs)
                    claimed_or_triaged = True
                    # A done claim is a stream message, not proof that the
                    # SDK/Claude process has exited.  Reap it before the
                    # verifier is allowed to materialize hidden files.
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
                                     **usage_kwargs)
                    keep_watching = await self._handle_triage(task_id, s, live_proc=proc)
                    if keep_watching:
                        self._last_event_ts[task_id] = time.monotonic()
                        continue  # a nudge landed on proc.stdin; keep reading this same session
                    claimed_or_triaged = True
                    break
                elif etype == "startup_failed":
                    category = str(payload.get("category") or "other_infrastructure_startup_failure")
                    reason = str(payload.get("reason") or payload.get("error") or category)[:500]
                    s = append_event(self.conn, source="worker", type="worker.startup_failed",
                                     task_id=task_id, session_id=str(proc.pid),
                                     payload={**event_payload, "category": category,
                                              "reason": reason})
                    claimed_or_triaged = True
                    self._capture_candidate(task_id, disposition="startup_failed",
                                            failure_cause=category)
                    self._infrastructure_failure = WorkerStartupFailure(task_id, category, reason)
                    # This is an infrastructure abort, not a worker/task
                    # failure. It must never enter the supervisor policy.
                    await self._teardown(task_id, expect_proc=proc)
                    break
                else:
                    append_event(self.conn, source="worker", type=f"worker.{etype}",
                                task_id=task_id, session_id=str(proc.pid), payload=event_payload,
                                **usage_kwargs)

            if not claimed_or_triaged:
                code = await proc.wait()
                s = self._mark_running_failure(task_id, source="worker", event_type="worker.exited",
                                               payload={"exit_code": code}, session_id=str(proc.pid))
                if s is not None:
                    candidate = self._capture_candidate(task_id, disposition="worker_failed",
                                                        failure_cause="worker.exited")
                    # No live worker remains. Release capacity before any
                    # supervisor await so independent queued work can launch.
                    await self._teardown(task_id, expect_proc=proc)
                    await self._handle_triage(task_id, s, live_proc=None,
                                              candidate_sha=candidate)
        finally:
            await self._teardown(task_id, expect_proc=proc)

    def _mark_running_failure(self, task_id: str, *, source, event_type, payload=None,
                              session_id=None) -> int | None:
        """Append a failure event iff the task is still 'running' -- a no-op
        guard, not a lock: nothing here awaits between the read and the
        write, so no other coroutine can interleave. Returns the new event's
        seq, or None if something else already moved the task on."""
        row = self.conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["state"] != "running":
            return None
        return append_event(self.conn, source=source, type=event_type, task_id=task_id,
                            payload=payload or {}, session_id=session_id)

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
        during partial/crashed writes without consulting hidden verification.
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
        trigger = self.conn.execute("SELECT payload FROM events WHERE seq = ?", (cause_seq,)).fetchone()
        trigger_payload = json.loads(trigger["payload"]) if trigger else {}
        cause = trigger_payload.get("cause")

        # Hidden/evaluator-only failures have no public diagnosis by contract.
        # Escalate with an honest category instead of asking a model to invent
        # an explanation from material it is not allowed to see.
        if cause == "hidden_tests_failed":
            policy_seq = append_event(
                self.conn, source="system", type="recovery.policy_applied", task_id=task_id,
                payload={"action_type": "ESCALATE_HUMAN", "diagnosis_code": "opaque_evaluator_mismatch",
                         "attempt_id": attempt["id"] if attempt else None,
                         "reason": "external evaluator supplied no actionable public information"},
            )
            if attempt:
                update_attempt(self.conn, attempt["id"], disposition="opaque_evaluator_mismatch")
            self._finish_interventions(task_id, "needs_human", "cannot_yet_evaluate")
            transition(self.conn, task_id, "needs_human", cause_seq=policy_seq)
            await self._teardown(task_id)
            return True

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

    async def _handle_triage(self, task_id: str, cause_seq: int, *, live_proc,
                             candidate_sha=None) -> bool:
        row = self.conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
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
            "AND seq > ? ORDER BY seq DESC LIMIT 1", (task_id, cause_seq)
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
        req_kwargs = {}
        if task["protected_paths"]:
            req_kwargs["protected_paths"] = tuple(json.loads(task["protected_paths"]))
        attempt = self.conn.execute("SELECT * FROM attempts WHERE id = ?", (task["current_attempt_id"],)).fetchone()
        candidate_sha = task["candidate_sha"] or (attempt["candidate_sha"] if attempt else None)
        req = VerifyRequest(task_id=task_id, worktree=task["worktree"], base_sha=verification_base_sha,
                            verify_cmd=task["verify_cmd"] or "true", setup_cmd=task["setup_cmd"],
                            hidden_cmd=task["hidden_cmd"], timeout_s=self.verify_timeout_s,
                            repo=task["repo"],
                            candidate_sha=candidate_sha,
                            worker_dirty=attempt["worker_dirty"] if attempt else None,
                            artifact_root=(str(self.artifact_root / task_id)
                                           if self.artifact_root else None),
                            **req_kwargs)
        attempt_id = task["current_attempt_id"]
        update_attempt(self.conn, attempt_id, verification_started_at=self._timestamp(),
                       disposition="verifying")
        append_event(self.conn, source="verifier", type="verify.started", task_id=task_id,
                     payload={"attempt_id": attempt_id})
        # The gate owns subprocesses and filesystem work but has no scheduler
        # state access. Running it in a thread keeps the asyncio control loop
        # able to launch unrelated workers while verification is in progress.
        result = await asyncio.to_thread(run_verify, req)
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
            transition(self.conn, task_id, "delivering", cause_seq=s)
            await self._deliver(task_id)
        else:
            s = append_event(self.conn, source="verifier", type="verify.failed",
                             task_id=task_id, payload=payload)
            await self._handle_triage(task_id, s, live_proc=None)

    # -- delivering -------------------------------------------------------------

    async def _deliver(self, task_id: str) -> None:
        task = dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        try:
            etype, payload = delivery.deliver(
                task,
                artifact_root=(str(self.artifact_root / task_id)
                               if self.artifact_root else None),
            )
        except delivery.DeliveryError as e:
            s = append_event(self.conn, source="delivery", type="delivery.failed",
                             task_id=task_id, payload={"error": str(e)})
            await self._handle_triage(task_id, s, live_proc=None)
            return
        s = append_event(self.conn, source="delivery", type=etype, task_id=task_id, payload=payload)
        transition(self.conn, task_id, "delivered", cause_seq=s)
        task = self.conn.execute(
            "SELECT current_attempt_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task and task["current_attempt_id"]:
            update_attempt(self.conn, task["current_attempt_id"], disposition="delivered")
        self._finish_interventions(task_id, "delivered", "improved")
        self._record_verification_recovery(task_id)

    def _record_verification_recovery(self, task_id: str) -> None:
        """Record only verification-driven descendant recovery.

        This deliberately does not inspect ``bench.fault_injected``. A later
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
        than tearing down the new attempt out from under it."""
        if expect_proc is not None and self._procs.get(task_id) is not expect_proc:
            return

        proc = self._procs.pop(task_id, None)
        exit_watcher = self._exit_watchers.pop(task_id, None)
        watcher = self._watchers.pop(task_id, None)
        self._last_event_ts.pop(task_id, None)
        self._wait_grace.pop(task_id, None)

        if exit_watcher is not None and exit_watcher is not asyncio.current_task():
            exit_watcher.cancel()
            await asyncio.gather(exit_watcher, return_exceptions=True)

        if proc is not None:
            await self._reap_process(task_id, proc)
            self._reaped_tasks.discard(task_id)
            self._reap_locks.pop(task_id, None)
            # asyncio doesn't close the subprocess transport just because the
            # process exited; leaving it for GC risks it firing after the
            # loop closes ("Exception ignored in: ...__del__ ... Event loop
            # is closed"). Harmless but noisy -- close it explicitly.
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                transport.close()

        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()

        wt = self._worktrees.pop(task_id, None)
        if wt is not None:
            # Attempt refs are durable candidates; only the pooled checkout
            # is disposable.
            self._pool.release(wt, preserve_branch=True)
            slot = self._worker_slots.pop(task_id, None)
            if slot is not None:
                attempt = latest_attempt(self.conn, task_id)
                self._append_timing_event(
                    task_id, "worker.slot_released",
                    attempt_id=attempt["id"] if attempt else None,
                    payload={"slot": str(slot), "occupancy": len(self._worker_slots),
                             "limit": self.max_concurrency},
                )
        elif task_id in self._worker_slots:
            # A lease without a checkout is a scheduler bug. Refuse to hide a
            # negative/double-release accounting error behind a counter update.
            raise RuntimeError(f"worker slot {task_id} has no worktree to release")
        cleanup_worker_sandbox(proc)
