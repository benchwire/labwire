"""The operator grant store (SPEC §8.6): S3 authorization an agent cannot mint.

A grant is a record in a directory the server reads and an operator tool
writes. The protocol has no method that touches this store, which is the
entire point: whatever an agent can do over the wire, minting authorization
is not part of it.

Three files, with deliberately separated writers:

- ``grants.json``: written only by the operator tool (``labwire grant``).
  The server reads it, re-reading on mtime change, and never writes it.
- ``pending.jsonl``: written only by the server. A refused S3 submission
  records what was asked, so the operator's approval tool reads the real
  command and parameters from the server's own record, never from a digest
  relayed through the agent that wants the approval.
- ``uses.json``: written only by the server. Use counts live here rather
  than in ``grants.json`` so neither writer ever touches the other's file.

Example:
    >>> # store = GrantStore(Path(os.environ["LABWIRE_GRANT_STORE"]))
"""

import contextlib
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

GrantRefusal = Literal[
    "absent",
    "unsupported_scheme",
    "unknown",
    "command_mismatch",
    "params_mismatch",
    "instrument_mismatch",
    "not_yet_valid",
    "expired",
    "exhausted",
    "revoked",
]
"""SPEC §8.6 refusal reasons, in the order they are checked."""

PENDING_CAP = 64
PENDING_TTL = timedelta(minutes=15)


