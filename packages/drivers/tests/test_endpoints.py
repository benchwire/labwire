"""The endpoint file loader: strict, and every problem is an error."""

from pathlib import Path

import pytest
from labwire.drivers import Endpoint, load_endpoints

GOOD = """\
version: 1
instruments:
  psu:
    driver: labwire.drivers:PowerSupply
    transport: tcp
    address: "127.0.0.1:5025"
  balance:
    driver: labwire.drivers:Balance
    transport: serial
    device: /dev/tty.usbserial-A50
    baud: 19200
    annotation: balance-annotation.yaml
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "labwire-instruments.yaml"
    path.write_text(text)
    return path


def test_a_good_file_loads_both_transports(tmp_path: Path) -> None:
    endpoints = {e.name: e for e in load_endpoints(_write(tmp_path, GOOD))}
    assert endpoints["psu"].transport == "tcp"
    assert endpoints["psu"].address == "127.0.0.1:5025"
    assert endpoints["balance"].transport == "serial"
    assert endpoints["balance"].baud == 19200
    assert endpoints["balance"].annotation == tmp_path / "balance-annotation.yaml"


def test_tcp_endpoint_builds_a_link_and_instrument(tmp_path: Path) -> None:
    endpoints = load_endpoints(_write(tmp_path, GOOD))
    psu = next(e for e in endpoints if e.name == "psu")
    instrument = psu.instrument()
    from labwire.drivers import PowerSupply

    assert isinstance(instrument, PowerSupply)


@pytest.mark.parametrize(
    ("broken", "complaint"),
    [
        (GOOD.replace("version: 1", "version: 2"), "version: 1"),
        (GOOD.replace("transport: tcp", "transport: gpib"), "'tcp' or 'serial'"),
        (GOOD.replace('address: "127.0.0.1:5025"', "address: nocolon"), "host:port"),
        (GOOD.replace("device: /dev/tty.usbserial-A50", "address: 1.2.3.4:1"), "serial needs"),
        (GOOD.replace("driver: labwire.drivers:Balance", "driver: nodots"), "ClassName"),
        (GOOD + "extra: true\n", "unknown top-level"),
        (GOOD + "  rogue: true\n", "expected a mapping"),
        (GOOD.replace("baud: 19200", "baud: -1"), "positive"),
        (GOOD.replace("    baud: 19200\n", "    baud: 19200\n    typo_key: x\n"), "unknown keys"),
    ],
)
def test_every_malformation_is_an_error(tmp_path: Path, broken: str, complaint: str) -> None:
    with pytest.raises(ValueError, match=complaint):
        load_endpoints(_write(tmp_path, broken))


def test_serial_endpoint_link_uses_the_serial_transport(tmp_path: Path) -> None:
    endpoint = Endpoint(
        "b", "labwire.drivers:Balance", "serial", device="/dev/null-not-real", baud=9600
    )
    link = endpoint.link()
    assert "serial" in str(link._describe)  # pyright: ignore[reportPrivateUsage]
