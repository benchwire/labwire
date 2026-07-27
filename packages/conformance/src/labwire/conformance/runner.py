"""Connect to a server and run every applicable check."""

import contextlib
from typing import Any

from labwire.conformance._checks import (
    CHECKS,
    CheckContext,
    CheckFailed,
    NotApplicable,
    RunOptions,
    Unexercised,
)
from labwire.conformance._raw import RawWire
from labwire.conformance._report import CheckOutcome, Report, Status
from labwire.core import LabwireClient
from labwire.core.capabilities import InstrumentDescriptor
from labwire.core.messages import ResourceReadResult


async def run_suite(url: str, options: RunOptions | None = None) -> Report:
    """Run the conformance suite against a live server.

    Example:
        >>> # report = await run_suite("ws://127.0.0.1:9500")
        >>> # print(report.render())
    """
    if not url.startswith("ws://") and not url.startswith("wss://"):
        raise ValueError(
            "only WebSocket targets are implemented (ws:// or wss://); the stdio "
            "transport is specified but has no conformance runner yet (SPEC 15.2)"
        )
    options = options or RunOptions()

    async with RawWire(url) as wire:
        await wire.initialize()
        described = await wire.call("instrument/describe", {}, request_id=2)
    raw_descriptor: dict[str, Any] = described.get("result") or {}

    descriptor: InstrumentDescriptor | None
    try:
        descriptor = InstrumentDescriptor.model_validate(raw_descriptor)
    except Exception:
        descriptor = None

    async with await LabwireClient.connect(url, client_name="labwire-conformance") as client:
        resource_reads: dict[str, ResourceReadResult] = {}
        if descriptor is not None:
            for declared in descriptor.resources:
                with contextlib.suppress(Exception):
                    # A failed read is reported by resources.read_each.
                    resource_reads[declared.uri] = await client.read_resource(declared.uri)

        ctx = CheckContext(
            url=url,
            client=client,
            raw_descriptor=raw_descriptor,
            descriptor=descriptor,
            resource_reads=resource_reads,
            options=options,
        )
        report = Report(
            instrument=str(raw_descriptor.get("identity", {}).get("model", "unknown")),
            target=url,
        )
        for check in CHECKS:
            try:
                detail = await check.run(ctx)
                status = Status.PASSED
            except CheckFailed as exc:
                status, detail = Status.FAILED, str(exc)
            except NotApplicable as exc:
                status, detail = Status.NOT_APPLICABLE, str(exc)
            except Unexercised as exc:
                status, detail = Status.UNEXERCISED, str(exc)
            except Exception as exc:
                status, detail = Status.FAILED, f"check crashed: {exc!r}"
            report.add(
                CheckOutcome(
                    check_id=check.check_id,
                    spec=check.spec,
                    level=check.level,
                    status=status,
                    detail=detail,
                )
            )
    return report
