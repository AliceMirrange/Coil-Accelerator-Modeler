from datetime import datetime
from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path
import sys

from maxwell_circuit_toolkit import AcceleratorCircuitOptions


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "maxwell_accelerator_builder.pyw"
loader = SourceFileLoader("accelerator_builder_config_test", str(BUILDER))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = module
loader.exec_module(module)


def test_unified_ini_contains_all_gui_and_circuit_defaults():
    settings = module.load_builder_settings()
    defaults = settings["gui_defaults"]
    prefixes = settings["variable_prefixes"]
    assert module.CONFIG_PATH.name == "default.ini"
    assert defaults["maxwell_design"] == "LinearAccelerator2D"
    assert defaults["circuit_design"] == "AcceleratorExternalCircuit"
    assert defaults["coil_count"] == "10"
    assert float(defaults["coil_outer_diameter"]) > 0
    assert int(defaults["coil_turns"]) > 0
    assert defaults["coil_material"] == "铜"
    assert defaults.getboolean("material_weight_optimization") is False
    assert defaults["capacitor_energy_density_j_per_g"] == "0.85"
    assert prefixes["r_dc"] == "coil_R"
    assert prefixes["is_copper"] == "is_copper"
    assert prefixes["pos_on"] == "POSon"
    assert prefixes["position_factor"] == "u"
    assert prefixes["capacitance"] == "C"
    assert prefixes["initial_voltage"] == "V"
    assert prefixes["last_resistance"] == "Rlast"
    assert settings["initial_values"]["position_factor"] == "0.5"
    assert settings["circuit"]["topology"] == "single_boost"
    assert settings["vpulse"]["shunt_resistance"] == "10000ohm"
    assert settings["vpulse"]["V2"] == "1.25*$V"
    assert settings["vpwl"]["fall_delta"] == "0.01mm"
    assert settings["vpwl"]["end_position"] == "1e9mm"
    assert settings["voltage_controlled_switch"]["Von"] == "0.9*$V"
    assert settings["voltage_controlled_switch"]["Voff"] == "0.1*$V"
    assert settings["initial_values"]["last_resistance"] == "1ohm"
    assert module.diode_model_parameters(settings) == {
        "IS": "1e-14A",
        "RS": "0ohm",
        "N": "1",
        "EG": "1.11V",
        "XTI": "3",
        "BV": "1e+30V",
        "IBV": "0.001A",
        "TNOM": "27",
    }


def test_builder_config_was_merged_into_main_program():
    assert not (ROOT / "builder_config.py").exists()
    assert "def load_builder_settings" in BUILDER.read_text(encoding="utf-8")


def test_first_launch_project_path_uses_program_directory():
    settings = module.load_builder_settings()
    settings["gui_defaults"]["last_project_path"] = ""
    path = module.initial_project_path(
        settings,
        base_dir=ROOT,
        now=datetime(2026, 8, 28, 1, 2, 3),
    )
    assert path == ROOT / "LinearAccelerator_20260828_010203.aedt"


def test_model_gui_defaults_are_injected_from_ini(monkeypatch):
    class Variable:
        def __init__(self, value=None, **_kwargs):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    settings = module.load_builder_settings()
    defaults = settings["gui_defaults"]
    defaults["last_project_path"] = str(ROOT / "remembered.aedt")
    defaults["maxwell_design"] = "ConfiguredMaxwell"
    defaults["aedt_version"] = "2025.2"
    defaults["non_graphical"] = "true"
    defaults["coil_count"] = "12"
    defaults["inner_mode"] = "independent"
    defaults["gap_mode"] = "independent"
    defaults["position_mode"] = "induction"
    defaults["common_inner_diameter"] = "21"
    defaults["common_gap"] = "6"
    defaults["coil_outer_diameter"] = "45"
    defaults["material_weight_optimization"] = "true"
    defaults["capacitor_energy_density_j_per_g"] = "0.92"
    monkeypatch.setattr(module.tk, "StringVar", Variable)
    monkeypatch.setattr(module.tk, "BooleanVar", Variable)
    monkeypatch.setattr(module.AcceleratorBuilderGUI, "_build_ui", lambda self: None)
    monkeypatch.setattr(module.AcceleratorBuilderGUI, "_rebuild_rows", lambda self: None)
    gui = module.AcceleratorBuilderGUI(object(), settings=settings, manage_window=False)
    assert gui.project_path.get() == str(ROOT / "remembered.aedt")
    assert gui.design_name.get() == "ConfiguredMaxwell"
    assert gui.aedt_version.get() == "2025.2"
    assert gui.non_graphical.get() is True
    assert gui.coil_count.get() == "12"
    assert gui.inner_mode.get() == "independent"
    assert gui.gap_mode.get() == "independent"
    assert gui.position_mode.get() == "induction"
    assert gui.material_weight_optimization.get() is True
    assert gui.capacitor_energy_density.get() == "0.92"
    assert gui._collect_prefixes().is_copper == "is_copper"
    assert gui.common_inner.get() == "21"
    assert gui.common_gap.get() == "6"
    assert gui.row_defaults[1] == "45"


