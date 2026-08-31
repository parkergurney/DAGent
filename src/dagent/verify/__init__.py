"""Deterministic verify gate: turns a worker's "done" claim into evidence."""
from dagent.verify.gate import (
    VerifyRequest, VerifyResult, normalize_failure_signature, run_verify,
)

__all__ = ["VerifyRequest", "VerifyResult", "run_verify", "normalize_failure_signature"]
