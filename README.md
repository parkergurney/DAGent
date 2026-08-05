# agent-orchestrator (working name)

Talk to one process; it runs a team of Claude Code sessions — spawned in
isolated git worktrees, supervised by a deterministic state machine, verified
by tests before anything is called done, and benchmarked against baselines.

Design: [docs/design.md](docs/design.md). Status: M5 complete (worktree pool, dep resolution, all three delivery modes), plus an `orchestrator` CLI on top.
Usage: [docs/usage.md](docs/usage.md).

Day-to-day operation is meant to be natural language through an agent session:
ask it to queue tasks, start or watch a batch, check status, and answer
escalations. The agent uses the `orchestrator` CLI underneath; operators should
only need raw commands for setup, debugging, or automation.

## Layout

    docs/design.md        full architecture design doc, the source of truth
    CLAUDE.md              trimmed core (thesis, architecture, invariants), always loaded
    .claude/skills/        topic skills with the rest of docs/design.md, loaded on demand
    docs/usage.md         practical "how do I actually run tasks" guide
    docs/devlog.md        session log; writes the eventual post
    src/orchestrator/
      cli.py              `orchestrator` console script: add-task/run/daemon/answer/status
      store/              SQLite: events (append-only facts) + tasks (derived)
      scheduler/          state machine, asyncio loop, watchdog
      worker/             Agent SDK sessions, FakeWorker, worktree pool
      verify/             deterministic verify gate + standalone CLI
      supervisor/         TriagePacket -> single LLM call -> closed action enum
      delivery/           pr | local | scout
    bench/                harness, conditions, task suites
    tests/scenarios/      FakeWorker scenario suite = regression suite
    data/                 runtime state, gitignored
