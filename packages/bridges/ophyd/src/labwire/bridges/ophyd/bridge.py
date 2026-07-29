"""Serve a live ophyd Device through the Labwire protocol.

:func:`OphydInstrument` builds a real :class:`labwire.core.Instrument`
subclass from a resolved descriptor, so every v0.2 rule, mandatory UCUM
units, safety-class confirmation, the command lifecycle, applies to a
bridged device exactly as it does to a native one. An under-annotated device
cannot be constructed at all.

ophyd is synchronous, so every device call runs in a worker thread via
``asyncio.to_thread``; the server's event loop is never blocked by a slow
motor. A badly behaved device still occupies a thread, which is documented
rather than hidden (see DESIGN.md).

Example:
    >>> # instrument = OphydInstrument(device, load_annotations(path))
    >>> # await InstrumentServer(instrument).serve_websocket("127.0.0.1", 9520)
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from labwire.bridges.ophyd.annotations import (
    AnnotationFile,
    ResolvedComponent,
    ResolvedInstrument,
    resolve,
)
from labwire.bridges.ophyd.introspect import ComponentRole, introspect
from labwire.core import (
    CanceledError,
    CommandContext,
    DeviceTimeoutError,
    HardwareFaultError,
    Instrument,
    InstrumentServer,
    TelemetryChannel,
    channel,
    command,
)
from pydantic import BaseModel, ConfigDict, create_model

_POLL_S = 0.05
_SETTLE_TIMEOUT_S = 5.0  # how long stop() gets to prove itself (SPEC 8.3)
_STATUS_TIMEOUT_S = 300.0

_PYTHON_TYPES: dict[str, type] = {
    "float64": float,
    "int64": int,
    "bool": bool,
    "string": str,
}


def _plain(value: Any) -> Any:
    """Coerce a device value to a plain JSON type.

    Scientific devices hand back numpy scalars, whose repr is not a JSON
    number. Canonicalization tolerates them (see ``labwire.core.jcs``), but
    the wire and the signed record should carry ordinary values.

    Example:
        >>> _plain(True)
        True
    """
    if isinstance(value, bool):
        return value
    if hasattr(value, "__index__") and not isinstance(value, int):
        return int(value)
    if hasattr(value, "__float__") and not isinstance(value, float | int):
        return float(value)
    if isinstance(value, float | int | str) or value is None:
        return value
    return str(value)


def _coerce(value: Any, dtype: str | None) -> Any:
    """Present a value as the type its channel declares.

    ophyd infers dtype from whatever a signal currently holds, so an axis
    resting at integer zero reads back as an int on a float64 channel.
    """
    try:
        if dtype == "float64":
            return float(value)
        if dtype == "int64":
            return int(value)
        if dtype == "bool":
            return bool(value)
        if dtype == "string":
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return value
    return value


class OphydBridgeBase(Instrument):
    """Shared runtime for bridged ophyd devices.

    Subclasses are generated per device by :func:`OphydInstrument`; this base
    holds the device handle, the polling loop, and the ophyd call plumbing.

    Example:
        >>> # OphydInstrument(device, annotations) returns an instance of a
        >>> # generated subclass of this class.
    """

    resolved: ResolvedInstrument

    def __init__(self, device: Any) -> None:
        super().__init__()
        self._device = device
        self._channel_by_key: dict[str, TelemetryChannel] = {
            spec.name: chan for spec, chan in self._declared_channels()
        }

    def _declared_channels(self) -> list[tuple[Any, TelemetryChannel]]:
        return [(chan.spec, chan) for chan in self._channels.values()]  # pyright: ignore[reportPrivateUsage]

    async def on_start(self, server: InstrumentServer) -> None:
        """Confirm the device answers, then stream its read channels."""
        connect = getattr(self._device, "wait_for_connection", None)
        if callable(connect):
            try:
                await asyncio.to_thread(connect, timeout=5.0)
            except TypeError:
                await asyncio.to_thread(connect)
            except Exception as exc:
                raise HardwareFaultError(f"device did not connect: {exc}") from exc
        server.spawn(self._poll_loop(server))

    async def _poll_loop(self, server: InstrumentServer) -> None:
        while True:
            try:
                readings = await self._read_device()
            except Exception:
                readings = {}
            for key, value in readings.items():
                chan = self._channel_by_key.get(key)
                if chan is not None:
                    chan.publish(value)
            await server.clock.sleep(_POLL_S)

    async def _read_device(self) -> dict[str, Any]:
        raw = await asyncio.to_thread(self._device.read)
        readings = cast("dict[str, Any]", raw)
        dtypes = {c.key: c.dtype for c in self.resolved.components}
        return {
            key: _coerce(_plain(cast("dict[str, Any]", reading).get("value")), dtypes.get(key))
            for key, reading in readings.items()
            if key in self._channel_by_key
        }

    async def _await_status(self, ctx: CommandContext, status: Any, what: str) -> None:
        """Wait for an ophyd status, honoring cancellation honestly.

        Cancellation reaches here only for commands declared
        ``cancel_semantics: "abort"`` (the server refuses the rest). The
        device's stop() is called ONCE, and then the record must earn its
        claim: if the status object resolves unsuccessfully within the
        settlement window, the halt is confirmed; if it resolves
        successfully, completion won the race; if it never resolves, the
        settlement is ``unconfirmed``, because a stop request returning is
        not the same as motion stopping (SPEC-FINDINGS F10). The old
        behavior here reported canceled "whether or not the device obeys
        stop()", which is exactly the lie the field report indicted.
        TODO-VERIFY: the abort path has only ever run against synthetic
        test devices; EpicsMotor.stop() on a real IOC is unexercised.
        """
        waited = 0.0
        stop_sent_at: float | None = None
        stop_error: str | None = None
        while not bool(getattr(status, "done", False)):
            if ctx.cancel_requested and ctx.cancel_semantics == "abort" and stop_sent_at is None:
                stop_sent_at = waited
                stop = getattr(self._device, "stop", None)
                if callable(stop):
                    try:
                        await asyncio.to_thread(stop, success=False)
                    except Exception as exc:
                        stop_error = f"stop() raised {type(exc).__name__}: {exc}"
                else:
                    stop_error = "device has no stop()"
            if stop_sent_at is not None and waited - stop_sent_at >= _SETTLE_TIMEOUT_S:
                raise CanceledError(
                    f"{stop_error or 'stop() returned'} but the {what} status never "
                    f"resolved within {_SETTLE_TIMEOUT_S} s; physical state unconfirmed"
                )
            if waited >= _STATUS_TIMEOUT_S:
                raise DeviceTimeoutError(f"{what} did not complete within {_STATUS_TIMEOUT_S} s")
            await ctx.sleep(_POLL_S)
            waited += _POLL_S
        if not bool(getattr(status, "success", True)):
            if stop_sent_at is not None and stop_error is None:
                ctx.confirm_halted(f"{what} status resolved unsuccessful after stop()")
            failure = None
            exception = getattr(status, "exception", None)
            if callable(exception):
                failure = exception()
            raise HardwareFaultError(f"{what} failed: {failure or 'device reported no success'}")
        # done and successful: fall through and return normally; if a cancel
        # was pending, the server records ran_to_completion.

    async def _move(self, ctx: CommandContext, key: str, value: Any) -> dict[str, Any]:
        """Actuate a positioner through the device's own set()."""
        try:
            status = await asyncio.to_thread(self._device.set, value)
        except Exception as exc:
            raise HardwareFaultError(f"move failed: {exc}") from exc
        await self._await_status(ctx, status, "move")
        readings = await self._read_device()
        return {"value": readings.get(key, _plain(value))}

    async def _set_component(self, ctx: CommandContext, attr: str, value: Any) -> dict[str, Any]:
        signal = getattr(self._device, attr)
        try:
            status = await asyncio.to_thread(signal.set, value)
        except Exception as exc:
            raise HardwareFaultError(f"set {attr} failed: {exc}") from exc
        await self._await_status(ctx, status, f"set {attr}")
        readings = await self._read_device()
        key = next(
            (c.key for c in self.resolved.components if c.attr == attr and c.key in readings),
            None,
        )
        return {"value": _plain(readings[key]) if key else _plain(value)}

    async def _trigger(self, ctx: CommandContext) -> dict[str, Any]:
        trigger = self._device.trigger
        try:
            status = await asyncio.to_thread(trigger)
        except Exception as exc:
            raise HardwareFaultError(f"trigger failed: {exc}") from exc
        await self._await_status(ctx, status, "trigger")
        return await self._read_device()

    async def _stop(self, ctx: CommandContext) -> dict[str, bool]:
        stop = self._device.stop
        try:
            await asyncio.to_thread(stop, success=True)
        except TypeError:
            await asyncio.to_thread(stop)
        except Exception as exc:
            raise HardwareFaultError(f"stop failed: {exc}") from exc
        return {"stopped": True}


