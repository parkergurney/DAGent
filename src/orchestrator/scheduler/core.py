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
import json
import os
import signal
import time

from orchestrator import delivery
from orchestrator.scheduler.reconcile import reconcile
from orchestrator.store import append_event, transition
from orchestrator.supervisor import ACTION_MODELS, always_escalate, build_packet
from orchestrator.supervisor.llm import SupervisorResult
from orchestrator.verify.gate import VerifyRequest, run_verify
from orchestrator.worker import WorktreePool, spawn_fake_worker

# Team states in which nothing is left for the scheduler to drive; the team
# is "settled" once every task sits in one of these.
_SETTLED_STATES = ("needs_human", "delivered", "failed", "cancelled")


class Scheduler:
    def __init__(self, conn, repo_root, worktree_root, *,
                max_concurrency=4, stall_threshold_s=300, watchdog_interval_s=5,
                verify_timeout_s=600, spawn_worker=spawn_fake_worker, worker_model=None,
                supervisor=always_escalate,
                max_nudges=2, wait_ceiling_s=1800, transcript_tail_tokens=3000, yolo=False):
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

        # Pool size == max_concurrency: never a reason for more slots than
        # tasks that can be running at once (design.md section 8 / M5).
        self._pool = WorktreePool(repo_root, worktree_root, max_concurrency)
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._watchers: dict[str, asyncio.Task] = {}
        self._worktrees: dict[str, object] = {}
        self._last_event_ts: dict[str, float] = {}
        self._wait_grace: dict[str, float] = {}  # task_id -> seconds, set by a "wait" decision

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
        self._pool.open()
        for row in self.conn.execute("SELECT id FROM tasks WHERE state = 'triage'").fetchall():
            sc = self.conn.execute(
                "SELECT payload FROM events WHERE task_id = ? AND type = 'task.state_changed' "
                "ORDER BY seq DESC LIMIT 1", (row["id"],)).fetchone()
            trigger_seq = json.loads(sc["payload"])["cause_seq"]
            await self._handle_triage(row["id"], trigger_seq, live_proc=None)

        watchdog = asyncio.create_task(self._watchdog_loop())
        try:
            while forever or not self._team_settled():
                self._advance_deps()
                await self._launch_ready()
                await asyncio.sleep(poll_interval_s if (forever and self._team_settled()) else 0.05)
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            await asyncio.gather(*(self._teardown(tid) for tid in list(self._procs)),
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

    def _advance_deps(self) -> None:
        blocked = self.conn.execute("SELECT id FROM tasks WHERE state = 'blocked'").fetchall()
        for row in blocked:
            deps = [r["depends_on"] for r in self.conn.execute(
                "SELECT depends_on FROM task_deps WHERE task_id = ?", (row["id"],))]
            if not deps:
                s = append_event(self.conn, source="scheduler", type="dep.satisfied", task_id=row["id"])
                transition(self.conn, row["id"], "queued", cause_seq=s)
                continue
            states = [self.conn.execute("SELECT state FROM tasks WHERE id = ?", (d,)).fetchone()["state"]
                     for d in deps]
            if any(s in ("failed", "cancelled") for s in states):
                s = append_event(self.conn, source="scheduler", type="dep.failed", task_id=row["id"])
                transition(self.conn, row["id"], "cancelled", cause_seq=s)
            elif all(s == "delivered" for s in states):
                s = append_event(self.conn, source="scheduler", type="dep.satisfied", task_id=row["id"])
                transition(self.conn, row["id"], "queued", cause_seq=s)

    # -- queued -> running ----------------------------------------------------

    async def _launch_ready(self) -> None:
        while len(self._procs) < self.max_concurrency:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE state = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return
            await self._launch(dict(row))

    async def _launch(self, task: dict, *, retries: int | None = None) -> None:
        """(queued|triage) -> running. `retries`, when given, is a restart's
        new count -- transition() folds it into the same state-change event
        as session_id/worktree/base_sha, no separate write."""
        wt, base_sha = await self._pool.acquire(task["id"])
        proc = await self.spawn_worker(task, wt, model=self.worker_model)
        session_id = str(proc.pid)

        s = append_event(self.conn, source="scheduler", type="worker.spawned",
                         task_id=task["id"], session_id=session_id)
        fields = dict(session_id=session_id, worktree=str(wt), base_sha=base_sha)
        if retries is not None:
            fields["retries"] = retries
        transition(self.conn, task["id"], "running", cause_seq=s, **fields)

        self._procs[task["id"]] = proc
        self._worktrees[task["id"]] = wt
        self._last_event_ts[task["id"]] = time.monotonic()
        self._watchers[task["id"]] = asyncio.create_task(self._watch(task["id"], proc))

    # -- running: read the worker's event stream -----------------------------

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
                # tokens/cost ride on the payload (design.md section 5) but also
                # get their own event columns -- FakeWorker payloads simply
                # lack these keys, a no-op for the fault-injection suite, and
                # only light up for real Agent SDK workers (M3).
                usage_kwargs = dict(tokens_in=payload.get("tokens_in"),
                                    tokens_out=payload.get("tokens_out"),
                                    cost_usd=payload.get("cost_usd"))

                if etype == "done_claimed":
                    s = append_event(self.conn, source="worker", type="worker.done_claimed",
                                     task_id=task_id, session_id=str(proc.pid), payload=payload,
                                     **usage_kwargs)
                    claimed_or_triaged = True
                    await self._enter_verifying(task_id, s)
                    break
                elif etype == "asked":
                    s = append_event(self.conn, source="worker", type="worker.asked",
                                     task_id=task_id, session_id=str(proc.pid), payload=payload,
                                     **usage_kwargs)
                    keep_watching = await self._handle_triage(task_id, s, live_proc=proc)
                    if keep_watching:
                        self._last_event_ts[task_id] = time.monotonic()
                        continue  # a nudge landed on proc.stdin; keep reading this same session
                    claimed_or_triaged = True
                    break
                else:
                    append_event(self.conn, source="worker", type=f"worker.{etype}",
                                task_id=task_id, session_id=str(proc.pid), payload=payload,
                                **usage_kwargs)

            if not claimed_or_triaged:
                code = await proc.wait()
                s = self._mark_running_failure(task_id, source="worker", event_type="worker.exited",
                                               payload={"exit_code": code}, session_id=str(proc.pid))
                if s is not None:
                    await self._handle_triage(task_id, s, live_proc=None)
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

    async def _handle_triage(self, task_id: str, cause_seq: int, *, live_proc) -> bool:
        """Returns True iff the caller should keep watching `live_proc` (only
        possible for nudge/wait); False means this attempt is over -- either
        genuinely finished (escalate/abandon) or replaced by a freshly
        launched attempt the caller no longer owns (restart)."""
        row = self.conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row["state"] != "triage":
            transition(self.conn, task_id, "triage", cause_seq=cause_seq)

        packet = build_packet(self.conn, task_id, cause_seq, yolo=self.yolo,
                              live_session=live_proc is not None, max_nudges=self.max_nudges,
                              transcript_tail_tokens=self.transcript_tail_tokens)
        append_event(self.conn, source="supervisor", type="supervisor.invoked", task_id=task_id,
                    payload={"trigger": packet.trigger.type, "allowed_actions": packet.allowed_actions})

        result = await self.supervisor(packet)
        action = result.action
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
        s = append_event(self.conn, source="supervisor", type="supervisor.acted", task_id=task_id,
                         payload={"action": action.action, **action.model_dump(exclude={"action"})},
                         tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                         cost_usd=result.cost_usd)

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
            if action.feedback:
                brief = f"{brief}\n\nFeedback from a previous attempt:\n{action.feedback}"
            await self._launch({**task, "brief": brief}, retries=task["retries"] + 1)
            return False

        if action.action == "escalate":
            transition(self.conn, task_id, "needs_human", cause_seq=s)
            await self._teardown(task_id)
            return False

        # abandon: allowed_actions only ever offers it in yolo mode.
        transition(self.conn, task_id, "failed", cause_seq=s)
        await self._teardown(task_id)
        return False

    # -- verifying ------------------------------------------------------------

    async def _enter_verifying(self, task_id: str, cause_seq: int) -> None:
        transition(self.conn, task_id, "verifying", cause_seq=cause_seq)
        await self._run_verify(task_id)

    async def _run_verify(self, task_id: str) -> None:
        task = dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        req_kwargs = {}
        if task["protected_paths"]:
            req_kwargs["protected_paths"] = tuple(json.loads(task["protected_paths"]))
        req = VerifyRequest(task_id=task_id, worktree=task["worktree"], base_sha=task["base_sha"],
                            verify_cmd=task["verify_cmd"] or "true", setup_cmd=task["setup_cmd"],
                            timeout_s=self.verify_timeout_s, **req_kwargs)
        append_event(self.conn, source="verifier", type="verify.started", task_id=task_id)
        result = run_verify(req)
        payload = {"cause": result.cause, "exit_code": result.exit_code,
                  "duration_s": result.duration_s, "flaky": result.flaky,
                  "output_tail": result.output_tail}
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
            etype, payload = delivery.deliver(task)
        except delivery.DeliveryError as e:
            s = append_event(self.conn, source="delivery", type="delivery.failed",
                             task_id=task_id, payload={"error": str(e)})
            await self._handle_triage(task_id, s, live_proc=None)
            return
        s = append_event(self.conn, source="delivery", type=etype, task_id=task_id, payload=payload)
        transition(self.conn, task_id, "delivered", cause_seq=s)

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

    async def _teardown(self, task_id: str, *, expect_proc=None) -> None:
        """Idempotent, and safe to call speculatively: if `expect_proc` is
        given and no longer matches what's tracked for task_id, a restart
        already replaced this attempt with a fresh one -- do nothing rather
        than tearing down the new attempt out from under it."""
        if expect_proc is not None and self._procs.get(task_id) is not expect_proc:
            return

        proc = self._procs.pop(task_id, None)
        watcher = self._watchers.pop(task_id, None)
        self._last_event_ts.pop(task_id, None)
        self._wait_grace.pop(task_id, None)

        if proc is not None:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
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
            self._pool.release(wt)
