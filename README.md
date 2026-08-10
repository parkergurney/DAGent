# agent-orchestrator (working name)

Talk to one process; it runs a team of Claude Code sessions — spawned in
internal git worktrees, supervised by a deterministic state machine, and
tracked with durable attempts, candidate lineage, and experiment metrics.

Design: [docs/design.md](docs/design.md). Status: M6 Harbor boundary complete
(worktree pool, dependencies, delivery modes, durable metrics, and CLI).
Usage: [docs/usage.md](docs/usage.md).

## Current security model

Harbor is the supported benchmark isolation boundary. Harbor or another
explicitly trusted outer environment supplies OS-level isolation; the
orchestrator does not sandbox a live Claude worker or protect the host
filesystem from it. Workers in one Harbor trial share that trial's container
resources. Internal Git worktrees isolate concurrent edits from one another,
not workers from the host.

Hidden tests and scoring belong in Harbor's separate verifier environment.
Their results must never enter the agent environment. Visible verification is
public worker feedback only, runs against agent-visible repository state, and
inherits the worker environment; it must use the same trusted outer boundary.
Caller-supplied worker environment variables may contain credentials and are
used only for the child process, never persisted or logged. The orchestrator
does not access the macOS Keychain.

Real CLI runs require either `--external-isolation` (a caller declaration that
Harbor/container isolation is present) or `--trusted-development` (explicit
direct-host development mode). Fake workers remain available without either.

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
    src/orchestrator/harbor.py  small Harbor adapter boundary
    src/orchestrator/metrics.py durable experiment metrics
    src/orchestrator/policies.py policy selection for Harbor experiments
    tests/scenarios/      FakeWorker scenario suite = regression suite
    data/                 runtime state, gitignored
