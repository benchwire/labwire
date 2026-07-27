"""JSON-RPC session layer: correlation, dispatch, timeouts, shutdown.

A :class:`JsonRpcSession` owns one transport end. A background reader routes
incoming responses to pending request futures and dispatches incoming
requests/notifications to registered handlers without blocking the reader.

Example:
    >>> from labwire.core.session import JsonRpcSession
    >>> from labwire.core.transport import MemoryTransport
    >>> ours, theirs = MemoryTransport.pair()
    >>> session = JsonRpcSession(ours)  # start with `async with session:`
"""

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Self

from labwire.core.errors import InternalError, LabwireError, error_from_wire
from labwire.core.transport import Transport, TransportClosed
from labwire.core.types import (
    JsonRpcError,
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    parse_message,
)

logger = logging.getLogger("labwire.session")

RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class SessionClosed(LabwireError):
    """Raised by pending and future calls once the session has closed."""

    code = -32008
    category = "internal"


class JsonRpcSession:
    """One JSON-RPC 2.0 session over a :class:`~labwire.core.transport.Transport`.

    Example:
        >>> async def use(session: JsonRpcSession) -> None:
        ...     async with session:
        ...         result = await session.request("ping", {})
    """

    def __init__(
        self,
        transport: Transport,
        *,
        request_handler: RequestHandler | None = None,
        notification_handler: NotificationHandler | None = None,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self._transport = transport
        self._request_handler = request_handler
        self._notification_handler = notification_handler
        self._on_closed = on_closed
        self._on_closed_fired = False
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def __aenter__(self) -> Self:
        self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def start(self) -> None:
        """Start the background reader; idempotent."""
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop())

    async def request(self, method: str, params: dict[str, Any], *, timeout: float = 30.0) -> Any:
        """Send a request and await its result.

        Raises the typed :class:`LabwireError` for error responses,
        :class:`TimeoutError` after ``timeout`` seconds, and
        :class:`SessionClosed` if the session closes first.

        Example:
            >>> # result = await session.request("instrument/describe", {})
        """
        if self._closed:
            raise SessionClosed("session is closed")
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            message = JsonRpcRequest(id=request_id, method=method, params=params)
            await self._transport.send(message.model_dump(mode="json"))
            async with asyncio.timeout(timeout):
                return await future
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a notification (no response expected).

        Example:
            >>> # await session.notify("notifications/initialized", {})
        """
        if self._closed:
            raise SessionClosed("session is closed")
        message = JsonRpcNotification(method=method, params=params)
        await self._transport.send(message.model_dump(mode="json"))

    async def wait_closed(self) -> None:
        """Wait until the session's reader has ended (transport EOF or close).

        Caller cancellation propagates normally; the reader's own outcome is
        never raised here.

        Example:
            >>> # await session.wait_closed()
        """
        if self._reader is not None:
            await asyncio.wait({self._reader})

    async def close(self) -> None:
        """Close the session: stop the reader, fail pending requests, close transport."""
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        handler_tasks = list(self._handler_tasks)
        for task in handler_tasks:
            task.cancel()
        if handler_tasks:
            await asyncio.gather(*handler_tasks, return_exceptions=True)
        self._teardown(SessionClosed("session is closed"))
        await self._transport.close()

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._transport.receive()
                self._dispatch(raw)
        except TransportClosed:
            self._closed = True
            self._teardown(SessionClosed("transport closed"))
        except Exception:
            logger.exception("session reader failed; closing session")
            self._closed = True
            self._teardown(SessionClosed("session reader failed"))

    def _teardown(self, exc: LabwireError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        if self._on_closed is not None and not self._on_closed_fired:
            self._on_closed_fired = True
            try:
                self._on_closed()
            except Exception:
                logger.exception("session on_closed callback failed")

    def _dispatch(self, raw: dict[str, Any]) -> None:
        try:
            message = parse_message(raw)
        except Exception as exc:
            # Invalid envelope: answer -32600 (SPEC §12.1). Echo the id when
            # it is a valid integer; JSON-RPC prescribes id null otherwise.
            if "id" in raw:
                raw_id = raw.get("id")
                error = JsonRpcError(code=-32600, message=f"invalid request: {exc}")
                self._spawn(self._send_error(raw_id if isinstance(raw_id, int) else None, error))
            return
        match message:
            case JsonRpcResponse():
                future = self._pending.get(message.id)
                if future is not None and not future.done():
                    future.set_result(message.result)
            case JsonRpcErrorResponse():
                if message.id is not None:
                    future = self._pending.get(message.id)
                    if future is not None and not future.done():
                        future.set_exception(error_from_wire(message.error))
            case JsonRpcRequest():
                self._spawn(self._handle_request(message))
            case JsonRpcNotification():
                if self._notification_handler is not None:
                    self._spawn(self._notification_handler(message.method, message.params))

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._handler_tasks.add(task)
        task.add_done_callback(self._reap)

    def _reap(self, task: asyncio.Task[None]) -> None:
        self._handler_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and not isinstance(exc, TransportClosed | SessionClosed):
            logger.error("session handler task failed", exc_info=exc)

    async def _handle_request(self, message: JsonRpcRequest) -> None:
        error: JsonRpcError
        if self._request_handler is None:
            error = JsonRpcError(code=-32601, message=f"method not found: {message.method}")
            await self._send_error(message.id, error)
            return
        try:
            result = await self._request_handler(message.method, message.params)
        except LabwireError as exc:
            await self._send_error(message.id, exc.to_wire())
        except Exception:
            logger.exception("request handler crashed for method %s", message.method)
            await self._send_error(message.id, InternalError("internal server error").to_wire())
        else:
            reply = JsonRpcResponse(id=message.id, result=result)
            with contextlib.suppress(TransportClosed):  # peer gone: response undeliverable
                await self._transport.send(reply.model_dump(mode="json"))

    async def _send_error(self, request_id: int | None, error: JsonRpcError) -> None:
        reply = JsonRpcErrorResponse(id=request_id, error=error)
        with contextlib.suppress(TransportClosed):  # peer gone: response undeliverable
            await self._transport.send(reply.model_dump(mode="json", exclude_none=True))
