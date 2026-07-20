# agent-orchestrator (working name)

Talk to one process; it runs a crew of Claude Code sessions — spawned in
isolated git worktrees, supervised by a deterministic state machine, verified
by tests before anything is called done, and benchmarked against baselines.

Design: [docs/design.md](docs/design.md). Status: pre-M0.

## Layout

    docs/design.md        source of truth for architecture (CLAUDE.md -> here)
    docs/devlog.md        session log; writes the eventual post
    src/orchestrator/
      store/              SQLite: events (append-only facts) + tasks (derived)
      scheduler/          state machine, asyncio loop, watchdog
      worker/             Agent SDK sessions, FakeWorker, worktree pool
      verify/             deterministic verify gate + standalone CLI
      supervisor/         TriagePacket -> single LLM call -> closed action enum
      delivery/           pr | local | scout
    bench/                harness, conditions, task suites
    tests/scenarios/      FakeWorker scenario suite = regression suite
    data/                 runtime state, gitignored