def test_last_project_path_is_preserved_without_rewriting_ini(tmp_path):
    config_path = tmp_path / "default.ini"
    config_path.write_text(module.CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    selected = tmp_path / "existing.aedt"
    module.save_last_project_path(selected, config_path)
    text = config_path.read_text(encoding="utf-8")
    settings = module.load_builder_settings(config_path)
    assert "; Every initial value shown" in text
    assert settings["gui_defaults"]["last_project_path"] == str(selected)
    assert module.initial_project_path(settings) == selected


def test_existing_project_selection_disables_tk_overwrite_prompt(monkeypatch, tmp_path):
    selected = tmp_path / "existing.aedt"
    selected.write_bytes(b"test")
    captured = {}

    class Value:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    def fake_dialog(**kwargs):
        captured.update(kwargs)
        return str(selected)

    monkeypatch.setattr(module.filedialog, "asksaveasfilename", fake_dialog)
    fake_gui = type("FakeGUI", (), {"project_path": Value(str(selected))})()
    module.AcceleratorBuilderGUI._choose_project(fake_gui)
    assert captured["confirmoverwrite"] is False
    assert fake_gui.project_path.get() == str(selected)


def test_log_files_resolve_under_logs_directory():
    assert module.get_log_path("sample.log") == ROOT / "logs" / "sample.log"


def test_gui_uses_microsoft_yahei_ui_for_all_tk_named_fonts(monkeypatch):
    configured = {}
    options = []

    class Font:
        def __init__(self, name):
            self.name = name

        def configure(self, **kwargs):
            configured[self.name] = kwargs

    class Root:
        def option_add(self, pattern, value):
            options.append((pattern, value))

    root = Root()
    monkeypatch.setattr(
        module.tkfont,
        "names",
        lambda owner: ("TkDefaultFont", "TkHeadingFont", "UserDefinedFont"),
    )
    monkeypatch.setattr(
        module.tkfont,
        "nametofont",
        lambda name, root: Font(name),
    )

    module.configure_gui_fonts(root)

    assert module.GUI_FONT_FAMILY == "Microsoft YaHei UI"
    assert configured == {
        "TkDefaultFont": {"family": "Microsoft YaHei UI"},
        "TkHeadingFont": {"family": "Microsoft YaHei UI"},
    }
    assert options == [
        ("*Font", "TkDefaultFont"),
        ("*TCombobox*Listbox.font", "TkDefaultFont"),
    ]


def test_window_title_contains_the_current_application_version():
    assert module.APP_VERSION == "1.0.0"
    assert module.WINDOW_TITLE == "Coil Accelerator Modeler 1.0.0"
    assert f'version = "{module.APP_VERSION}"' in (ROOT / "pyproject.toml").read_text(
        encoding="ascii"
    )


def test_changelog_starts_with_the_current_application_version():
    changelog = (ROOT / "logs" / "CHANGELOG.txt").read_text(encoding="utf-8")
    assert changelog.startswith(f"v{module.APP_VERSION}\n")


def test_two_old_gui_entry_points_were_replaced_by_one_combined_entry():
    assert (ROOT / "maxwell_accelerator_builder.pyw").is_file()
    assert not (ROOT / "linear_accelerator_builder_fixed.pyw").exists()
    assert not (ROOT / "maxwell_circuit_builder.pyw").exists()


def test_direct_circuit_options_keep_library_fallbacks():
    options = AcceleratorCircuitOptions()
    assert options.coil_resistance_prefix == "coil_R"
    assert options.pos_on_prefix == "POSon"
    assert options.ron == "0.001ohm"
    assert options.v2 == "1.25*$V"
    assert options.initial_voltage_variable == "V"
    assert options.vpwl_fall_delta == "0.01mm"
    assert options.vpwl_end_position == "1e9mm"
    assert options.diode_model_parameters["BV"] == "1e+30V"
    assert options.last_resistance_variable == "Rlast"
    assert options.fallback_last_resistance == "1ohm"
    assert options.projectile_initial_z_variable == "$proj_initZ"
    assert options.fallback_projectile_initial_z == "0mm"
    assert options.scr_hold_pulse_width == "1e9mm"


def test_ini_accepts_odd_even_boost_as_default_topology(tmp_path):
    target = tmp_path / "odd_even.ini"
    target.write_text(
        module.CONFIG_PATH.read_text(encoding="utf-8").replace(
            "topology = single_boost", "topology = odd_even_boost"
        ),
        encoding="utf-8",
    )
    assert module.load_builder_settings(target)["circuit"]["topology"] == "odd_even_boost"


def test_ini_accepts_thin_film_capacitor_scr_as_default_topology(tmp_path):
    target = tmp_path / "film_scr.ini"
    target.write_text(
        module.CONFIG_PATH.read_text(encoding="utf-8").replace(
            "topology = single_boost", "topology = film_capacitor_scr"
        ),
        encoding="utf-8",
    )
    assert (
        module.load_builder_settings(target)["circuit"]["topology"]
        == "film_capacitor_scr"
    )
