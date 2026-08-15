# Harbor canary

The environment and verifier intentionally create the same fixed Git baseline.
The verifier is separate, so the agent image never contains `tests/grader.py`.
The agent publishes the declared patch, result, metrics, base SHA, manifest,
and sanitized task summary under `/logs/artifacts/`.
`run_manifest.json` is written before the scheduler starts and records the
comparison inputs without recording credential values.

The canary uses local Ollama through its Anthropic-compatible endpoint. The
default model is `qwen3-coder:30b`, the context is 32K, the worker timeout is
20 minutes, and the orchestrator concurrency ceiling is 2. No Anthropic API
key is used. Ollama must be running
on the host and reachable from Docker at `host.docker.internal:11434`.
The Ollama worker uses a compact coding-tool profile to leave room for model
output; the normal Claude backend retains its complete Claude Code tool set.

Run one policy/seed cell with Harbor using the host-side launcher. It hashes
the complete task package before execution and passes the hash into the
manifest without exposing verifier files to the agent:

```sh
harbor/tasks/orchestrator-canary/run_canary.sh sequential 0
```

The Claude Code system prompt and tool definitions consume roughly 16K tokens,
so 16K is too small for this worker path. If the host has enough memory, keep
Ollama at `OLLAMA_CONTEXT_LENGTH=32768` and keep that value fixed across every
policy and seed.

For the first canary, run the launcher once each for `sequential`,
`naive-parallel`, and `orchestrator`, keeping the model and seed fixed. After
those pass, repeat with three distinct seeds per policy. The task package, base
image, verifier, model, resource limits, and authentication mechanism must
remain unchanged.
