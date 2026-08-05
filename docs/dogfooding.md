## Phase 1 — author batch N

Ask the orchestrator skill to draft it, then **read and commit the script**.
NL is the authoring interface; the script is the artifact.

> Draft `dogfood/batch01.sh` — 16 add-task calls against the `sqlite-utils` repo,
> writing to `data/dogfood-b01.db`. Mine github.com/simonw/sqlite-utils issues
> for real material. Composition: 10 straightforward, 3 in a dependency chain
> (one 3-deep), 2 deliberately underspecified, 1 genuinely hard. Delivery: 11
> local, 4 pr, 1 scout. Capture task ids into shell vars for `--depends-on`.
> Don't run it.

Template shape:

```bash
#!/usr/bin/env bash
set -euo pipefail
DB=data/dogfood-b01.db
VERIFY="/Users/parkergurney/.venvs/dogfood/bin/python -m pytest -x -q"
# setup_cmd omitted — deps pre-installed in ~/.venvs/dogfood

t1=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "..." --brief "..." \
  --verify-cmd "$VERIFY" --delivery-mode local)

t2=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "..." --brief "..." \
  --verify-cmd "$VERIFY" --delivery-mode local --depends-on "$t1")
```

`add-task` prints the bare id on stdout, so `$(...)` capture works directly.

Review before committing: are these genuinely distinct tasks, or the same shape
reworded? Is the "hard" one actually hard? Then `git add dogfood/batch01.sh`.

```bash
bash dogfood/batch01.sh        # creates tasks only, nothing runs
```

---

## Phase 2 — run and observe

```bash
orchestrator run --db data/dogfood-b01.db \
  --repo-root ~/Development/sqlite-utils --max-concurrency 4
```

(Confirm `--repo-root` semantics against `--help` on first use — it's required
and the audit didn't pin down whether it's the repo or a parent dir.)

Run it in the foreground. `_notify_loop` streams `[needs_human]`, `[delivered]`,
`[failed]` live — that IS your monitor. Don't add a `watch orchestrator status`
loop; plain `status` won't surface escalations anyway.

Through the skill, mid-run:
- *"what's going on"* → `status --digest`
- *"why is task X stuck"* → `status <id>` (prints escalation summary, numbered
  options, and the exact answer command)
- *"answer task X: use option 2, and note that…"* → `answer <id> "…"`

**Rules:** don't intervene reflexively — a worker that looks stuck is the
watchdog's job. Answer `needs_human` promptly (that path is under test too), but
record per escalation: *was it warranted?* *was `recommended` the option you'd
have picked?* Both are Phase 4 findings. Log surprises to devlog **as they
happen** — you will not reconstruct "why did it nudge instead of restart" in
three weeks, and this phase generates the post's best material.

Note: `answer` transitions `needs_human → queued`, not `→ running` — the answer
is folded into the brief and a fresh session picks it up. Expected, documented.

---

## Phase 3 — fault injection

Fresh 3-4 task batch each (`data/dogfood-fault1.db`, etc.).

**3.1 Kill the daemon mid-batch.** `kill -9` the `run` process while tasks are
`running`, restart. Expect reconciliation → synthetic `worker.exited` → triage.
No task lost, no manual DB edits.

**3.2 Kill one worker subprocess** (not the daemon). Expect watchdog silence
detection past 300s → `worker.stalled` → triage.

**3.3 Force a verify failure.** A brief you know breaks a test. Expect
`tests_failed` → `restart`. **Then confirm attempt 2's prompt actually contains
the feedback** — that the failure output reached the new session, not just the
event log. Most commonly-broken link in the loop.

**3.4 Force a delivery failure.** *New, because audit D/13 says this path has no
scheduler-level test.* Easiest trigger: start a `pr`-mode task, then before it
delivers, push a conflicting commit to your fork's `main` so the push is
rejected. Expect `delivery.failed` → triage → sane supervisor action. This is
the one code path running live for the first time.

---

## Phase 4 — review

