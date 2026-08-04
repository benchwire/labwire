"""Constants and the launcher for the shipped ``synapse-sim`` simulator.

Kept out of ``conftest.py`` on purpose. Several packages in this workspace have
a ``conftest.py``, they all import as the module name ``conftest``, and a test
module doing ``from conftest import ...`` would take whichever one reached
``sys.modules`` first. This module's name is unique in the repository, so the
import is unambiguous.
"""

import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

CONFIRMATION = "synapse-operator-confirmation"
"""The S2 confirmation token the test server accepts."""

SERIAL = "SIM-LABWIRE-0001"
"""The simulator's serial, which the grant store is bound to."""

DEVICE_NAME = "labwire-synapse-sim"
"""The simulator's device name, which becomes the instrument model."""

READY_TIMEOUT_S = 30.0
"""How long to wait for the simulator to answer ``Info()``."""


def free_port() -> int:
    """One TCP port that was free a moment ago.

    There is no way to hand a bound socket to the simulator: it takes a port
    number on the command line, so this is the usual small race, small enough
    to live with.

    Example:
        >>> isinstance(free_port(), int)
        True
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def running_simulator(synapse: Any) -> Generator[str]:
    """Run one ``synapse-sim`` subprocess and yield its ``host:port`` URI.

    Readiness is a real ``Info()`` round trip rather than a sleep, because a
    bound port is not a served device.

    Example:
        >>> # with running_simulator(synapse) as uri: ...
    """
    port = free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "synapse.simulator",
            "--iface-ip",
            "127.0.0.1",
            "--rpc-port",
            str(port),
            "--discovery-port",
            str(free_port()),
            "--name",
            DEVICE_NAME,
            "--serial",
            SERIAL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    uri = f"127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            if process.poll() is not None:
                _, err = process.communicate()
                raise RuntimeError(
                    f"synapse-sim exited before it was ready: {err.decode()[-2000:]}"
                )
            if synapse.Device(uri).info() is not None:
                break
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"synapse-sim did not answer Info() on {uri} within {READY_TIMEOUT_S}s"
                )
            time.sleep(0.05)
        yield uri
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged simulator
            process.kill()
            process.wait(timeout=10)
