"""Deterministic verify gate: turns a worker's "done" claim into evidence."""
from orchestrator.verify.gate import (
    DEFAULT_PROTECTED, VerifyRequest, VerifyResult, normalize_failure_signature, run_verify,
)

__all__ = ["VerifyRequest", "VerifyResult", "run_verify", "normalize_failure_signature",
           "DEFAULT_PROTECTED"]