def _make_setter(
    resolved_component: ResolvedComponent,
    description: str,
    safety_class: str,
    units: dict[str, str],
) -> Callable[..., Awaitable[Any]]:
    """Build a typed ``set_<attr>`` coroutine the @command decorator accepts."""
    python_type = _PYTHON_TYPES[resolved_component.dtype]
    attr = resolved_component.attr
    result_model = _value_model(f"set_{attr}", python_type)

    async def setter(self: OphydBridgeBase, ctx: CommandContext, value: Any) -> dict[str, Any]:
        return await self._set_component(ctx, attr, value)  # pyright: ignore[reportPrivateUsage]

    setter.__name__ = f"set_{attr}"
    setter.__qualname__ = setter.__name__
    setter.__doc__ = description
    setter.__annotations__ = {"value": python_type, "return": result_model}
    setter.__signature__ = inspect.Signature(  # pyright: ignore[reportFunctionMemberAccess]
        [
            inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            inspect.Parameter(
                "value", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=python_type
            ),
        ],
        return_annotation=result_model,
    )
    return command(
        units=units,
        returns_units={"value": resolved_component.unit},
        safety_class=cast("Any", safety_class),
        description=description,
    )(setter)


def _make_mover(
    resolved_component: ResolvedComponent,
    description: str,
    safety_class: str,
    units: dict[str, str],
    cancel_semantics: str = "none",
) -> Callable[..., Awaitable[Any]]:
    """Build the positioner ``move`` coroutine."""
    python_type = _PYTHON_TYPES[resolved_component.dtype]
    key = resolved_component.key
    result_model = _value_model("move", python_type)

    async def move(self: OphydBridgeBase, ctx: CommandContext, value: Any) -> dict[str, Any]:
        return await self._move(ctx, key, value)  # pyright: ignore[reportPrivateUsage]

    move.__name__ = "move"
    move.__qualname__ = "move"
    move.__doc__ = description
    move.__annotations__ = {"value": python_type, "return": result_model}
    move.__signature__ = inspect.Signature(  # pyright: ignore[reportFunctionMemberAccess]
        [
            inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            inspect.Parameter(
                "value", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=python_type
            ),
        ],
        return_annotation=result_model,
    )
    return command(
        units=units,
        returns_units={"value": resolved_component.unit},
        safety_class=cast("Any", safety_class),
        cancel=cast("Any", cancel_semantics),
        description=description,
    )(move)


