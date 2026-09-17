"""Pure-Python smoke tests for the accelerator builder variable logic."""

from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "maxwell_accelerator_builder.pyw"
loader = SourceFileLoader("accelerator_builder_test", str(BUILDER))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = module
loader.exec_module(module)


def make_coils():
    return (
        module.CoilInput(1, 20, 40, 20, 100, 0.65, "铜", 5),
        module.CoilInput(2, 22, 42, 21, 120, 0.60, "铝", 0),
    )


def make_prefixes(**overrides):
    defaults = module.variable_prefixes_from_settings()
    values = {key: getattr(defaults, key) for key in module.VariablePrefixes.__dataclass_fields__}
    values.update(overrides)
    return module.VariablePrefixes(**values)


def test_custom_independent_prefixes():
    p = make_prefixes(
        inner_diameter="coil_id",
        outer_diameter="coil_od",
        length="coil_len",
        turns="coil_N",
        fill_factor="coil_fill",
        rho="coil_rho",
        gap="coil_gap",
        z_start="coil_z",
        r_dc="coil_R",
    )
    cfg = module.BuildConfig(
        project_path=Path("x.aedt"), design_name="D", aedt_version=None,
        non_graphical=False, common_inner_diameter=False, common_gap=False,
        prefixes=p, coils=make_coils(),
    )
    variables = module.build_project_variable_map(cfg)
    assert variables["$coil_z_2"] == "$coil_z_1+$coil_len_1+$coil_gap_1"
    assert variables["$coil_rho_1"] == "1.724e-08Ohmm"
    assert variables["$coil_id_1"] == "20mm"
    assert variables["$coil_od_1"] == "40mm"
    assert "$coil_N_2" in variables["$coil_R_2"]
    assert variables["$coil_center_1"] == "$coil_z_1+$coil_len_1/2"
    assert variables["$POSon_1"] == "-1000mm"
    assert "$u_1" not in variables
    assert variables["$u_2"] == "0.5"
    assert variables["$POSon_2"] == "$coil_center_1+$u_2*($coil_center_2-$coil_center_1)"


def test_induction_mode_poson_uses_coil_start_plus_u_times_length():
    cfg = module.BuildConfig(
        project_path=Path("x.aedt"), design_name="D", aedt_version=None,
        non_graphical=False, common_inner_diameter=True, common_gap=True,
        prefixes=make_prefixes(), coils=make_coils(), position_mode="induction",
    )
    variables = module.build_project_variable_map(cfg)
    assert "$u_1" not in variables
    assert variables["$POSon_1"] == "-1000mm"
    assert variables["$POSon_2"] == "$coil_center_2-$coil_Len_2/2+$u_2*$coil_Len_2"


def test_common_inner_and_gap_use_prefix_without_index():
    p = make_prefixes(inner_diameter="coil_id", gap="coil_gap")
    cfg = module.BuildConfig(
        project_path=Path("x.aedt"), design_name="D", aedt_version=None,
        non_graphical=False, common_inner_diameter=True, common_gap=True,
        prefixes=p, coils=make_coils(),
    )
    variables = module.build_project_variable_map(cfg)
    assert "$coil_id" in variables
    assert "$coil_id_1" not in variables
    assert "$coil_gap" in variables
    assert "$coil_gap_1" not in variables



def test_requested_default_prefixes():
    p = module.variable_prefixes_from_settings()
    assert p.inner_diameter == "coil_inner_Diameter"
    assert p.outer_diameter == "coil_outer_Diameter"
    assert p.length == "coil_Len"
    assert p.turns == "Turn"
    assert p.r_dc == "coil_R"
    assert p.fill_factor == "fill_factor"
    assert p.rho == "rho"
    assert p.is_copper == "is_copper"
    assert p.gap == "gap"
    assert p.z_start == "z_start"
    assert p.coil_center == "coil_center"
    assert p.pos_on == "POSon"
    assert p.pos_dur == "POSdur"
    assert p.position_factor == "u"
    assert p.last_resistance == "Rlast"


def test_material_optimization_builds_selector_driven_rho_values():
    cfg = module.BuildConfig(
        project_path=Path("x.aedt"), design_name="D", aedt_version=None,
        non_graphical=False, common_inner_diameter=True, common_gap=True,
        prefixes=make_prefixes(), coils=make_coils(), material_weight_optimization=True,
    )
    variables = module.build_project_variable_map(cfg)
    assert variables["$is_copper_1"] == "1"
    assert variables["$is_copper_2"] == "0"
    assert variables["$rho_1"] == "if($is_copper_1==1,1.724e-08Ohmm,2.82e-08Ohmm)"
    assert variables["$rho_2"] == "if($is_copper_2==1,1.724e-08Ohmm,2.82e-08Ohmm)"


class FakeReleaseApp:
    def __init__(self):
        self.calls = []

    def release_desktop(self, **kwargs):
        self.calls.append(kwargs)
        return True


