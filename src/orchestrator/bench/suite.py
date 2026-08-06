"""Benchmark suite file loading.

The suite file is TOML so it can be reviewed and committed like dogfood batch
scripts, but the runner can consume it without shelling out to `add-task`.
"""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BenchTask:
    key: str
    title: str
    brief: str
    verify_cmd: str | None = None
    hidden_cmd: str | None = None
    setup_cmd: str | None = None
    protected_paths: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    delivery_mode: str = "scout"
    max_retries: int | None = None


@dataclass(frozen=True)
class BenchSuite:
    name: str
    repo: str | None
    base_branch: str = "main"
    verify_cmd: str | None = None
    hidden_cmd: str | None = None
    setup_cmd: str | None = None
    protected_paths: tuple[str, ...] = ()
    delivery_mode: str = "scout"
    max_retries: int = 2
    tasks: tuple[BenchTask, ...] = field(default_factory=tuple)


def load_suite(path: str | Path) -> BenchSuite:
    path = Path(path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    bench = raw.get("bench", {})
    task_rows = raw.get("tasks", [])
    if not task_rows:
        raise ValueError("benchmark suite must define at least one [[tasks]] entry")

    suite_defaults = {
        "verify_cmd": bench.get("verify_cmd"),
        "hidden_cmd": bench.get("hidden_cmd"),
        "setup_cmd": bench.get("setup_cmd"),
        "protected_paths": tuple(bench.get("protected_paths", [])),
        "delivery_mode": bench.get("delivery_mode", "scout"),
        "max_retries": int(bench.get("max_retries", 2)),
    }
    tasks = []
    seen = set()
    for i, row in enumerate(task_rows, start=1):
        key = str(row.get("id") or row.get("key") or f"t{i}")
        if key in seen:
            raise ValueError(f"duplicate task id {key!r}")
        seen.add(key)
        deps = tuple(row.get("depends_on", []))
        unknown = [dep for dep in deps if dep not in seen]
        if unknown:
            raise ValueError(
                f"task {key!r} depends on unknown or later task(s): {', '.join(unknown)}"
            )
        tasks.append(BenchTask(
            key=key,
            title=row["title"],
            brief=row["brief"],
            verify_cmd=row.get("verify_cmd", suite_defaults["verify_cmd"]),
            hidden_cmd=row.get("hidden_cmd", suite_defaults["hidden_cmd"]),
            setup_cmd=row.get("setup_cmd", suite_defaults["setup_cmd"]),
            protected_paths=tuple(row.get("protected_paths", suite_defaults["protected_paths"])),
            depends_on=deps,
            delivery_mode=row.get("delivery_mode", suite_defaults["delivery_mode"]),
            max_retries=row.get("max_retries"),
        ))

    return BenchSuite(
        name=bench.get("name", path.stem),
        repo=bench.get("repo"),
        base_branch=bench.get("base_branch", "main"),
        verify_cmd=suite_defaults["verify_cmd"],
        hidden_cmd=suite_defaults["hidden_cmd"],
        setup_cmd=suite_defaults["setup_cmd"],
        protected_paths=suite_defaults["protected_paths"],
        delivery_mode=suite_defaults["delivery_mode"],
        max_retries=suite_defaults["max_retries"],
        tasks=tuple(tasks),
    )