def _declare_result(fn: Any, model: type[BaseModel]) -> None:
    """Point a generated coroutine's declared result at a model built at runtime.

    The model does not exist until the device has been introspected, so it
    cannot be written as an annotation; the decorator reads the signature, so
    both the annotation and the signature are replaced.

    Example:
        >>> # _declare_result(read, readings_model)
    """
    fn.__annotations__["return"] = model
    fn.__signature__ = inspect.Signature(
        [
            inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ],
        return_annotation=model,
    )


def _value_model(name: str, python_type: type) -> type[BaseModel]:
    """A closed one-field result model for an actuation command.

    ``move`` and ``set_<attr>`` report where the device landed. Declaring that
    as ``dict[str, float]`` would be an open mapping of quantities; one named
    field says the same thing and can be annotated.

    Example:
        >>> # _value_model("Ophyd_SynAxis_move", float)
    """
    return create_model(
        f"{name}_value", __config__=ConfigDict(extra="forbid"), value=(python_type, ...)
    )


def _readings_model(name: str, components: list[ResolvedComponent]) -> type[BaseModel]:
    """A closed result model naming exactly the channels this device reads.

    ``read`` and ``trigger`` return a value per exposed channel, and the
    channel set is known once the device has been introspected. Declaring it
    as ``dict[str, float]`` would be an open mapping of quantities under names
    the schema never states, which one unit code cannot describe, so the model
    is built from the resolved components instead. The bridge already filtered
    its readings to the declared channels; this makes that discipline part of
    the contract rather than a property of the code.

    Example:
        >>> # _readings_model("Ophyd_SynAxis", resolved.components)
    """
    fields: dict[str, Any] = {
        component.key: (_PYTHON_TYPES[component.dtype], ...) for component in components
    }
    return create_model(f"{name}_readings", __config__=ConfigDict(extra="forbid"), **fields)


