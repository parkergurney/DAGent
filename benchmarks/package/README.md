# Benchmark input package

This is the fixed input package for benchmark runs.
Every graph has ten nodes and distinct write scopes:

- `serial`: one long dependency chain;
- `wide`: ten independent roots;
- `diamond`: fan-out, fan-in, and a final tail;
- `mixed`: independent roots, fan-out, fan-in, and a longer critical path.

The three policies are `sequential`, `naive-parallel`, and `dagent`.
Profiles are seeded and target the lexicographically first graph root, so a
fault cell can distinguish an observed failure from a target that was never
launched. Cloud Claude and local Ollama are separate tracks with independent
resource metadata; they must not be pooled into one comparison.

Validate and enumerate the matrix without running workers:

```sh
dagent-experiment prepare --output-dir results/benchmark-matrix
```

Run one free deterministic cell against a throwaway repository:

```sh
dagent-experiment run --graph wide --policy orchestrator --seed 0 \
  --profile clean --backend-track cloud-claude \
  --repo-root /path/to/throwaway-repo --output-dir results/benchmark/cell-01
```

Summarize saved cells with `dagent-experiment report ...` or the
backward-compatible `dagent-report ...` command. The local Ollama track
is metadata-ready but is not silently substituted for the cloud track; a real
backend run requires the explicit `--live` flag inside a trusted outer
boundary.
