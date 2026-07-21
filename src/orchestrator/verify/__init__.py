"""Deterministic verify gate: turns a worker's "done" claim into evidence."""
from orchestrator.verify.gate import DEFAULT_PROTECTED, VerifyRequest, VerifyResult, run_verify

__all__ = ["VerifyRequest", "VerifyResult", "run_verify", "DEFAULT_PROTECTED"]
