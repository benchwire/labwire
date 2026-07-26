"""EPICS engineering-unit (EGU) strings to UCUM codes.

EPICS `.EGU` fields are free text: a facility writes whatever its engineers
type. Labwire v0.2 requires a UCUM code on every quantity, so the bridge
translates the spellings that are conventional in beamline practice and
treats everything else as **unresolved** — a human then supplies the code in
the annotation file. Nothing here guesses.

The mappings are chosen from EPICS convention, not validated against a UCUM
implementation.
<!-- TODO-VERIFY: check this table against a UCUM validator when the main
roadmap's UCUM grammar work lands. -->

Example:
    >>> from labwire.bridges.ophyd._egu import egu_to_ucum
    >>> egu_to_ucum("microns")
    'um'
    >>> egu_to_ucum("furlongs per fortnight") is None
    True
"""

# Keys are compared case-sensitively first, then case-insensitively, because
# UCUM itself is case-sensitive ("mm" is not "MM") but EGU rarely is.
_EGU_TO_UCUM: dict[str, str] = {
    # length
    "m": "m",
    "cm": "cm",
    "mm": "mm",
    "um": "um",
    "µm": "um",
    "μm": "um",
    "micron": "um",
    "microns": "um",
    "nm": "nm",
    "A": "Ao",  # ångström: UCUM 'Ao'
    "angstrom": "Ao",
    "Å": "Ao",
    # angle
    "deg": "deg",
    "degrees": "deg",
    "rad": "rad",
    "mrad": "mrad",
    "urad": "urad",
    # time
    "s": "s",
    "sec": "s",
    "seconds": "s",
    "ms": "ms",
    "us": "us",
    "min": "min",
    "h": "h",
    # frequency
    "Hz": "Hz",
    "kHz": "kHz",
    "MHz": "MHz",
    # electrical
    "V": "V",
    "mV": "mV",
    "kV": "kV",
    "amp": "A",
    "amps": "A",
    "mA": "mA",
    "uA": "uA",
    "nA": "nA",
    "ohm": "Ohm",
    "ohms": "Ohm",
    # energy
    "eV": "eV",
    "keV": "keV",
    "MeV": "MeV",
    "J": "J",
    "mJ": "mJ",
    # temperature — UCUM spells degrees Celsius "Cel"
    "C": "Cel",
    "degC": "Cel",
    "deg C": "Cel",
    "Celsius": "Cel",
    "K": "K",
    "kelvin": "K",
    # pressure
    "torr": "torr",
    "mtorr": "mtorr",
    "mbar": "mbar",
    "bar": "bar",
    "Pa": "Pa",
    "psi": "[psi]",
    # mass
    "g": "g",
    "mg": "mg",
    "kg": "kg",
    # volume / flow
    "L": "L",
    "mL": "mL",
    "uL": "uL",
    "mL/min": "mL/min",
    "uL/min": "uL/min",
    "sccm": "mL/min",  # standard cm3/min at STP; the closest honest UCUM form
    # speed / acceleration
    "mm/s": "mm/s",
    "mm/s2": "mm/s2",
    "deg/s": "deg/s",
    "m/s": "m/s",
    # dimensionless and counting
    "%": "%",
    "percent": "%",
    "counts": "{counts}",
    "cts": "{counts}",
    "count": "{counts}",
    "pixels": "{pixels}",
    "arb": "{arbitrary}",
    "au": "{arbitrary}",
    "a.u.": "{arbitrary}",
}

# EGU strings that explicitly mean "no unit given". These must NOT become "1":
# a blank EGU is the commonest EPICS case and says nothing about whether the
# quantity is dimensionless (SPEC §7.2 forbids guessing).
_EMPTY_EGU = {"", "none", "n/a", "na", "-", "unitless"}


def egu_to_ucum(egu: str | None) -> str | None:
    """Translate an EPICS EGU string to a UCUM code, or None if unresolved.

    Example:
        >>> egu_to_ucum("degC")
        'Cel'
        >>> egu_to_ucum("") is None
        True
    """
    if egu is None:
        return None
    trimmed = egu.strip()
    if trimmed.lower() in _EMPTY_EGU:
        return None
    if trimmed in _EGU_TO_UCUM:
        return _EGU_TO_UCUM[trimmed]
    lowered = trimmed.lower()
    for candidate, code in _EGU_TO_UCUM.items():
        if candidate.lower() == lowered:
            return code
    return None