def OphydInstrument(
    device: Any,
    annotations: AnnotationFile | None = None,
    *,
    allow_partial: bool = False,
) -> OphydBridgeBase:
    """Build a Labwire instrument backed by a live ophyd device.

    The device is introspected, merged with ``annotations``, and refused
    outright if any quantity still lacks a UCUM unit: the same rule the
    protocol applies to native instruments.

    Example:
        >>> from ophyd.sim import SynAxis
        >>> # instrument = OphydInstrument(SynAxis(name="ax"), annotations)
    """
    draft = introspect(device)
    resolved = resolve(draft, annotations or AnnotationFile(), allow_partial=allow_partial)

    namespace: dict[str, Any] = {
        "identity": resolved.identity,
        "resolved": resolved,
        "__doc__": resolved.description,
    }
    channel_units: dict[str, str] = {}
    for component in resolved.components:
        if component.role is not ComponentRole.CHANNEL:
            continue
        channel_units[component.key] = component.unit
        namespace[f"channel_{component.attr}"] = channel(
            component.key,
            unit=component.unit,
            dtype=component.dtype,
            description=component.description,
            qudt_quantity_kind=component.qudt_quantity_kind,
        )

    readable = [c for c in resolved.components if c.role is ComponentRole.CHANNEL]
    readings_model = _readings_model(f"Ophyd_{resolved.identity.model}", readable)
    by_key = {component.key: component for component in resolved.components}
    for spec in resolved.commands:
        if spec.name == "move" and spec.component_key:
            component = by_key[spec.component_key]
            namespace["move"] = _make_mover(
                component,
                spec.description,
                spec.safety_class,
                {"value": component.unit},
                cancel_semantics=spec.cancel_semantics,
            )
        elif spec.name.startswith("set_") and spec.component_key:
            component = by_key[spec.component_key]
            namespace[spec.name] = _make_setter(
                component, spec.description, spec.safety_class, {"value": component.unit}
            )
        elif spec.name == "trigger":

            async def trigger(self: OphydBridgeBase, ctx: CommandContext) -> Any:
                return await self._trigger(ctx)  # pyright: ignore[reportPrivateUsage]

            trigger.__doc__ = spec.description
            _declare_result(trigger, readings_model)
            namespace["trigger"] = command(
                returns_units=dict(channel_units),
                safety_class=cast("Any", spec.safety_class),
                description=spec.description,
            )(trigger)
        elif spec.name == "stop":

            async def stop(self: OphydBridgeBase, ctx: CommandContext) -> dict[str, bool]:
                return await self._stop(ctx)  # pyright: ignore[reportPrivateUsage]

            stop.__doc__ = spec.description
            namespace["stop"] = command(
                safety_class=cast("Any", spec.safety_class), description=spec.description
            )(stop)
        elif spec.name == "read":

            async def read(self: OphydBridgeBase, ctx: CommandContext) -> Any:
                return await self._read_device()  # pyright: ignore[reportPrivateUsage]

            read.__doc__ = spec.description
            _declare_result(read, readings_model)
            namespace["read"] = command(
                returns_units=dict(channel_units),
                safety_class=cast("Any", spec.safety_class),
                description=spec.description,
            )(read)

    generated = type(f"Ophyd_{resolved.identity.model}", (OphydBridgeBase,), namespace)
    return generated(device)
