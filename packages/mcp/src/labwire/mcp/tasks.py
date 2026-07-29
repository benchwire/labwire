"""The io.modelcontextprotocol/tasks extension, hand-implemented.

The Python SDK v2 does not ship this extension ("The tasks extension
(SEP-2663) is not part of this release"), so this module implements it
from the extension's own spec text (modelcontextprotocol/ext-tasks,
specification/draft/tasks.md). The 2026-07-28 changelog calls tasks an
official extension while the ext-tasks repository labels itself
experimental; this implementation treats the text as less stable than
core and says so in the README. TODO-VERIFY: the extension's final
official-vs-experimental status.

Status mapping, stated plainly because two points will surprise readers:

- Task ``failed`` is reserved by the extension for JSON-RPC faults ONLY.
  An instrument-level failure (hardware fault, interlock) maps to task
  ``completed`` with ``is_error: true`` INSIDE the CallToolResult.
- A ``cancelled`` task carries no result field, so when a client's
  tasks/cancel ends a run, the settlement outcome is summarized in
  ``statusMessage`` and the signed bundle stays on the instrument host;
  every other terminal path delivers the full record via ``completed``.
- ``tasks/cancel`` is cooperative by spec: the ack promises nothing. A
  command declared ``cancel_semantics: "none"`` acks the cancel and
  keeps running, exactly as the extension allows, and terminates
  ``completed``; abort and between_steps commands get a real Labwire
  cancel and settle per SPEC 8.3.
- Labwire's accepted/running/canceling states all surface as ``working``
  (the extension has no queued or cancel-pending status); the truth
  lives in ``statusMessage``.

Task state is in-process: an adapter restart loses task handles (the
signed bundles do not vanish; they are on the instrument host). The
stdio adapter is one process and the README says so.
"""

import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from mcp_types import RequestParams, Result

from mcp import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult

EXTENSION_ID = "io.modelcontextprotocol/tasks"
DEFAULT_TTL_MS = 30 * 60 * 1000
POLL_INTERVAL_MS = 500


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class CreateTaskWire(Result):
    """The flat CreateTaskResult of the extension (resultType "task")."""

    result_type: Literal["task"] = "task"
    task_id: str
    status: str
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    ttl_ms: int | None
    poll_interval_ms: int | None = None


class TaskDetailWire(Result):
    """The DetailedTask returned by tasks/get (resultType "complete")."""

    result_type: Literal["complete"] = "complete"
    task_id: str
    status: str
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    ttl_ms: int | None
    poll_interval_ms: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class TaskAckWire(Result):
    """The empty ack of tasks/update and tasks/cancel."""

    result_type: Literal["complete"] = "complete"


class TaskIdParams(RequestParams):
    """Params carrying one taskId."""

    task_id: str


class TaskUpdateParams(RequestParams):
    """tasks/update params; input_responses unused here (see module doc)."""

    task_id: str
    input_responses: dict[str, Any] | None = None


@dataclass
class TaskRecord:
    """One live or terminal task."""

    task_id: str
    created_at: str
    last_updated_at: str
    ttl_ms: int
    status: str = "working"
    status_message: str | None = None
    result: CallToolResult | None = None
    error: dict[str, Any] | None = None
    cancel_semantics: str = "none"
    runner: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    labwire_cancel: Any = None  # async callable initiating the instrument cancel

    def expired(self) -> bool:
        """Whether the TTL has elapsed since creation."""
        born = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        age_ms = (datetime.now(UTC) - born).total_seconds() * 1000.0
        return age_ms > self.ttl_ms

    def touch(self) -> None:
        """Refresh last_updated_at to now."""
        self.last_updated_at = _now()


class TaskStore:
    """In-process task table with bearer-entropy ids and TTL purging."""

    def __init__(self, ttl_ms: int = DEFAULT_TTL_MS) -> None:
        self._ttl_ms = ttl_ms
        self._tasks: dict[str, TaskRecord] = {}

    def create(self, cancel_semantics: str, message: str) -> TaskRecord:
        """A durably-registered record; exists before the result returns."""
        now = _now()
        record = TaskRecord(
            # Spec: ids are bearer tokens and MUST resist enumeration.
            task_id=f"task-{secrets.token_urlsafe(24)}",
            created_at=now,
            last_updated_at=now,
            ttl_ms=self._ttl_ms,
            status_message=message,
            cancel_semantics=cancel_semantics,
        )
        self._tasks[record.task_id] = record
        return record

    def get(self, task_id: str) -> TaskRecord:
        """Look up a task; expired or unknown ids are -32602 per the spec."""
        record = self._tasks.get(task_id)
        if record is not None and record.expired():
            del self._tasks[task_id]
            record = None
        if record is None:
            raise MCPError(code=INVALID_PARAMS, message=f"unknown or expired task: {task_id}")
        return record


def create_wire(record: TaskRecord) -> CreateTaskWire:
    """The CreateTaskResult for a fresh task."""
    return CreateTaskWire(
        task_id=record.task_id,
        status=record.status,
        status_message=record.status_message,
        created_at=record.created_at,
        last_updated_at=record.last_updated_at,
        ttl_ms=record.ttl_ms,
        poll_interval_ms=POLL_INTERVAL_MS,
    )


def detail_wire(record: TaskRecord) -> TaskDetailWire:
    """The DetailedTask for tasks/get: status-specific fields inlined."""
    result_payload: dict[str, Any] | None = None
    if record.status == "completed" and record.result is not None:
        result_payload = record.result.model_dump(mode="json", by_alias=True, exclude_none=True)
    return TaskDetailWire(
        task_id=record.task_id,
        status=record.status,
        status_message=record.status_message,
        created_at=record.created_at,
        last_updated_at=record.last_updated_at,
        ttl_ms=record.ttl_ms,
        poll_interval_ms=POLL_INTERVAL_MS,
        result=result_payload,
        error=record.error if record.status == "failed" else None,
    )
