# Harbor canary

The environment and verifier intentionally create the same fixed Git baseline.
The verifier is separate, so the agent image never contains `tests/grader.py`.
The agent publishes only the four declared files under `/logs/artifacts/`.

Run one policy/seed cell with Harbor using the custom import path:

```sh
harbor run -p harbor/tasks/orchestrator-canary \
  -a orchestrator.harbor_agent:HarborOrchestratorAgent \
  -m anthropic/claude-sonnet-5
```

For the controlled pilot, repeat the fresh-trial command with the same task
and model while setting `policy` in the installed-agent config to
`sequential`, `naive-parallel`, and `orchestrator`, using three distinct trial
seeds per policy. The task package, base image, verifier, model, resource
limits, and authentication mechanism must remain unchanged.
