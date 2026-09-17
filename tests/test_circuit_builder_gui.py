"""Regression tests for the embedded Maxwell Circuit GUI."""

import inspect
from configparser import ConfigParser
from pathlib import Path

from maxwell_circuit_toolkit import gui as module


class DummyVar:
    def __init__(self, value=None, **_kwargs):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class DummyRoot:
    def __init__(self):
        self.title_value = None
        self.minsize_value = None

    def title(self, value):
        self.title_value = value

    def minsize(self, width, height):
        self.minsize_value = (width, height)


def test_gui_initializes_storage_vars_before_build_ui(monkeypatch):
    monkeypatch.setattr(module.tk, "StringVar", DummyVar)
    monkeypatch.setattr(module.tk, "BooleanVar", DummyVar)

    seen = {}

    def fake_build_ui(self):
        # This function is called by __init__. If either attribute is missing,
        # this test reproduces the v0.9.0 startup AttributeError immediately.
        seen["cap"] = self.fallback_capacitance.get()
        seen["vinit"] = self.fallback_initial_voltage.get()
        seen["esr"] = self.fallback_esr.get()
        seen["rlast"] = self.fallback_last_resistance

    monkeypatch.setattr(module.CircuitBuilderGUI, "_build_ui", fake_build_ui)
    root_dir = Path(__file__).resolve().parents[1]
    settings = ConfigParser(interpolation=None)
    settings.optionxform = str
    settings.read(root_dir / "default.ini", encoding="utf-8")
    diode_parameters = {
        key: value
        for key, value in settings.items("diode")
        if key != "model_name" and value.strip()
    }
    gui = module.CircuitBuilderGUI(
        DummyRoot(),
        settings=settings,
        config_path=root_dir / "default.ini",
        log_dir=root_dir / "logs",
        diode_parameters=diode_parameters,
    )

    initial = settings["initial_values"]
    assert seen == {
        "cap": initial["capacitance"],
        "vinit": initial["initial_voltage"],
        "esr": initial["esr"],
        "rlast": initial["last_resistance"],
    }
    assert gui.fallback_capacitance.get() == initial["capacitance"]
    assert gui.fallback_initial_voltage.get() == initial["initial_voltage"]
    assert gui.fallback_esr.get() == initial["esr"]
    assert gui.last_resistance_variable == "Rlast"
    assert gui.circuit_design.get() == "AcceleratorExternalCircuit"
    assert gui.delete_existing.get() is True
    assert "External Circuit" in gui.root.title_value


def test_circuit_page_has_no_inner_notebook_or_hidden_parameter_tabs():
    source = inspect.getsource(module.CircuitBuilderGUI._build_ui)
    assert "ttk.Notebook" not in source
    assert "switch_tab" not in source
    assert "pulse_tab" not in source


def test_projectile_move_reminder_is_persistent_and_repeated_after_build():
    assert module.PROJECTILE_MOVE_VECTOR == "0,0,$proj_initZ"
    assert module.PROJECTILE_MOVE_REMINDER == (
        "\u5728\u7ed8\u5236\u5b8c\u52a8\u5b50\u540e\uff0c\u6cbfZ+\u65b9\u5411\u6267\u884cMove\u64cd\u4f5c\uff0c"
        "Moving Vector\u4e3a(0,0,$proj_initZ)"
    )
    assert "PROJECTILE_MOVE_REMINDER" in inspect.getsource(
        module.CircuitBuilderGUI._build_ui
    )
    assert "PROJECTILE_MOVE_REMINDER" in inspect.getsource(
        module.CircuitBuilderGUI._poll_events
    )


def test_clicking_move_reminder_copies_vector_without_parentheses():
    class ClipboardRoot:
        def __init__(self):
            self.clipboard = None
            self.updated = False

        def clipboard_clear(self):
            self.clipboard = ""

        def clipboard_append(self, value):
            self.clipboard += value

        def update_idletasks(self):
            self.updated = True

    root = ClipboardRoot()
    gui = type("GUI", (), {"root": root, "status": DummyVar()})()

    module.CircuitBuilderGUI._copy_projectile_move_vector(gui)

    assert root.clipboard == "0,0,$proj_initZ"
    assert root.updated is True
    assert gui.status.get() == "已复制 Moving Vector：0,0,$proj_initZ"
    source = inspect.getsource(module.CircuitBuilderGUI._build_ui)
    assert "<Button-1>" in source
    assert "_copy_projectile_move_vector" in source
