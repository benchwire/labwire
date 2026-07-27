"""The instrument endpoint configuration file.

One YAML file per deployment says which driver speaks to which physical
endpoint over which transport. The loader is strict: unknown keys, unknown
transports, and malformed addresses are errors, because a silently ignored
endpoint field is how a driver ends up pointed at the wrong socket.

Example file (see docs/HARDWARE.md for the walkthrough):

.. code-block:: yaml

    version: 1
    instruments:
      psu:
        driver: labwire.drivers:PowerSupply
        transport: tcp
        address: "10.0.0.5:5025"
      balance:
        driver: labwire.drivers:Balance
        transport: serial
        device: /dev/tty.usbserial-A50
        baud: 9600
        annotation: balance-annotation.yaml
"""

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml
from labwire.drivers._lineproto import LineProtocolClient

_KNOWN_KEYS = {"driver", "transport", "address", "device", "baud", "annotation"}


@dataclass(frozen=True)
class Endpoint:
    """One configured instrument endpoint.

    Example:
        >>> Endpoint("psu", "labwire.drivers:PowerSupply", "tcp",
        ...          address="127.0.0.1:5025").transport
        'tcp'
    """

    name: str
    driver: str
    transport: str
    address: str | None = None
    device: str | None = None
    baud: int = 9600
    annotation: Path | None = None

    def link(self) -> LineProtocolClient:
        """The line-protocol link this endpoint describes."""
        if self.transport == "tcp":
            assert self.address is not None
            host, _, port = self.address.rpartition(":")
            return LineProtocolClient(host, int(port))
        assert self.device is not None
        return LineProtocolClient.serial(self.device, baudrate=self.baud)

    def instrument(self) -> Any:
        """Import the driver and construct it over this endpoint's link."""
        module_name, _, attribute = self.driver.partition(":")
        driver_cls = getattr(importlib.import_module(module_name), attribute)
        return driver_cls(link=self.link())


def load_endpoints(path: Path) -> list[Endpoint]:
    """Parse and validate an endpoint file; every problem is an error.

    Example:
        >>> # endpoints = load_endpoints(Path("labwire-instruments.yaml"))
    """
    raw: Any = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping with 'version: 1'")
    document = cast("dict[str, Any]", raw)
    if document.get("version") != 1:
        raise ValueError(f"{path}: expected a mapping with 'version: 1'")
    instruments_raw: Any = document.get("instruments")
    if not isinstance(instruments_raw, dict) or not instruments_raw:
        raise ValueError(f"{path}: 'instruments' must be a non-empty mapping")
    instruments = cast("dict[str, Any]", instruments_raw)
    extras = set(document) - {"version", "instruments"}
    if extras:
        raise ValueError(f"{path}: unknown top-level keys: {sorted(extras)}")

    endpoints: list[Endpoint] = []
    for name, entry_raw in instruments.items():
        where = f"{path}: instruments.{name}"
        if not isinstance(entry_raw, dict):
            raise ValueError(f"{where}: expected a mapping")
        entry = cast("dict[str, Any]", entry_raw)
        unknown = set(entry) - _KNOWN_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown keys: {sorted(unknown)}")
        driver = entry.get("driver")
        if not isinstance(driver, str) or ":" not in driver:
            raise ValueError(f"{where}: 'driver' must look like 'package.module:ClassName'")
        transport = entry.get("transport")
        if transport not in ("tcp", "serial"):
            raise ValueError(f"{where}: 'transport' must be 'tcp' or 'serial'")
        address = entry.get("address")
        device = entry.get("device")
        if transport == "tcp":
            if not isinstance(address, str) or ":" not in address:
                raise ValueError(f"{where}: tcp needs 'address: host:port'")
            if device is not None:
                raise ValueError(f"{where}: 'device' belongs to serial endpoints")
        else:
            if not isinstance(device, str) or not device:
                raise ValueError(f"{where}: serial needs 'device: /dev/...'")
            if address is not None:
                raise ValueError(f"{where}: 'address' belongs to tcp endpoints")
        baud = entry.get("baud", 9600)
        if not isinstance(baud, int) or baud <= 0:
            raise ValueError(f"{where}: 'baud' must be a positive integer")
        annotation = entry.get("annotation")
        endpoints.append(
            Endpoint(
                name=name,
                driver=driver,
                transport=transport,
                address=address,
                device=device,
                baud=baud,
                annotation=(path.parent / annotation) if isinstance(annotation, str) else None,
            )
        )
    return endpoints