def test_graphical_release_keeps_desktop_and_project_open():
    app = FakeReleaseApp()
    module._release_aedt_control(app, non_graphical=False)
    assert app.calls == [{"close_projects": False, "close_desktop": False}]


def test_non_graphical_release_closes_hidden_desktop():
    app = FakeReleaseApp()
    module._release_aedt_control(app, non_graphical=True)
    assert app.calls == [{"close_projects": True, "close_desktop": True}]


class FakeReleaseAppCloseOnExitOnly:
    def __init__(self):
        self.calls = []

    def release_desktop(self, *, close_projects=True, close_on_exit=True):
        self.calls.append({"close_projects": close_projects, "close_on_exit": close_on_exit})
        return True


def test_release_keyword_compatibility_fallback():
    app = FakeReleaseAppCloseOnExitOnly()
    module._release_aedt_control(app, non_graphical=False)
    assert app.calls == [{"close_projects": False, "close_on_exit": False}]



class FakeVariableManager:
    def __init__(self, store):
        self.store = store

    @property
    def project_variables(self):
        return dict(self.store)

    def get_expression(self, name):
        return self.store.get(name)


class FakeBoundary:
    def __init__(self, name):
        self.name = name


class FakeBuildApp(FakeReleaseApp):
    geometry_mode = "about Z"

    def __init__(self):
        super().__init__()
        self.vars = {}
        self.variable_manager = FakeVariableManager(self.vars)
        self.coil_calls = []
        self.logger = None

    def __setitem__(self, name, value):
        self.vars[name] = str(value)

    def assign_coil(self, **kwargs):
        self.coil_calls.append(kwargs)
        return FakeBoundary(kwargs["name"])


class FakeBody:
    def __init__(self, name):
        self.name = name


class FakeToolkit:
    last = None

    def __init__(self, app):
        self.app = app
        self.units = None
        self.rectangles = []
        self.windings = []
        self.adds = []
        self.saved = None
        self.events = []
        FakeToolkit.last = self

    def set_model_units(self, units):
        self.units = units

    def set_global_variables(self, variables, *, verify=True):
        self.app.vars.update({k: str(v) for k, v in variables.items()})
        self.global_variable_batches = getattr(self, "global_variable_batches", [])
        self.global_variable_batches.append((dict(variables), verify))

    def verify_supported_real_variables(self, variables):
        assert any("mm" in value for value in self.app.vars.values())
        assert any("Ohmm" in value for value in self.app.vars.values())
        return {name: 0.0 for name in variables}

    def rectangle(self, **kwargs):
        self.rectangles.append(kwargs)
        return FakeBody(kwargs["name"])

    def create_empty_winding(self, **kwargs):
        self.windings.append(kwargs)
        return FakeBoundary(kwargs["name"])

    def add_coils_to_winding(self, winding, coil):
        self.adds.append((winding.name, coil.name))
        return True

    def prepare_accelerator_optimization_variables(self, **kwargs):
        self.prepare_optimetrics_kwargs = kwargs
        self.events.append("prepare")
        return {"$coil_center_1": "$z_start_1+$coil_Len_1/2"}

    def create_accelerator_screening_optimization(self, **kwargs):
        self.optimetrics_kwargs = kwargs
        self.events.append("create_optimetrics")
        return FakeBoundary("AcceleratorScreeningOptimization"), {"$POSdur_1": (1.0, 25.0)}

    def save(self, path):
        self.saved = Path(path)
        self.events.append("save")
        return True


def _run_fake_build(monkeypatch, non_graphical):
    app = FakeBuildApp()
    launch_kwargs = {}

    def fake_launch(**kwargs):
        launch_kwargs.update(kwargs)
        return app

    monkeypatch.setattr(module, "launch_maxwell2d", fake_launch)
    monkeypatch.setattr(module, "Maxwell2DToolkit", FakeToolkit)
    cfg = module.BuildConfig(
        project_path=Path("x.aedt"), design_name="D", aedt_version=None,
        non_graphical=non_graphical, common_inner_diameter=True, common_gap=True,
        prefixes=module.variable_prefixes_from_settings(), coils=make_coils(),
    )
    values = module.build_maxwell_model(cfg)
    return app, launch_kwargs, values


def test_build_passes_non_graphical_and_releases_graphical(monkeypatch):
    app, launch_kwargs, values = _run_fake_build(monkeypatch, False)
    assert launch_kwargs["non_graphical"] is False
    assert app.calls == [{"close_projects": False, "close_desktop": False}]
    assert app.coil_calls[0]["conductors_number"] == "$Turn_1"
    assert FakeToolkit.last.windings[0]["resistance"] == "$coil_R_1"
    assert FakeToolkit.last.rectangles[0]["origin"] == [
        "$coil_inner_Diameter/2", "0mm", "$z_start_1"
    ]
    assert len(values) == 2


def test_build_passes_non_graphical_and_closes_hidden_session(monkeypatch):
    app, launch_kwargs, _ = _run_fake_build(monkeypatch, True)
    assert launch_kwargs["non_graphical"] is True
    assert app.calls == [{"close_projects": True, "close_desktop": True}]


