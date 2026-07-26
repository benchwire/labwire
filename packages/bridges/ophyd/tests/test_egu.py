"""Tests for the EPICS EGU to UCUM translation table."""

import pytest
from labwire.bridges.ophyd import egu_to_ucum


@pytest.mark.parametrize(
    ("egu", "ucum"),
    [
        ("mm", "mm"),
        ("um", "um"),
        ("µm", "um"),
        ("microns", "um"),
        ("deg", "deg"),
        ("degC", "Cel"),  # UCUM spells Celsius "Cel", never "C"
        ("C", "Cel"),
        ("K", "K"),
        ("eV", "eV"),
        ("keV", "keV"),
        ("mA", "mA"),
        ("ohms", "Ohm"),
        ("counts", "{counts}"),  # UCUM annotation form for a pure count
        ("sccm", "mL/min"),
        ("%", "%"),
        ("mm/s", "mm/s"),
        ("torr", "torr"),
    ],
)
def test_known_egu_spellings_translate(egu: str, ucum: str) -> None:
    assert egu_to_ucum(egu) == ucum


@pytest.mark.parametrize("egu", ["MM", "Deg", "COUNTS"])
def test_translation_falls_back_to_case_insensitive_match(egu: str) -> None:
    assert egu_to_ucum(egu) is not None


@pytest.mark.parametrize("egu", [None, "", "   ", "none", "N/A", "unitless", "-"])
def test_absent_units_do_not_become_dimensionless(egu: str | None) -> None:
    """A blank EGU says nothing; turning it into "1" would be a guess."""
    assert egu_to_ucum(egu) is None


@pytest.mark.parametrize("egu", ["furlongs per fortnight", "widgets", "µSv/banana"])
def test_unknown_egu_is_unresolved_rather_than_passed_through(egu: str) -> None:
    assert egu_to_ucum(egu) is None


def test_whitespace_is_tolerated() -> None:
    assert egu_to_ucum("  mm  ") == "mm"
