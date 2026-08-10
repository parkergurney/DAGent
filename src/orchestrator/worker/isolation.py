"""Worker isolation boundary declarations.

The orchestrator does not provide a host security sandbox.  A real worker may
run only when its caller has either placed it inside a trusted external
boundary (Harbor/container) or explicitly selected trusted host development
mode.  Fake workers are deterministic test fixtures and do not need either
declaration.
"""


class WorkerIsolationError(RuntimeError):
    """A live worker was requested without an explicit execution boundary."""


def validate_worker_boundary(*, fake_worker: bool, external_isolation: bool,
                             trusted_development: bool) -> None:
    """Fail closed unless a live worker's outer boundary is explicit.

    ``external_isolation`` is a caller declaration: the orchestrator cannot
    prove that Harbor or another trusted runtime supplied the boundary.  It
    therefore must never be presented as a sandbox switch or as protection
    from a worker.  ``trusted_development`` is the explicit local-host escape
    hatch for development and is not a benchmark isolation mode.
    """
    if fake_worker or external_isolation or trusted_development:
        return
    raise WorkerIsolationError(
        "real Claude workers require an external isolation boundary; "
        "use Harbor with external_isolation=True, or explicitly select "
        "trusted development mode for direct host execution"
    )