def test_build_emits_progress_messages(monkeypatch):
    app = FakeBuildApp()

    def fake_launch(**kwargs):
        return app

    monkeypatch.setattr(module, "launch_maxwell2d", fake_launch)
    monkeypatch.setattr(module, "Maxwell2DToolkit", FakeToolkit)
    cfg = module.BuildConfig(
        project_path=Path("x.aedt"), design_name="D", aedt_version=None,
        non_graphical=True, common_inner_diameter=True, common_gap=True,
        prefixes=module.variable_prefixes_from_settings(), coils=make_coils(),
    )
    messages = []
    module.build_maxwell_model(cfg, progress=messages.append)
    assert any("启动" in m for m in messages)
    assert any("第 1/2 级" in m for m in messages)
    assert any("保存" in m for m in messages)
    assert any("释放" in m for m in messages)


def test_default_project_directory_is_program_directory():
    settings = module.load_builder_settings()
    settings["gui_defaults"]["last_project_path"] = ""
    assert module.initial_project_path(settings).parent == ROOT


def test_build_registers_project_variables_as_one_batch(monkeypatch):
    app, _, _ = _run_fake_build(monkeypatch, False)
    batches = FakeToolkit.last.global_variable_batches
    assert len(batches) == 1
    variables, verify = batches[0]
    assert verify is True
    assert "$coil_outer_Diameter_1" in variables
    assert "$coil_R_2" in variables


def test_optimetrics_wrapper_passes_prefixes_and_ini_defaults(monkeypatch, tmp_path):
    app = FakeBuildApp()
    project = tmp_path / "existing.aedt"
    project.write_bytes(b"test")
    launch_kwargs = {}

    def fake_launch(**kwargs):
        launch_kwargs.update(kwargs)
        return app

    monkeypatch.setattr(module, "launch_maxwell2d", fake_launch)
    monkeypatch.setattr(module, "Maxwell2DToolkit", FakeToolkit)
    cfg = module.BuildConfig(
        project_path=project,
        design_name="D",
        aedt_version=None,
        non_graphical=False,
        common_inner_diameter=True,
        common_gap=True,
        prefixes=module.variable_prefixes_from_settings(),
        coils=make_coils(),
        position_mode="induction",
        default_position_factor="0.6",
        default_position_duration="12mm",
        default_capacitance="850uF",
        default_initial_voltage="410V",
        material_weight_optimization=True,
        capacitor_energy_density_j_per_g=0.9,
    )

    bounds = module.add_screening_optimization(cfg)

    assert bounds == {"$POSdur_1": (1.0, 25.0)}
    assert FakeToolkit.last.optimetrics_kwargs["position_on_prefix"] == "POSon"
    assert FakeToolkit.last.optimetrics_kwargs["position_factor_prefix"] == "u"
    assert FakeToolkit.last.optimetrics_kwargs["outer_diameter_prefix"] == "coil_outer_Diameter"
    assert FakeToolkit.last.optimetrics_kwargs["turns_prefix"] == "Turn"
    assert FakeToolkit.last.optimetrics_kwargs["gap_prefix"] == "gap"
    assert FakeToolkit.last.optimetrics_kwargs["common_gap"] is True
    assert FakeToolkit.last.optimetrics_kwargs["position_mode"] == "induction"
    assert FakeToolkit.last.optimetrics_kwargs["default_position_factor"] == "0.6"
    assert FakeToolkit.last.optimetrics_kwargs["default_position_duration"] == "12mm"
    assert FakeToolkit.last.optimetrics_kwargs["default_capacitance"] == "850uF"
    assert FakeToolkit.last.optimetrics_kwargs["inner_diameter_prefix"] == "coil_inner_Diameter"
    assert FakeToolkit.last.optimetrics_kwargs["common_inner_diameter"] is True
    assert FakeToolkit.last.optimetrics_kwargs["fill_factor_prefix"] == "fill_factor"
    assert FakeToolkit.last.optimetrics_kwargs["resistivity_prefix"] == "rho"
    assert FakeToolkit.last.optimetrics_kwargs["material_selector_prefix"] == "is_copper"
    assert FakeToolkit.last.optimetrics_kwargs["initial_voltage_variable"] == "V"
    assert FakeToolkit.last.optimetrics_kwargs["default_initial_voltage"] == "410V"
    assert FakeToolkit.last.optimetrics_kwargs["material_weight_optimization"] is True
    assert FakeToolkit.last.optimetrics_kwargs["capacitor_energy_density_j_per_g"] == 0.9
    assert FakeToolkit.last.optimetrics_kwargs["prepare_variables"] is False
    assert FakeToolkit.last.events == ["prepare", "save", "create_optimetrics", "save"]
    assert launch_kwargs["new_desktop"] is False
    assert app.calls == [{"close_projects": False, "close_desktop": False}]
