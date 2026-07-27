from labwire.core import PROTOCOL_VERSION, __version__


def test_version_is_set() -> None:
    assert __version__ == "0.2.1"


def test_protocol_version() -> None:
    assert PROTOCOL_VERSION == "0.2"
