"""A simulation of the Opentrons robot-server command layer, for tests.

The Flex driver under test (PyLabRobot PR #1184) talks HTTP to the
robot-server API: health check, empty run creation, instrument
discovery, then one POST plus polling per command. This module fakes
exactly that layer with an httpx ``MockTransport`` handler, so the
tests exercise the driver's real request-building and polling code and
can assert on the precise command stream a robot would have received.
This mirrors how the PR itself was validated ("against a simulation of
the command layer"); nothing here touches hardware and no hardware
behavior is claimed.

The simulation is deliberately stateful and script-able per test:
``fail_types`` makes named commands fail like the API fails them, and
``hold_types`` parks named commands in ``running`` until the test
releases them, which is how the cancel-at-a-boundary tests open their
race window.
"""

import asyncio
import itertools
import json
from typing import Any

import httpx


class FlexServerSim:
    """Stateful fake of the robot-server endpoints the Flex driver uses."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.fail_types: set[str] = set()
        self.hold_types: set[str] = set()
        self.release = asyncio.Event()
        self.stopped = False
        self._ids = itertools.count(1)
        self._status: dict[str, tuple[str, str]] = {}  # id -> (type, status)

    def transport(self) -> httpx.MockTransport:
        """The transport to hand to an ``httpx.AsyncClient``."""
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(
                200,
                json={
                    "name": "simulated-flex",
                    "api_version": "sim-7.0",
                    "robot_model": "OT-3 Standard (simulated)",
                },
            )
        if path == "/instruments":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "instrumentType": "pipette",
                            "mount": "left",
                            "instrumentName": "p1000_single_flex",
                            "instrumentModel": "p1000_single_v3.6",
                            "data": {"channels": 1, "min_volume": 5.0, "max_volume": 1000.0},
                        }
                    ]
                },
            )
        if path == "/runs" and request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run-sim-1"}})
        if path.endswith("/actions") and request.method == "POST":
            self.stopped = True
            return httpx.Response(201, json={"data": {"actionType": "stop"}})
        if request.method == "DELETE":
            return httpx.Response(200, json={"data": {}})
        if path.endswith("/commands") and request.method == "POST":
            body = json.loads(request.content)["data"]
            command_type = body["commandType"]
            params = body.get("params", {})
            self.commands.append((command_type, params))
            command_id = f"cmd-{next(self._ids)}"
            self._status[command_id] = (command_type, "queued")
            return httpx.Response(201, json={"data": {"id": command_id, "status": "queued"}})
        if "/commands/" in path and request.method == "GET":
            command_id = path.rsplit("/", 1)[1]
            command_type, _ = self._status[command_id]
            if command_type in self.fail_types:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": command_id,
                            "status": "failed",
                            "error": {"detail": f"simulated {command_type} failure"},
                        }
                    },
                )
            if command_type in self.hold_types and not self.release.is_set():
                return httpx.Response(200, json={"data": {"id": command_id, "status": "running"}})
            result: dict[str, Any] = {}
            if command_type == "loadPipette":
                result = {"pipetteId": "pipette-sim-1"}
            if command_type == "loadLabware":
                result = {"labwareId": f"labware-{command_id}"}
            return httpx.Response(
                200,
                json={"data": {"id": command_id, "status": "succeeded", "result": result}},
            )
        return httpx.Response(404, json={"detail": f"unhandled: {request.method} {path}"})

    def sent(self, command_type: str) -> list[dict[str, Any]]:
        """Every params payload sent for the given command type, in order."""
        return [params for sent_type, params in self.commands if sent_type == command_type]
