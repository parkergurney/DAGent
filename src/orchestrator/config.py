"""Orchestrator config: defaults from design.md section 12, with optional TOML
overrides.

Frozen dataclass, single flat namespace. No env-var layering, no validation
framework -- unknown TOML keys are ignored so a config file can carry comments
or future keys without breaking M0.
"""
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    max_concurrency: int = 4
    max_retries: int = 2
    # One equivalent descendant failure is enough to use deterministic
    # escalation, unless a later candidate or signature supplies new evidence.
    repeated_failure_threshold: int = 1
    max_nudges: int = 2
    stall_threshold_s: int = 300
    wait_ceiling_s: int = 1800
    verify_timeout_s: int = 600
    transcript_tail_tokens: int = 3000
    # Placeholder model ids; pin exact versions in the Harbor experiment
    # configuration when running an evaluation.
    model_worker: str = "claude-sonnet-5"
    model_supervisor: str = "claude-sonnet-5"
    protocol_recovery_v2: bool = True
    deterministic_recovery: bool = True
    adaptive_scheduling: bool = True
    evidence_ladder: bool = True


def load(path: str | Path | None = None) -> Config:
    if not path or not Path(path).exists():
        return Config()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    known = {field.name for field in fields(Config)}
    return Config(**{k: v for k, v in data.items() if k in known})
