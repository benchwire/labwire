"""A hardware-free Synapse device for the bridge's tests.

Everything here runs against ``synapse-sim``, the simulator that ships with
``science-synapse``. It is started as a subprocess on an ephemeral port, one
per test, so no test can inherit another's signal chain: a Synapse device has
exactly one chain and one lifecycle, and a rejected configure leaves it with
the chain wiped, which is not state to hand the next test.

The whole suite skips cleanly when ``science-synapse`` is absent, which is how
normal CI stays green without it. The branch-local
``synapse-bridge-experimental`` job installs it deliberately.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

synapse = pytest.importorskip(
    "synapse", reason="needs science-synapse (the synapse-bridge-experimental CI job)"
)

from labwire.bridges.synapse import SynapseInstrument  # noqa: E402
from labwire.core import (  # noqa: E402
    GrantStore,
    InstrumentServer,
    LabwireClient,
    MemoryTransport,
)
from synapse_sim_support import SERIAL, running_simulator  # noqa: E402

CONFIRMATION = "synapse-operator-confirmation"


@pytest.fixture
def sim() -> Iterator[str]:
    """A running ``synapse-sim``; yields its ``host:port`` URI."""
    with running_simulator(synapse) as uri:
        yield uri


@pytest.fixture
def device(sim: str) -> Any:
    """A ``synapse.Device`` pointed at the simulator."""
    return synapse.Device(sim)


@pytest.fixture
def instrument(device: Any) -> Any:
    """The bridge instrument, built from a live device."""
    return SynapseInstrument(device, telemetry_window_s=0.2)


@pytest.fixture
def grants(tmp_path: Path) -> GrantStore:
    """A grant store bound to the simulator's serial.

    Constructed here rather than reached for through the server, because the
    tests need to approve a pending request and there is deliberately no
    protocol method that mints a grant (SPEC §8.6).
    """
    return GrantStore(tmp_path / "grants", serial_number=SERIAL)


@pytest.fixture
async def served(
    instrument: Any, grants: GrantStore
) -> AsyncIterator[tuple[Any, InstrumentServer, LabwireClient]]:
    """The instrument served over the protocol, with a client attached."""
    server = InstrumentServer(
        instrument,
        confirmation_token=CONFIRMATION,
        grant_store=grants,
    )
    await server.start()
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        yield instrument, server, client
    await server.aclose()
