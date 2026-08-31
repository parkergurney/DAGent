"""Durable execution leases and fencing tokens.

An execution lease is scoped to an attempt.  Its monotonically increasing
``generation`` is the fencing token: once a lease is released or recovered,
all actions carrying that lease identity are stale, even if the old worker
later sends a well-formed event.  The module deliberately has no scheduler or
process knowledge; callers carry the returned lease identity into their own
actions and validate it at the write boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from dagent.store.events import (
    _insert_event,
    _now,
    replay_leases,
    ulid,
)


class ExecutionLeaseError(RuntimeError):
    """Base class for deterministic lease failures."""


class LeaseBusyError(ExecutionLeaseError):
    """The attempt already has a live, unexpired owner."""


class LeaseNotFoundError(ExecutionLeaseError):
    """The requested attempt or lease identity does not exist."""


class StaleLeaseError(ExecutionLeaseError):
    """The supplied lease is no longer allowed to perform an action."""


# Names that make integration call sites read naturally.
LeaseConflictError = LeaseBusyError
LeaseOwnershipError = StaleLeaseError


@dataclass(frozen=True)
class ExecutionLease:
    lease_id: str
    attempt_id: str
    task_id: str
    generation: int
    owner_id: str
    status: str
    acquired_at: str
    renewed_at: str
    expires_at: str | None
    released_at: str | None
    release_reason: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "ExecutionLease":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


def _as_timestamp(value: str | datetime | None) -> str:
    if value is None:
        return _now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if not isinstance(value, str):
        raise TypeError("now must be an ISO timestamp or datetime")
    _parse_timestamp(value)
    return value


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _expiry(now: str, ttl_s: float | None) -> str | None:
    if ttl_s is None:
        return None
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)) or ttl_s <= 0:
        raise ValueError("ttl_s must be a positive number or None")
    return (_parse_timestamp(now) + timedelta(seconds=ttl_s)).isoformat()


def _atomic(conn):
    """Use an immediate transaction, or a savepoint inside a caller txn."""
    class Atomic:
        def __enter__(self):
            self.nested = conn.in_transaction
            if self.nested:
                conn.execute("SAVEPOINT execution_lease_mutation")
            else:
                conn.execute("BEGIN IMMEDIATE")
            return conn

        def __exit__(self, exc_type, exc, tb):
            if self.nested:
                if exc_type is None:
                    conn.execute("RELEASE SAVEPOINT execution_lease_mutation")
                else:
                    conn.execute("ROLLBACK TO SAVEPOINT execution_lease_mutation")
                    conn.execute("RELEASE SAVEPOINT execution_lease_mutation")
            elif exc_type is None:
                conn.commit()
            else:
                conn.rollback()
            return False
    return Atomic()


def _require_owner(owner_id: str) -> str:
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("owner_id must be a non-empty string")
    return owner_id


def _identity(attempt_id, lease_id, generation, owner_id):
    if isinstance(attempt_id, ExecutionLease):
        lease = attempt_id
        if lease_id is not None and lease_id != lease.lease_id:
            raise ValueError("lease_id conflicts with the lease object")
        if generation is not None and generation != lease.generation:
            raise ValueError("generation conflicts with the lease object")
        # Keep an explicitly supplied owner in the identity so the database
        # lookup reports a stale-owner rejection rather than turning a late
        # worker action into an argument-shape error.
        resolved_owner = lease.owner_id if owner_id is None else _require_owner(owner_id)
        return lease.attempt_id, lease.lease_id, lease.generation, resolved_owner
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("attempt_id must be a non-empty string")
    if not isinstance(lease_id, str) or not lease_id:
        raise ValueError("lease_id is required")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ValueError("generation must be a positive integer")
    return attempt_id, lease_id, generation, _require_owner(owner_id)


def _row_for_identity(conn, attempt_id, lease_id, generation, owner_id):
    row = conn.execute(
        "SELECT * FROM execution_leases WHERE lease_id = ? AND attempt_id = ? AND generation = ?",
        (lease_id, attempt_id, generation),
    ).fetchone()
    if row is None:
        raise LeaseNotFoundError(
            f"unknown lease {lease_id!r} for attempt {attempt_id!r} generation {generation}"
        )
    if row["owner_id"] != owner_id:
        raise StaleLeaseError(f"lease {lease_id!r} is owned by another execution")
    return row


def _require_active(row, now: str):
    if row["status"] != "active":
        raise StaleLeaseError(f"lease {row['lease_id']!r} is {row['status']}, not active")
    if row["expires_at"] is not None and _parse_timestamp(now) >= _parse_timestamp(row["expires_at"]):
        raise StaleLeaseError(f"lease {row['lease_id']!r} has expired")


def _release_row(conn, row, *, status, reason, now, source):
    conn.execute(
        "UPDATE execution_leases SET status = ?, released_at = ?, release_reason = ?, "
        "updated_at = ? WHERE lease_id = ?",
        (status, now, reason, now, row["lease_id"]),
    )
    _insert_event(
        conn, now, source=source,
        type="execution_lease.recovered" if status == "recovered" else "execution_lease.released",
        task_id=row["task_id"],
        payload={
            "lease_id": row["lease_id"], "attempt_id": row["attempt_id"],
            "task_id": row["task_id"], "generation": row["generation"],
            "owner_id": row["owner_id"], "status": status,
            "released_at": now, "release_reason": reason,
        },
    )


def acquire(conn, attempt_id: str, owner_id: str, *, ttl_s: float | None = 300,
            now: str | datetime | None = None, lease_id: str | None = None,
            source: str = "scheduler") -> ExecutionLease:
    """Acquire the next generation for an attempt.

    A live lease is never stolen.  An expired lease is durably recovered first,
    then the next generation is allocated in the same transaction.
    """
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("attempt_id must be a non-empty string")
    owner_id = _require_owner(owner_id)
    now = _as_timestamp(now)
    expires_at = _expiry(now, ttl_s)
    lease_id = lease_id or ulid()
    with _atomic(conn):
        attempt = conn.execute(
            "SELECT task_id FROM attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if attempt is None:
            raise LeaseNotFoundError(f"unknown attempt {attempt_id!r}")
        current = conn.execute(
            "SELECT * FROM execution_leases WHERE attempt_id = ? AND status = 'active'",
            (attempt_id,),
        ).fetchone()
        if current is not None:
            if current["expires_at"] is None or _parse_timestamp(now) < _parse_timestamp(current["expires_at"]):
                raise LeaseBusyError(f"attempt {attempt_id!r} already has active lease")
            _release_row(conn, current, status="recovered", reason="expired_before_reacquire",
                         now=now, source="watchdog")
        generation = conn.execute(
            "SELECT COALESCE(MAX(generation), 0) + 1 FROM execution_leases WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()[0]
        payload = {
            "lease_id": lease_id, "attempt_id": attempt_id, "task_id": attempt["task_id"],
            "generation": generation, "owner_id": owner_id, "acquired_at": now,
            "renewed_at": now, "expires_at": expires_at, "created_at": now,
        }
        conn.execute(
            "INSERT INTO execution_leases "
            "(lease_id, attempt_id, task_id, generation, owner_id, status, acquired_at, "
            " renewed_at, expires_at, released_at, release_reason, created_at, updated_at) "
            "VALUES (?,?,?,?,?,'active',?,?,?,NULL,NULL,?,?)",
            (lease_id, attempt_id, attempt["task_id"], generation, owner_id, now, now,
             expires_at, now, now),
        )
        _insert_event(conn, now, source=source, type="execution_lease.acquired",
                      task_id=attempt["task_id"], payload=payload)
        return ExecutionLease.from_row(conn.execute(
            "SELECT * FROM execution_leases WHERE lease_id = ?", (lease_id,)
        ).fetchone())


def validate(conn, attempt_id, lease_id=None, generation=None, owner_id=None, *,
             now: str | datetime | None = None) -> ExecutionLease:
    """Validate that an ownership token is current, active, and unexpired."""
    attempt_id, lease_id, generation, owner_id = _identity(
        attempt_id, lease_id, generation, owner_id
    )
    now = _as_timestamp(now)
    row = _row_for_identity(conn, attempt_id, lease_id, generation, owner_id)
    _require_active(row, now)
    return ExecutionLease.from_row(row)


def renew(conn, attempt_id, lease_id=None, generation=None, owner_id=None, *,
          ttl_s: float | None = 300, now: str | datetime | None = None,
          source: str = "worker") -> ExecutionLease:
    """Renew a current lease without changing its fencing generation."""
    attempt_id, lease_id, generation, owner_id = _identity(
        attempt_id, lease_id, generation, owner_id
    )
    now = _as_timestamp(now)
    expires_at = _expiry(now, ttl_s)
    with _atomic(conn):
        row = _row_for_identity(conn, attempt_id, lease_id, generation, owner_id)
        _require_active(row, now)
        conn.execute(
            "UPDATE execution_leases SET renewed_at = ?, expires_at = ?, updated_at = ? "
            "WHERE lease_id = ?",
            (now, expires_at, now, lease_id),
        )
        _insert_event(
            conn, now, source=source, type="execution_lease.renewed", task_id=row["task_id"],
            payload={"lease_id": lease_id, "attempt_id": attempt_id,
                     "task_id": row["task_id"], "generation": generation,
                     "owner_id": owner_id, "renewed_at": now, "expires_at": expires_at},
        )
        return ExecutionLease.from_row(conn.execute(
            "SELECT * FROM execution_leases WHERE lease_id = ?", (lease_id,)
        ).fetchone())


def release(conn, attempt_id, lease_id=None, generation=None, owner_id=None, *,
            reason: str = "released", now: str | datetime | None = None,
            source: str = "worker") -> ExecutionLease:
    """Release a lease; repeating the exact release is idempotent."""
    attempt_id, lease_id, generation, owner_id = _identity(
        attempt_id, lease_id, generation, owner_id
    )
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    now = _as_timestamp(now)
    with _atomic(conn):
        row = _row_for_identity(conn, attempt_id, lease_id, generation, owner_id)
        if row["status"] != "active":
            return ExecutionLease.from_row(row)
        _require_active(row, now)
        _release_row(conn, row, status="released", reason=reason, now=now, source=source)
        return ExecutionLease.from_row(conn.execute(
            "SELECT * FROM execution_leases WHERE lease_id = ?", (lease_id,)
        ).fetchone())


def recover(conn, attempt_id, lease_id=None, generation=None, *,
            reason: str = "recovered", now: str | datetime | None = None,
            source: str = "watchdog") -> ExecutionLease:
    """Fence an active lease from a watchdog/reconciler; repeating is safe.

    Recovery intentionally does not require the worker owner.  The caller must
    still identify the exact attempt, lease id, and generation, so it cannot
    accidentally recover a newer lease.
    """
    if isinstance(attempt_id, ExecutionLease):
        lease = attempt_id
        if lease_id is not None and lease_id != lease.lease_id:
            raise ValueError("lease_id conflicts with the lease object")
        if generation is not None and generation != lease.generation:
            raise ValueError("generation conflicts with the lease object")
        attempt_id, lease_id, generation = lease.attempt_id, lease.lease_id, lease.generation
    else:
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("attempt_id must be a non-empty string")
        if not isinstance(lease_id, str) or not lease_id:
            raise ValueError("lease_id is required")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise ValueError("generation must be a positive integer")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    now = _as_timestamp(now)
    with _atomic(conn):
        row = conn.execute(
            "SELECT * FROM execution_leases WHERE lease_id = ? AND attempt_id = ? "
            "AND generation = ?", (lease_id, attempt_id, generation),
        ).fetchone()
        if row is None:
            raise LeaseNotFoundError(f"unknown lease {lease_id!r}")
        if row["status"] != "active":
            return ExecutionLease.from_row(row)
        _release_row(conn, row, status="recovered", reason=reason, now=now, source=source)
        return ExecutionLease.from_row(conn.execute(
            "SELECT * FROM execution_leases WHERE lease_id = ?", (lease_id,)
        ).fetchone())


__all__ = [
    "ExecutionLease", "ExecutionLeaseError", "LeaseBusyError", "LeaseConflictError",
    "LeaseNotFoundError", "LeaseOwnershipError", "StaleLeaseError", "acquire", "renew",
    "release", "recover", "validate", "replay_leases",
]
