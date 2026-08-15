"""Focused proof cases for execution leases and fencing tokens."""

import json

import pytest

from orchestrator.execution_lease import (
    LeaseBusyError,
    StaleLeaseError,
    acquire,
    recover,
    release,
    renew,
    validate,
)
from orchestrator.store import connect, create_attempt, create_task
from orchestrator.store.events import replay, replay_leases


def _attempt(conn, *, task_id="task-1", attempt_id="attempt-1"):
    create_task(conn, task_id=task_id, title="task", brief="brief", repo="repo",
                delivery_mode="scout")
    create_attempt(conn, attempt_id=attempt_id, task_id=task_id, run_id="run-1", attempt_no=1,
                   base_sha="base", candidate_branch=f"attempt/{attempt_id}",
                   execution_contract="public")
    return attempt_id


def _lease_rows(conn):
    return [dict(row) for row in conn.execute(
        "SELECT * FROM execution_leases ORDER BY generation"
    )]


def _events(conn):
    return conn.execute("SELECT * FROM events ORDER BY seq").fetchall()


def test_acquire_renew_validate_use_a_monotonic_fencing_generation():
    conn = connect()
    attempt_id = _attempt(conn)

    first = acquire(conn, attempt_id, "worker-a", ttl_s=None,
                    now="2026-01-01T00:00:00+00:00")
    renewed = renew(conn, first, ttl_s=30, now="2026-01-01T00:01:00+00:00")

    assert first.generation == renewed.generation == 1
    assert validate(conn, renewed, now="2026-01-01T00:01:01+00:00").lease_id == first.lease_id
    with pytest.raises(LeaseBusyError):
        acquire(conn, attempt_id, "worker-b", ttl_s=None,
                now="2026-01-01T00:01:02+00:00")

    released = release(conn, renewed, reason="worker-exited",
                       now="2026-01-01T00:01:03+00:00")
    second = acquire(conn, attempt_id, "worker-b", ttl_s=None,
                     now="2026-01-01T00:01:04+00:00")
    assert released.status == "released"
    assert second.generation == 2
    assert second.lease_id != first.lease_id


def test_stale_worker_is_fenced_after_recovery_and_reacquisition():
    conn = connect()
    attempt_id = _attempt(conn)
    old = acquire(conn, attempt_id, "worker-a", ttl_s=None)
    recovered = recover(conn, old, reason="worker-crashed")
    current = acquire(conn, attempt_id, "worker-b", ttl_s=None)

    assert recovered.status == "recovered"
    assert current.generation == old.generation + 1
    with pytest.raises(StaleLeaseError):
        validate(conn, old)
    with pytest.raises(StaleLeaseError):
        renew(conn, old)
    with pytest.raises(StaleLeaseError):
        validate(conn, current, owner_id="late-worker")


def test_duplicate_release_and_recovery_are_idempotent():
    conn = connect()
    attempt_id = _attempt(conn)
    released = release(conn, acquire(conn, attempt_id, "worker-a", ttl_s=None),
                       reason="normal", now="2026-01-01T00:00:01+00:00")
    duplicate_release = release(conn, released, reason="duplicate",
                                now="2026-01-01T00:00:02+00:00")
    assert duplicate_release == released
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'execution_lease.released'"
    ).fetchone()[0] == 1

    recovered = recover(conn, acquire(conn, attempt_id, "worker-b", ttl_s=None),
                        reason="reconcile", now="2026-01-01T00:00:03+00:00")
    duplicate_recovery = recover(conn, recovered, reason="reconcile-again",
                                 now="2026-01-01T00:00:04+00:00")
    assert duplicate_recovery == recovered
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'execution_lease.recovered'"
    ).fetchone()[0] == 1


def test_expired_lease_is_recovered_before_next_generation():
    conn = connect()
    attempt_id = _attempt(conn)
    old = acquire(conn, attempt_id, "worker-a", ttl_s=10,
                  now="2026-01-01T00:00:00+00:00")
    current = acquire(conn, attempt_id, "worker-b", ttl_s=None,
                      now="2026-01-01T00:00:11+00:00")

    assert current.generation == 2
    row = conn.execute(
        "SELECT status, release_reason FROM execution_leases WHERE lease_id = ?",
        (old.lease_id,),
    ).fetchone()
    assert dict(row) == {"status": "recovered", "release_reason": "expired_before_reacquire"}


def test_lease_and_task_replay_agree_after_restart(tmp_path):
    db = str(tmp_path / "orchestrator.db")
    conn = connect(db)
    attempt_id = _attempt(conn)
    first = acquire(conn, attempt_id, "worker-a", ttl_s=None,
                    now="2026-01-01T00:00:00+00:00")
    release(conn, first, reason="retry", now="2026-01-01T00:00:01+00:00")
    second = acquire(conn, attempt_id, "worker-b", ttl_s=None,
                     now="2026-01-01T00:00:02+00:00")
    renew(conn, second, ttl_s=None, now="2026-01-01T00:00:03+00:00")
    conn.close()

    conn = connect(db)
    live_leases = {row["lease_id"]: dict(row) for row in conn.execute(
        "SELECT * FROM execution_leases ORDER BY generation"
    )}
    assert replay_leases(_events(conn)) == live_leases
    live_tasks = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM tasks")}
    assert replay(_events(conn)) == live_tasks


def test_missing_lease_table_is_added_on_reopen(tmp_path):
    db = str(tmp_path / "migration.db")
    conn = connect(db)
    conn.execute("DROP TABLE execution_leases")
    conn.commit()
    conn.close()

    conn = connect(db)
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'execution_leases'"
    ).fetchone()[0] == "execution_leases"
    indexes = {
        row["name"] for row in conn.execute("PRAGMA index_list(execution_leases)")
    }
    assert "idx_execution_leases_active" in indexes


def test_lease_events_are_durable_and_have_fencing_metadata():
    conn = connect()
    attempt_id = _attempt(conn)
    lease = acquire(conn, attempt_id, "worker-a", ttl_s=None)
    event = conn.execute(
        "SELECT * FROM events WHERE type = 'execution_lease.acquired'"
    ).fetchone()
    payload = json.loads(event["payload"])
    assert payload["lease_id"] == lease.lease_id
    assert payload["attempt_id"] == attempt_id
    assert payload["generation"] == 1
    assert payload["owner_id"] == "worker-a"
