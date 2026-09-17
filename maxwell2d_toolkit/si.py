"""AEDT scalar normalization and real-value validation."""

from __future__ import annotations

import math
from numbers import Real
import re
from typing import Any, Iterable


_NUMBER_WITH_UNIT = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z_]+)?\s*$"
)

# Canonical spellings below are taken from the Ansys Electronics Desktop unit
# table. Input aliases that are not AEDT tokens have a blank canonical spelling
# and are emitted as unitless SI values.
_UNITS = {
    "length": {
        "": (1.0, ""),
        "m": (1.0, ""),
        "meter": (1.0, "meter"),
        "meters": (1.0, ""),
        "cm": (1e-2, "cm"),
        "dm": (1e-1, "dm"),
        "fm": (1e-15, "fm"),
        "ft": (0.3048, "ft"),
        "in": (0.0254, "in"),
        "km": (1e3, "km"),
        "mil": (2.54e-5, "mil"),
        "mile": (1609.344, "mile"),
        "mm": (1e-3, "mm"),
        "um": (1e-6, "um"),
        "nm": (1e-9, "nm"),
        "pm": (1e-12, "pm"),
        "uin": (2.54e-8, "uin"),
        "yd": (0.9144, "yd"),
    },
    "capacitance": {
        "": (1.0, ""),
        "f": (1.0, "F"),
        "farad": (1.0, "F"),
        "farads": (1.0, ""),
        "ff": (1e-15, "fF"),
        "mf": (1e-3, "mF"),
        "uf": (1e-6, "uF"),
        "nf": (1e-9, "nF"),
        "pf": (1e-12, "pF"),
    },
    "voltage": {
        "": (1.0, ""),
        "v": (1.0, "V"),
        "volt": (1.0, ""),
        "volts": (1.0, ""),
        "fv": (1e-15, "fV"),
        "gv": (1e9, "gV"),
        "megv": (1e6, "MegV"),
        "mv": (1e-3, "mV"),
        "nv": (1e-9, "nV"),
        "pv": (1e-12, "pV"),
        "uv": (1e-6, "uV"),
        "kv": (1e3, "kV"),
    },
    "resistance": {
        "": (1.0, ""),
        "ohm": (1.0, "ohm"),
        "ohms": (1.0, ""),
        "gohm": (1e9, "GOhm"),
        "mohm": (1e-3, "mOhm"),
        "uohm": (1e-6, "uOhm"),
        "kohm": (1e3, "kOhm"),
        "megohm": (1e6, "megohm"),
    },
}

# Relevant official AEDT spellings, extended with the installed PyAEDT table
# when available. PyAEDT 1.3.0 omits units that AEDT 2024 R2 accepts, including
# Ohmm and kg_per_m3, so the explicit official additions remain necessary.
_OFFICIAL_AEDT_UNITS = {
    canonical
    for unit_map in _UNITS.values()
    for _, canonical in unit_map.values()
    if canonical
} | {
    "",
    "A",
    "F",
    "GOhm",
    "Hz",
    "J",
    "Ohmm",
    "Ohmcm",
    "Ohmum",
    "Ohmmm2_per_mm",
    "V",
    "cm",
    "deg",
    "fF",
    "kg",
    "kg_per_m3",
    "g_per_cm3",
    "kg_per_l",
    "kg_per_dm3",
    "g_per_l",
    "J_per_m3",
    "kJ_per_m3",
    "kHz",
    "kOhm",
    "kV",
    "mF",
    "mOhm",
    "mV",
    "meter",
    "mm",
    "nF",
    "nV",
    "ohm",
    "pF",
    "pV",
    "rad",
    "s",
    "uF",
    "uOhm",
    "uV",
    "um",
}
try:
    from ansys.aedt.core.generic.constants import AEDT_UNITS as _PYAEDT_UNITS
except ImportError:
    pass
else:
    _OFFICIAL_AEDT_UNITS.update(
        unit for unit_map in _PYAEDT_UNITS.values() for unit in unit_map
    )
AEDT_SUPPORTED_UNITS = frozenset(_OFFICIAL_AEDT_UNITS)
_AEDT_UNIT_TO_SI_SCALE = {
    canonical: scale
    for unit_map in _UNITS.values()
    for scale, canonical in unit_map.values()
    if canonical
}


def format_si_number(value: Real) -> str:
    """Return a finite real number as a compact decimal string."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"SI variable value must be real, got {type(value).__name__}.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("SI variable value must be finite.")
    return format(number, ".12g")


def format_aedt_value_from_si(value: Real, units: str = "") -> str:
    """Format an SI value using the exact supported units of an AEDT variable."""

    unit = str(units or "").strip()
    if not unit:
        return format_si_number(value)
    if unit not in AEDT_SUPPORTED_UNITS:
        raise ValueError(f"AEDT value uses an unsupported unit: {unit!r}.")
    if unit not in _AEDT_UNIT_TO_SI_SCALE:
        raise ValueError(f"AEDT SI conversion is not defined for unit: {unit!r}.")
    return f"{format_si_number(float(value) / _AEDT_UNIT_TO_SI_SCALE[unit])}{unit}"


def _parse_scalar(value: Real | str, quantity: str) -> tuple[float, str]:
    if quantity not in _UNITS:
        raise ValueError(f"Unsupported scalar quantity: {quantity!r}")
    if isinstance(value, Real) and not isinstance(value, bool):
        return float(value), ""
    if not isinstance(value, str):
        raise TypeError(f"{quantity} must be a real number or scalar string.")
    match = _NUMBER_WITH_UNIT.fullmatch(value)
    if match is None:
        raise ValueError(f"{quantity} must be a scalar value, got {value!r}.")
    return float(match.group(1)), (match.group(2) or "").casefold()


def to_si_number(value: Real | str, quantity: str) -> str:
    """Convert one scalar to a unitless SI number."""
    number, unit = _parse_scalar(value, quantity)
    try:
        scale = _UNITS[quantity][unit][0]
    except KeyError:
        raise ValueError(f"Unsupported {quantity} unit in {value!r}.") from None
    return format_si_number(number * scale)


def normalize_aedt_scalar(value: Real | str, quantity: str) -> str:
    """Keep an official AEDT unit, otherwise emit a unitless SI number."""
    number, unit = _parse_scalar(value, quantity)
    try:
        scale, canonical = _UNITS[quantity][unit]
    except KeyError:
        raise ValueError(f"Unsupported {quantity} unit in {value!r}.") from None
    if canonical:
        if canonical not in AEDT_SUPPORTED_UNITS:
            raise ValueError(f"Internal AEDT unit table does not contain {canonical!r}.")
        return f"{format_si_number(number)}{canonical}"
    return format_si_number(number * scale)


def verify_aedt_supported_real_variables(
    app: Any,
    names: Iterable[str],
) -> dict[str, float]:
    """Require official AEDT units and finite real SI values."""
    variables = app.variable_manager.variables
    checked: dict[str, float] = {}
    for name in dict.fromkeys(str(item) for item in names):
        if name not in variables:
            raise ValueError(f"AEDT variable is missing after creation: {name}")
        data = variables[name]
        units = str(getattr(data, "units", "") or "").strip()
        if units not in AEDT_SUPPORTED_UNITS:
            raise ValueError(f"AEDT variable uses an unsupported unit: {name} has {units!r}.")
        value = getattr(data, "si_value", getattr(data, "numeric_value", None))
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(
                f"AEDT variable must evaluate to a real number, not "
                f"{type(value).__name__}: {name}={value!r}."
            )
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"AEDT variable must evaluate to a finite real: {name}={value!r}.")
        checked[name] = number
    return checked
