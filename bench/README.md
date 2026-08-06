# Benchmark Suites

M6 benchmark inputs live here as TOML suite files. Generate a starter file with:

```bash
bench-run example-suite bench/example-suite.toml
```

Run conditions into `data/bench/`:

```bash
bench-run run --suite bench/example-suite.toml --condition sequential --seed 1
bench-run run --suite bench/example-suite.toml --condition naive-parallel --seed 1
bench-run run --suite bench/example-suite.toml --condition orchestrator --seed 1
bench-run report data/bench
bench-run report data/bench --summary --group-by condition
```

Use fresh target repos, not dogfood repos. Hidden checks belong in each task's
`hidden_cmd`; protect hidden/instructor-owned files with `protected_paths`.
