"""Client SDK: discover, command, and stream any Labwire instrument.

Example:
    >>> # async with LabwireClient.attach(transport) as client:
    >>> #     desc = await client.describe()
    >>> #     handle = await client.submit("spin", {"target_rpm": 300.0})
    >>> #     result = await handle.result(timeout=60)
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, Self

from labwire.core._meta import PROTOCOL_VERSION
from labwire.core.capabilities import InstrumentDescriptor
from labwire.core.errors import (
    CanceledError,
    MethodNotFoundError,
    UnsupportedError,
    error_from_wire,
)
from labwire.core.messages import (
    TERMINAL_STATES,
    CommandStatus,
    EventNotification,
    InitializeResult,
    PeerInfo,
    ResourceReadResult,
    ServerCapabilities,
    SubmitResult,
    SubscribeResult,
    TelemetryNotification,
)
from labwire.core.session import JsonRpcSession, SessionClosed
from labwire.core.transport import Transport

logger = logging.getLogger("labwire.client")


class TelemetrySample(TelemetryNotification):
    """One delivered telemetry sample (SPEC §9.2).

    Example:
        >>> TelemetrySample(
        ...     subscription_id="sub-1",
        ...     channel="mass",
        ...     seq=1,
        ...     timestamp="2026-07-23T15:30:00.000000Z",
        ...     value=12.5,
        ... ).channel
        'mass'
    """


class CommandHandle:
    """A submitted run: watch its status pushes, await its result, cancel it.

    Example:
        >>> # handle = await client.submit("dispense", {"volume_ul": 500.0})
        >>> # async for status in handle.updates(): ...
        >>> # result = await handle.result(timeout=60)
    """

    _END: CommandStatus | None = None  # sentinel slot type for the updates queue

    def __init__(self, client: "LabwireClient", command_id: str) -> None:
        self.command_id = command_id
        self._client = client
        self._updates: asyncio.Queue[CommandStatus | None] = asyncio.Queue()
        self._terminal: asyncio.Future[CommandStatus] = asyncio.get_running_loop().create_future()

    def _push(self, status: CommandStatus) -> None:
        self._updates.put_nowait(status)
        if status.status in TERMINAL_STATES and not self._terminal.done():
            self._terminal.set_result(status)

    def _close(self, exc: SessionClosed) -> None:
        # Session gone mid-run: fail result() and end updates() instead of hanging.
        if not self._terminal.done():
            self._terminal.set_exception(exc)
            self._terminal.exception()  # mark retrieved: no GC warning if unawaited
        self._updates.put_nowait(None)

    async def updates(self) -> AsyncIterator[CommandStatus]:
        """Yield pushed status updates, ending with the terminal one.

        Ends early (without a terminal status) if the session closes.

        Example:
            >>> # async for status in handle.updates():
            >>> #     print(status.status, status.progress)
        """
        while True:
            status = await self._updates.get()
            if status is None:
                return
            yield status
            if status.status in TERMINAL_STATES:
                return

    async def result(self, *, timeout: float | None = None) -> Any:
        """Await the terminal state and return the command's result.

        Raises the typed :class:`LabwireError` if the run failed,
        :class:`CanceledError` if it was canceled, and
        :class:`SessionClosed` if the session dropped mid-run.

        Example:
            >>> # result = await handle.result(timeout=60)
        """
        async with asyncio.timeout(timeout):
            status = await asyncio.shield(self._terminal)
        if status.status == "succeeded":
            return status.result
        if status.error is not None:
            raise error_from_wire(status.error)
        raise CanceledError(f"run {self.command_id} was canceled")

    async def status(self) -> CommandStatus:
        """Poll the server for the current status (SPEC §8.2).

        Example:
            >>> # status = await handle.status()
        """
        raw = await self._client._request("command/status", {"command_id": self.command_id})  # pyright: ignore[reportPrivateUsage]
        return CommandStatus.model_validate(raw)

    async def cancel(self) -> CommandStatus:
        """Initiate cancellation (SPEC §8.3); terminal state arrives by push.

        Example:
            >>> # await handle.cancel()
        """
        raw = await self._client._request("command/cancel", {"command_id": self.command_id})  # pyright: ignore[reportPrivateUsage]
        return CommandStatus.model_validate(raw)


class TelemetrySubscription:
    """Async context manager + iterator over one telemetry subscription.

    Iteration ends (``StopAsyncIteration``) when the subscription is exited
    or the session closes.

    Example:
        >>> # async with client.telemetry(["mass"]) as sub:
        >>> #     async for sample in sub: ...
    """

    def __init__(
        self, client: "LabwireClient", channels: list[str], max_rate_hz: float | None
    ) -> None:
        self._client = client
        self._channels = channels
        self._max_rate_hz = max_rate_hz
        self._queue: asyncio.Queue[TelemetrySample | None] = asyncio.Queue()
        self.subscription_id: str | None = None

    async def __aenter__(self) -> Self:
        params: dict[str, Any] = {"channels": self._channels}
        if self._max_rate_hz is not None:
            params["max_rate_hz"] = self._max_rate_hz
        raw = await self._client._request("telemetry/subscribe", params)  # pyright: ignore[reportPrivateUsage]
        result = SubscribeResult.model_validate(raw)
        self.subscription_id = result.subscription_id
        self._client._telemetry_queues[result.subscription_id] = self._queue  # pyright: ignore[reportPrivateUsage]
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self.subscription_id is not None:
            self._client._telemetry_queues.pop(self.subscription_id, None)  # pyright: ignore[reportPrivateUsage]
            self._queue.put_nowait(None)  # end any pending/future iteration
            with contextlib.suppress(Exception):  # session may already be gone
                await self._client._request(  # pyright: ignore[reportPrivateUsage]
                    "telemetry/unsubscribe", {"subscription_id": self.subscription_id}
                )

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> TelemetrySample:
        sample = await self._queue.get()
        if sample is None:
            self._queue.put_nowait(None)  # keep the stream ended for re-iteration
            raise StopAsyncIteration
        return sample


class EventStream:
    """Async iterator over pushed instrument events (SPEC §11).

    Registered at creation time, so no events are missed between creating
    the stream and first iterating it. Call :meth:`close` (or exit the
    ``async with`` block) to stop receiving; iteration also ends when the
    session closes.

    Example:
        >>> # async with client.events() as events:
        >>> #     event = await anext(events)
    """

    def __init__(self, client: "LabwireClient") -> None:
        self._client = client
        self._queue: asyncio.Queue[EventNotification | None] = asyncio.Queue()
        client._event_queues.append(self._queue)  # pyright: ignore[reportPrivateUsage]

    def close(self) -> None:
        """Deregister the stream; pending iteration ends."""
        queues = self._client._event_queues  # pyright: ignore[reportPrivateUsage]
        if self._queue in queues:
            queues.remove(self._queue)
        self._queue.put_nowait(None)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.close()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> EventNotification:
        event = await self._queue.get()
        if event is None:
            self._queue.put_nowait(None)
            raise StopAsyncIteration
        return event


class LabwireClient:
    """An Agent Client for one Instrument Server (SPEC §2).

    Use :meth:`attach` for an existing transport (tests, in-process) or
    :meth:`connect` for a WebSocket URL; both are entered with
    ``async with``, which performs the SPEC §6.1 handshake.

    Example:
        >>> # async with await LabwireClient.connect("ws://127.0.0.1:9520") as client:
        >>> #     print((await client.describe()).identity.model)
    """

    def __init__(self, transport: Transport, *, client_name: str) -> None:
        self._session = JsonRpcSession(
            transport,
            request_handler=self._on_request,
            notification_handler=self._on_notification,
            on_closed=self._on_session_closed,
        )
        self._client_name = client_name
        self._handles: dict[str, CommandHandle] = {}
        self._telemetry_queues: dict[str, asyncio.Queue[TelemetrySample | None]] = {}
        self._event_queues: list[asyncio.Queue[EventNotification | None]] = []
        self.server_info: PeerInfo | None = None
        self.capabilities: ServerCapabilities | None = None

    @classmethod
    def attach(
        cls, transport: Transport, *, client_name: str = "labwire-client"
    ) -> "LabwireClient":
        """Wrap an already-open transport (memory pair, custom framing).

        Example:
            >>> # client = LabwireClient.attach(client_end)
            >>> # async with client: ...
        """
        return cls(transport, client_name=client_name)

    @classmethod
    async def connect(cls, url: str, *, client_name: str = "labwire-client") -> "LabwireClient":
        """Open a WebSocket connection to an Instrument Server (SPEC §5.1).

        Example:
            >>> # async with await LabwireClient.connect("ws://127.0.0.1:9520") as client:
            >>> #     await client.ping()
        """
        from labwire.core.transport.websocket import WebSocketTransport

        return cls(await WebSocketTransport.connect(url), client_name=client_name)

    async def __aenter__(self) -> Self:
        self._session.start()
        try:
            raw = await self._session.request(
                "initialize",
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "client_info": {"name": self._client_name, "version": "0.1.0"},
                    "capabilities": {},
                },
            )
            result = InitializeResult.model_validate(raw)
            if result.protocol_version != PROTOCOL_VERSION:
                raise UnsupportedError(
                    f"server speaks protocol {result.protocol_version!r}, "
                    f"client speaks {PROTOCOL_VERSION!r}"
                )
            self.server_info = result.server_info
            self.capabilities = result.capabilities
            await self._session.notify("notifications/initialized", {})
        except BaseException:
            # __aexit__ never runs when __aenter__ raises: close here or leak
            # the reader task and (for connect()) the open WebSocket.
            await self._session.close()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the session and transport."""
        await self._session.close()

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        return await self._session.request(method, params)

    async def ping(self) -> None:
        """Liveness check (SPEC §6.3).

        Example:
            >>> # await client.ping()
        """
        await self._request("ping", {})

    async def describe(self) -> InstrumentDescriptor:
        """Fetch the instrument's capability descriptor (SPEC §7).

        Example:
            >>> # desc = await client.describe()
        """
        raw = await self._request("instrument/describe", {})
        return InstrumentDescriptor.model_validate(raw)

    async def submit(
        self,
        command: str,
        params: dict[str, Any],
        *,
        confirmation: str | None = None,
        authorization: str | None = None,
        if_revision: dict[str, str] | None = None,
    ) -> CommandHandle:
        """Submit a command and return a handle to its run (SPEC §8.2).

        ``confirmation`` is REQUIRED for ``S2`` commands, and
        ``authorization`` (an operator grant id) for ``S3`` (SPEC §8.6); a
        confirmation never satisfies ``S3``. ``if_revision`` maps resource
        URIs to the revisions the plan was made against (SPEC §10.5).

        Example:
            >>> # handle = await client.submit("dispense", {"volume_ul": 500.0},
            >>> #                              confirmation="standing-grant")
        """
        payload: dict[str, Any] = {"command": command, "params": params}
        if confirmation is not None:
            payload["confirmation"] = confirmation
        if authorization is not None:
            payload["authorization"] = {"grant_id": authorization}
        if if_revision:
            payload["if_revision"] = dict(if_revision)
        raw = await self._request("command/submit", payload)
        result = SubmitResult.model_validate(raw)
        handle = CommandHandle(self, result.command_id)
        self._handles[result.command_id] = handle
        return handle

    async def read_resource(self, uri: str) -> ResourceReadResult:
        """Read a declared resource: index, content, and revision (SPEC §10.2).

        Example:
            >>> # deck = await client.read_resource("labwire:deck")
            >>> # deck.revision
        """
        raw = await self._request("resource/read", {"uri": uri})
        return ResourceReadResult.model_validate(raw)

    def telemetry(
        self, channels: list[str], *, max_rate_hz: float | None = None
    ) -> TelemetrySubscription:
        """Subscribe to telemetry channels for the ``async with`` body (SPEC §9).

        Example:
            >>> # async with client.telemetry(["mass"], max_rate_hz=5.0) as sub:
            >>> #     async for sample in sub: ...
        """
        return TelemetrySubscription(self, channels, max_rate_hz)

    def events(self) -> EventStream:
        """Stream instrument events as they arrive (SPEC §11).

        Example:
            >>> # async with client.events() as events:
            >>> #     async for event in events: ...
        """
        return EventStream(self)

    async def _on_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":  # SPEC §6.3: either party answers ping
            return {}
        raise MethodNotFoundError(f"method not found: {method}")

    def _on_session_closed(self) -> None:
        # Nothing pushed will ever arrive again: unblock every consumer.
        exc = SessionClosed("session closed with runs or streams still open")
        for handle in self._handles.values():
            handle._close(exc)  # pyright: ignore[reportPrivateUsage]
        for queue in self._telemetry_queues.values():
            queue.put_nowait(None)
        for event_queue in self._event_queues:
            event_queue.put_nowait(None)

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        try:
            if method == "notifications/command_status":
                status = CommandStatus.model_validate(params)
                handle = self._handles.get(status.command_id)
                if handle is not None:
                    handle._push(status)  # pyright: ignore[reportPrivateUsage]
                    if status.status in TERMINAL_STATES:
                        # prune: long agent sessions must not grow per submit
                        self._handles.pop(status.command_id, None)
            elif method == "notifications/telemetry":
                sample = TelemetrySample.model_validate(params)
                queue = self._telemetry_queues.get(sample.subscription_id)
                if queue is not None:
                    queue.put_nowait(sample)
            elif method == "notifications/event":
                event = EventNotification.model_validate(params)
                for event_queue in self._event_queues:
                    event_queue.put_nowait(event)
        except Exception:
            logger.exception("malformed %s notification ignored", method)