def _parse_when(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class Grant:
    """One provisioned grant, as read from ``grants.json``.

    Example:
        >>> # grant.max_uses
    """

    grant_id: str
    command: str
    params_digest: str
    not_before: str
    expires_at: str
    max_uses: int
    revoked: bool = False
    request_id: str | None = None
    issued_by: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class GrantVerdict:
    """The outcome of presenting a grant id for one submission.

    ``use_index`` is 1-based and set only on success, for the manifest.

    Example:
        >>> # verdict.reason
    """

    ok: bool
    reason: GrantRefusal | None = None
    grant: Grant | None = None
    use_index: int | None = None


@dataclass
class PendingRequest:
    """A refused S3 submission, recorded for the operator to inspect.

    Example:
        >>> # pending.request_id
    """

    request_id: str
    command: str
    params: dict[str, Any]
    params_digest: str
    serial_number: str
    requested_at: str
    expires_at: str


class GrantStore:
    """File-backed grant store; see the module docstring for the layout.

    Verification and consumption are synchronous and hold no awaits, so a
    caller that checks and consumes without yielding cannot race another
    submission in the same event loop; cross-process safety comes from the
    use file being written before the run is created.

    Example:
        >>> # GrantStore(Path("/etc/labwire/grants"), serial_number="rig-1")
    """

    def __init__(self, directory: Path, *, serial_number: str) -> None:
        self.directory = Path(directory)
        self.serial_number = serial_number
        self.directory.mkdir(parents=True, exist_ok=True)
        self._grants_path = self.directory / "grants.json"
        self._pending_path = self.directory / "pending.jsonl"
        self._uses_path = self.directory / "uses.json"
        self._grants: dict[str, Grant] = {}
        self._grants_mtime: float | None = None
        self._store_serial: str | None = None

    # -- grants.json (operator-written; server read-only) --------------------

    def _refresh(self) -> None:
        try:
            mtime = self._grants_path.stat().st_mtime
        except FileNotFoundError:
            self._grants, self._grants_mtime, self._store_serial = {}, None, None
            return
        if mtime == self._grants_mtime:
            return
        raw = cast("dict[str, Any]", json.loads(self._grants_path.read_text() or "{}"))
        instrument = cast("dict[str, Any]", raw.get("instrument") or {})
        self._store_serial = cast("str | None", instrument.get("serial_number"))
        loaded: dict[str, Grant] = {}
        for entry in cast("list[dict[str, Any]]", raw.get("grants", [])):
            grant = Grant(
                grant_id=str(entry["grant_id"]),
                command=str(entry["command"]),
                params_digest=str(entry["params_digest"]),
                not_before=str(entry.get("not_before", "1970-01-01T00:00:00Z")),
                expires_at=str(entry["expires_at"]),
                max_uses=int(entry.get("max_uses", 1)),
                revoked=bool(entry.get("revoked", False)),
                request_id=entry.get("request_id"),
                issued_by=entry.get("issued_by"),
                note=entry.get("note"),
            )
            loaded[grant.grant_id] = grant
        self._grants = loaded
        self._grants_mtime = mtime

    def _uses(self) -> dict[str, int]:
        try:
            return {str(k): int(v) for k, v in json.loads(self._uses_path.read_text()).items()}
        except (FileNotFoundError, ValueError):
            return {}

    def verify_and_consume(
        self, *, grant_id: str, command: str, params_digest: str, now: datetime
    ) -> GrantVerdict:
        """Check a grant against one submission and, if valid, spend one use.

        The check-and-consume is synchronous with no yields; the use count is
        persisted **before** success is reported, so a spent single-use grant
        stays spent across a restart.

        Example:
            >>> # store.verify_and_consume(grant_id="g-1", command="move_plate",
            >>> #     params_digest="sha256:...", now=clock.now())
        """
        self._refresh()
        grant = self._grants.get(grant_id)
        if grant is None:
            return GrantVerdict(False, "unknown")
        if grant.revoked:
            return GrantVerdict(False, "revoked")
        if grant.command != command:
            return GrantVerdict(False, "command_mismatch")
        if grant.params_digest != params_digest:
            return GrantVerdict(False, "params_mismatch")
        if self._store_serial is not None and self._store_serial != self.serial_number:
            return GrantVerdict(False, "instrument_mismatch")
        not_before = _parse_when(grant.not_before)
        expires_at = _parse_when(grant.expires_at)
        if not_before is not None and now < not_before:
            return GrantVerdict(False, "not_yet_valid")
        if expires_at is None or now >= expires_at:
            return GrantVerdict(False, "expired")
        uses = self._uses()
        used = uses.get(grant_id, 0)
        if used >= grant.max_uses:
            return GrantVerdict(False, "exhausted")
        uses[grant_id] = used + 1
        self._write_atomic(self._uses_path, json.dumps(uses, indent=2) + "\n")
        return GrantVerdict(True, None, grant, used + 1)

    # -- pending.jsonl (server-written) ---------------------------------------

    def record_pending(
        self, *, command: str, params: dict[str, Any], params_digest: str, now: datetime
    ) -> PendingRequest:
        """Record a refused S3 submission for the operator to inspect.

        Capped and expiring, so an agent cannot fill a disk by asking.

        Example:
            >>> # store.record_pending(command="move_plate", params={...},
            >>> #     params_digest="sha256:...", now=clock.now())
        """
        pending = PendingRequest(
            request_id=f"req-{secrets.token_hex(4)}",
            command=command,
            params=params,
            params_digest=params_digest,
            serial_number=self.serial_number,
            requested_at=now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            expires_at=(now + PENDING_TTL).astimezone(UTC).isoformat().replace("+00:00", "Z"),
        )
        alive = [
            entry
            for entry in self.pending(now=now)
            if entry.params_digest != params_digest or entry.command != command
        ][-(PENDING_CAP - 1) :]
        alive.append(pending)
        lines = "".join(json.dumps(vars(entry), sort_keys=True) + "\n" for entry in alive)
        self._write_atomic(self._pending_path, lines)
        return pending

    def pending(self, *, now: datetime) -> list[PendingRequest]:
        """Unexpired pending requests, oldest first.

        Example:
            >>> # store.pending(now=datetime.now(UTC))
        """
        try:
            lines = self._pending_path.read_text().splitlines()
        except FileNotFoundError:
            return []
        alive: list[PendingRequest] = []
        for line in lines:
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            raw.pop("extra", None)
            entry = PendingRequest(**raw)
            expires = _parse_when(entry.expires_at)
            if expires is not None and now < expires:
                alive.append(entry)
        return alive

    def find_pending(self, request_id: str, *, now: datetime) -> PendingRequest | None:
        """Look up one unexpired pending request by id.

        Example:
            >>> # store.find_pending("req-3f1c8d9e", now=datetime.now(UTC))
        """
        for entry in self.pending(now=now):
            if entry.request_id == request_id:
                return entry
        return None

    # -- operator side (used by the CLI, never by the server) -----------------

    def approve(
        self,
        request_id: str,
        *,
        now: datetime,
        ttl: timedelta,
        max_uses: int,
        issued_by: str | None = None,
        note: str | None = None,
    ) -> Grant:
        """Turn a pending request into a grant. Operator-tool code path.

        Example:
            >>> # store.approve("req-3f1c8d9e", now=..., ttl=timedelta(minutes=15),
            >>> #     max_uses=1)
        """
        pending = self.find_pending(request_id, now=now)
        if pending is None:
            raise KeyError(f"no unexpired pending request {request_id!r}")
        grant = Grant(
            grant_id=f"g-{secrets.token_hex(16)}",
            command=pending.command,
            params_digest=pending.params_digest,
            not_before=now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            expires_at=(now + ttl).astimezone(UTC).isoformat().replace("+00:00", "Z"),
            max_uses=max_uses,
            request_id=request_id,
            issued_by=issued_by,
            note=note,
        )
        raw: dict[str, Any] = {"version": 1, "instrument": {"serial_number": self.serial_number}}
        with contextlib.suppress(FileNotFoundError, ValueError):
            raw = json.loads(self._grants_path.read_text())
        raw.setdefault("grants", []).append(
            {k: v for k, v in vars(grant).items() if v is not None and k != "revoked"}
        )
        self._write_atomic(self._grants_path, json.dumps(raw, indent=2) + "\n")
        return grant

    def revoke(self, grant_id: str) -> bool:
        """Mark a grant revoked. Operator-tool code path.

        Example:
            >>> # store.revoke("g-7f2a91c4")
        """
        try:
            raw = json.loads(self._grants_path.read_text())
        except (FileNotFoundError, ValueError):
            return False
        found = False
        for entry in cast("list[dict[str, Any]]", raw.get("grants", [])):
            if entry.get("grant_id") == grant_id:
                entry["revoked"] = True
                found = True
        if found:
            self._write_atomic(self._grants_path, json.dumps(raw, indent=2) + "\n")
        return found

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        scratch = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex[:8]}")
        scratch.write_text(text)
        os.replace(scratch, path)
