import pytest

from maxwell2d_toolkit.si import (
    format_aedt_value_from_si,
    normalize_aedt_scalar,
    to_si_number,
    verify_aedt_supported_real_variables,
)


def test_format_aedt_value_from_si_uses_the_requested_supported_unit():
    assert format_aedt_value_from_si(0.0025, "mm") == "2.5mm"
    assert format_aedt_value_from_si(100e-6, "uF") == "100uF"
    assert format_aedt_value_from_si(0.5) == "0.5"


class FakeVariable:
    def __init__(self, value, units=""):
        self.numeric_value = value
        self.si_value = value
        self.units = units


def fake_app(variables):
    manager = type("Manager", (), {"variables": variables})()
    return type("App", (), {"variable_manager": manager})()


def test_supported_aedt_units_are_preserved():
    assert normalize_aedt_scalar("25mm", "length") == "25mm"
    assert normalize_aedt_scalar("220uF", "capacitance") == "220uF"
    assert normalize_aedt_scalar("400mOhm", "resistance") == "400mOhm"
    assert normalize_aedt_scalar("390V", "voltage") == "390V"


def test_non_aedt_aliases_are_converted_to_unitless_si_numbers():
    assert normalize_aedt_scalar("25m", "length") == "25"
    assert normalize_aedt_scalar("2volts", "voltage") == "2"
    assert to_si_number("25mm", "length") == "0.025"


def test_aedt_validation_accepts_supported_units_and_returns_si_value():
    app = fake_app({"$x": FakeVariable(0.025, "mm")})
    assert verify_aedt_supported_real_variables(app, ["$x"]) == {"$x": 0.025}


def test_aedt_validation_rejects_unsupported_unit():
    app = fake_app({"$x": FakeVariable(850.0, "J_per_kg")})
    with pytest.raises(ValueError, match="unsupported unit"):
        verify_aedt_supported_real_variables(app, ["$x"])


def test_aedt_validation_rejects_complex_even_with_zero_imaginary_part():
    app = fake_app({"$x": FakeVariable(complex(0.025, 0.0))})
    with pytest.raises(ValueError, match="real number"):
        verify_aedt_supported_real_variables(app, ["$x"])
