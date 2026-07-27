"""RFC 8785 (JCS) JSON canonicalization (SPEC §13.2).

Vendored implementation (the algorithm is small and dependency-free): keys
sort by code point, strings use minimal escaping with literal UTF-8, and
numbers follow ECMAScript ``Number::toString`` formatting.

Example:
    >>> from labwire.core.jcs import jcs_dumps
    >>> jcs_dumps({"b": 1.0, "a": "25°C"})
    '{"a":"25°C","b":1}'
"""

import json
import math
import operator
from decimal import Decimal
from typing import Any, SupportsFloat, SupportsIndex, cast


def _jcs_number(raw: float) -> str:
    # Normalize float subclasses (numpy scalars, most importantly): their repr
    # is not a JSON number, and this string is what gets signed.
    value = float(raw)
    if math.isnan(value) or math.isinf(value):
        raise ValueError("non-finite numbers are not representable in JSON")
    if value == 0:
        return "0"  # including -0.0, per ECMAScript
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    text = repr(value)  # shortest round-trip digits, Python formatting
    if "e" in text:
        mantissa, _, exponent = text.partition("e")
        exp = int(exponent)
        if -7 < exp < 21:  # ECMAScript uses plain decimal in this range
            scaled = Decimal(mantissa).scaleb(exp)  # exact: same digits, shifted
            return format(scaled, "f")
        sign = "+" if exp >= 0 else "-"
        return f"{mantissa}e{sign}{abs(exp)}"
    return text


def jcs_dumps(value: Any) -> str:
    """Serialize ``value`` as RFC 8785 canonical JSON.

    Example:
        >>> jcs_dumps({"v": 56.0})
        '{"v":56}'
    """
    match value:
        case None:
            return "null"
        case bool():
            return "true" if value else "false"
        case int():
            return str(value)
        case float():
            return _jcs_number(value)  # float subclasses normalize inside
        case str():
            return json.dumps(value, ensure_ascii=False)
        case list() | tuple():
            items: list[Any] = list(cast("list[Any] | tuple[Any, ...]", value))
            return "[" + ",".join(jcs_dumps(v) for v in items) + "]"
        case dict():
            mapping = cast("dict[str, Any]", value)
            parts = [
                f"{json.dumps(k, ensure_ascii=False)}:{jcs_dumps(v)}"
                for k, v in sorted(mapping.items())
            ]
            return "{" + ",".join(parts) + "}"
        case _:
            # Numeric types that do not subclass int/float, numpy integers are
            # the common case in scientific Python, are still real numbers.
            if hasattr(value, "__index__"):
                return str(operator.index(cast("SupportsIndex", value)))
            if hasattr(value, "__float__"):
                return _jcs_number(float(cast("SupportsFloat", value)))
            raise TypeError(f"not JSON-serializable: {type(value).__name__}")


def jcs_canonical(value: Any) -> bytes:
    """UTF-8 bytes of the canonical form: the signing/digest input.

    Example:
        >>> jcs_canonical({"v": 1})
        b'{"v":1}'
    """
    return jcs_dumps(value).encode()


def params_digest(params: "dict[str, Any]") -> str:
    """The SPEC §8.6 parameter digest: sha256 over RFC 8785 canonical JSON.

    Computed over the normalized parameter object of SPEC §8.2, so the
    digested thing and the recorded thing cannot disagree, and an auditor
    can recompute it offline from a manifest. The binding of an operator
    authorization to this digest is LAP's design, adopted with credit.

    Example:
        >>> params_digest({})
        'sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'
    """
    import hashlib

    return "sha256:" + hashlib.sha256(jcs_canonical(params)).hexdigest()
