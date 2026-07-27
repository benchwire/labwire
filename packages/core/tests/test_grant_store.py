"""The grant store's hard edges: atomicity, persistence, file separation."""

import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from labwire.core.grants import PENDING_CAP, GrantStore

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store() -> GrantStore:
    return GrantStore(Path(tempfile.mkdtemp(prefix="grants-")), serial_number="rig-1")


def _approved(store: GrantStore, *, max_uses: int = 1, command: str = "move_plate") -> str:
    pending = store.record_pending(
        command=command, params={"to": "labwire:deck/s0"}, params_digest="sha256:aa", now=NOW
    )
    grant = store.approve(pending.request_id, now=NOW, ttl=timedelta(minutes=15), max_uses=max_uses)
    return grant.grant_id


def test_a_spent_single_use_grant_survives_a_restart(store: GrantStore) -> None:
    """A restart must not resurrect a spent grant (SPEC 8.6)."""
    grant_id = _approved(store)
    first = store.verify_and_consume(
        grant_id=grant_id, command="move_plate", params_digest="sha256:aa", now=NOW
    )
    assert first.ok
    assert first.use_index == 1

    reborn = GrantStore(store.directory, serial_number="rig-1")  # a fresh process
    second = reborn.verify_and_consume(
        grant_id=grant_id, command="move_plate", params_digest="sha256:aa", now=NOW
    )
    assert not second.ok
    assert second.reason == "exhausted"


async def test_two_concurrent_submits_cannot_both_spend_the_last_use(
    store: GrantStore,
) -> None:
    """The check-and-consume holds no awaits, so racers serialize."""
    grant_id = _approved(store, max_uses=1)

    async def attempt() -> bool:
        return store.verify_and_consume(
            grant_id=grant_id, command="move_plate", params_digest="sha256:aa", now=NOW
        ).ok

    outcomes = await asyncio.gather(*(attempt() for _ in range(8)))
    assert outcomes.count(True) == 1


def test_every_refusal_reason_is_reachable(store: GrantStore) -> None:
    verify = store.verify_and_consume
    assert verify(grant_id="g-none", command="c", params_digest="d", now=NOW).reason == "unknown"

    grant_id = _approved(store, max_uses=2)
    assert (
        verify(grant_id=grant_id, command="other", params_digest="sha256:aa", now=NOW).reason
        == "command_mismatch"
    )
    assert (
        verify(grant_id=grant_id, command="move_plate", params_digest="sha256:bb", now=NOW).reason
        == "params_mismatch"
    )
    assert (
        verify(
            grant_id=grant_id,
            command="move_plate",
            params_digest="sha256:aa",
            now=NOW - timedelta(minutes=1),
        ).reason
        == "not_yet_valid"
    )
    assert (
        verify(
            grant_id=grant_id,
            command="move_plate",
            params_digest="sha256:aa",
            now=NOW + timedelta(hours=1),
        ).reason
        == "expired"
    )
    assert verify(grant_id=grant_id, command="move_plate", params_digest="sha256:aa", now=NOW).ok
    assert verify(grant_id=grant_id, command="move_plate", params_digest="sha256:aa", now=NOW).ok
    assert (
        verify(grant_id=grant_id, command="move_plate", params_digest="sha256:aa", now=NOW).reason
        == "exhausted"
    )

    revoked = _approved(store)
    assert store.revoke(revoked)
    assert (
        verify(grant_id=revoked, command="move_plate", params_digest="sha256:aa", now=NOW).reason
        == "revoked"
    )


def test_a_grant_for_another_instrument_is_refused(store: GrantStore) -> None:
    grant_id = _approved(store)
    other = GrantStore(store.directory, serial_number="rig-2")
    verdict = other.verify_and_consume(
        grant_id=grant_id, command="move_plate", params_digest="sha256:aa", now=NOW
    )
    assert verdict.reason == "instrument_mismatch"


def test_pending_requests_expire_and_are_capped(store: GrantStore) -> None:
    for index in range(PENDING_CAP + 20):
        store.record_pending(
            command="move_plate",
            params={"n": index},
            params_digest=f"sha256:{index:02x}",
            now=NOW,
        )
    assert len(store.pending(now=NOW)) <= PENDING_CAP
    assert store.pending(now=NOW + timedelta(hours=1)) == []  # all expired


def test_repeating_the_same_refused_call_does_not_multiply_pendings(
    store: GrantStore,
) -> None:
    """An agent retrying the identical call keeps one pending entry."""
    for _ in range(5):
        store.record_pending(
            command="move_plate", params={"to": "s0"}, params_digest="sha256:same", now=NOW
        )
    matching = [entry for entry in store.pending(now=NOW) if entry.params_digest == "sha256:same"]
    assert len(matching) == 1


def test_the_operator_file_and_server_files_are_separate(store: GrantStore) -> None:
    """The server never writes grants.json; the operator tool never writes uses."""
    grant_id = _approved(store)
    before = (store.directory / "grants.json").read_text()
    store.verify_and_consume(
        grant_id=grant_id, command="move_plate", params_digest="sha256:aa", now=NOW
    )
    after = (store.directory / "grants.json").read_text()
    assert before == after  # consuming touched uses.json, not the operator's file
    assert json.loads((store.directory / "uses.json").read_text())[grant_id] == 1


def test_grant_ids_carry_real_entropy(store: GrantStore) -> None:
    """A grant id is a bearer value (SPEC 8.6): 128 bits minimum."""
    grant_id = _approved(store)
    token = grant_id.removeprefix("g-")
    assert len(bytes.fromhex(token)) >= 16


def test_the_store_rereads_when_the_operator_file_changes(store: GrantStore) -> None:
    grant_id = _approved(store)
    # Simulate the operator revoking from another process: rewrite the file.
    raw = json.loads((store.directory / "grants.json").read_text())
    raw["grants"][0]["revoked"] = True
    import os
    import time

    (store.directory / "grants.json").write_text(json.dumps(raw))
    os.utime(store.directory / "grants.json", (time.time() + 2, time.time() + 2))
    verdict = store.verify_and_consume(
        grant_id=grant_id, command="move_plate", params_digest="sha256:aa", now=NOW
    )
    assert verdict.reason == "revoked"