```bash
orchestrator status --db data/dogfood-b01.db --digest
```

```sql
-- terminal distribution
SELECT state, count(*) FROM tasks GROUP BY state;

-- every supervisor decision + stated reasoning
SELECT task_id, json_extract(payload,'$.action'), json_extract(payload,'$.reason')
FROM events WHERE type='supervisor.acted' ORDER BY seq;

-- verify failures by cause; watch flaky
SELECT task_id, json_extract(payload,'$.cause'), json_extract(payload,'$.flaky')
FROM events WHERE type='verify.failed' ORDER BY seq;

-- did the supervisor ever fail to produce a valid action?
SELECT * FROM events WHERE type='supervisor.failed';

-- protected-path hits; only expect these when the batch passed explicit globs
SELECT * FROM events WHERE type='verify.failed'
  AND json_extract(payload,'$.cause')='protected_path_modified';

-- supervision cost share
SELECT source, sum(cost_usd) FROM events GROUP BY source;
```

By hand:

- **Read every delivered artifact.** Start with `orchestrator status <task_id>`:
  for `pr`, review the PR URL/commit; for `local`, run the printed
  `git diff <before_sha>..<after_sha>` and `git log` commands; for `scout`,
  read `data/<task_id>/report.md`. The status `patch:` line points at the
  event-specific saved patch, while `data/<task_id>/review.patch` is the latest
  convenience copy after pooled worktrees are torn down. "Tests passed" is not
  the bar — *would you merge this from a junior engineer*. Garbage that passes
  verification is a finding about the **verify gate**, not a success. Watch for
  stub implementations, weakened tests, changes wildly out of proportion to the
  brief.
- **Confirm cleanup.** After a non-daemon run, `git worktree list` for the
  target repo should not show pooled `slot-*` worktrees, `data/worktrees`
  should have no live slots, `git branch --list 'pool/slot-*'` should be
  empty, and `git status --short` should be clean except intentional local
  deliveries already merged into `main`.
- **Re-run the replay invariant against this real event log** — first time it's
  seen real volume and interleaving.
- **Judge each supervisor decision.** Defensible, not optimal, is the standard.
- **Escalation precision.** Warranted? Was `recommended` right?

---

## Phase 5 — fix, then a FRESH batch

Classify each finding:

- **Deterministic-code bug** → fix, then add a **FakeWorker scenario** (current
  set: `clean, no_commit, empty_diff, protected_edit, stall, ask, crash, wait`).
  Now covered in CI in milliseconds, zero tokens, forever.
- **Supervisor heuristic bug** → fix the prompt, then re-run packet replay
  (`supervisor/replay.py`) against saved packets to confirm no regression in
  prior triage decisions.
- **Contract change** → update the `sync:` block in design.md; `test_design_sync`
  fails CI if the skills copy drifts.

Then write **batch02.sh — different tasks, not reworded** — and repeat Phases
2-4. Old batches stay as occasional regression runs but don't count toward the
bar.

### Stopping condition

A **fresh** batch (never used to fix anything) comes back with:

1. Zero state-machine invariant violations — `replay(events) == tasks` holds.
2. Zero unhandled crashes requiring manual DB surgery.
3. Every triage decision defensible on inspection.
4. Delivered diffs you'd actually merge.

Plus all four fault-injection passes clean. **Then stop.** Don't keep dogfooding
"just to be safe" — that's the drift into tuning-against-one-repo you're
avoiding.

### Convergence signals

- Bugs-per-batch trending down matters more than any single clean run.
- Running out of novel task shapes = you're close. NOT a cue to invent contrived
  edge cases.
- If you want `--from-file` by batch 3, build it — M6's harness wants it too.

### After the bar clears

M6. Pin `model_worker`/`model_supervisor` to exact version strings first
(audit C/7). Author the seeded suite (one repo, 8-10 issues, hidden tests) and
the SWE-bench loader in parallel with the harness runner — on a repo the
orchestrator has never touched.
