from maxwell2d_toolkit.core import Maxwell2DToolkit


class FakeVariable:
    def __init__(self, units, numeric_value=0.0, si_value=None):
        self.units = units
        self.numeric_value = numeric_value
        self.si_value = numeric_value if si_value is None else si_value


class FakeVariableManager:
    def __init__(self, variables):
        self.variables = variables

    def __getitem__(self, name):
        return self.variables[name]


class FakeOptimizationSetup:
    def __init__(self, name):
        self.name = name
        self.props = {"Optimizer": "Quasi Newton", "Variables": {}, "StartingPoint": {}}
        self.auto_update = True
        self.variations = []
        self.goals = []
        self.update_count = 0

    def update(self):
        self.update_count += 1
        return True

    def add_variation(self, variable, *, min_value, max_value):
        self.variations.append((variable, min_value, max_value))
        return True

    def add_goal(self, **kwargs):
        self.goals.append(kwargs)
        return True


class FakeOptimizations:
    def __init__(self):
        self.design_setups = {}
        self.add_kwargs = None
        self.deleted = []

    def add(self, **kwargs):
        self.add_kwargs = kwargs
        return FakeOptimizationSetup(kwargs["name"])

    def delete(self, name):
        self.deleted.append(name)
        return True


class FakeApp:
    def __init__(self):
        evaluated = {
            "$coil_center_1": 0.01,
            "$coil_center_2": 0.0375,
            "$coil_center_3": 0.07,
            "$coil_Len_1": 0.02,
            "$coil_Len_2": 0.021,
            "$coil_Len_3": 0.022,
            "$coil_outer_Diameter_1": 0.04,
            "$coil_outer_Diameter_2": 0.042,
            "$coil_outer_Diameter_3": 0.044,
            "$Turn_1": 100.0,
            "$Turn_2": 120.0,
            "$Turn_3": 80.0,
            "$gap": 0.005,
        }
        names = {
            **{f"$z_start_{i}": FakeVariable("", (0.0, 0.025, 0.051)[i - 1]) for i in range(1, 4)},
            **{
                f"$coil_Len_{i}": FakeVariable(
                    "mm", evaluated[f"$coil_Len_{i}"] * 1000, evaluated[f"$coil_Len_{i}"]
                )
                for i in range(1, 4)
            },
            **{
                f"$coil_outer_Diameter_{i}": FakeVariable(
                    "mm",
                    evaluated[f"$coil_outer_Diameter_{i}"] * 1000,
                    evaluated[f"$coil_outer_Diameter_{i}"],
                )
                for i in range(1, 4)
            },
            **{f"$Turn_{i}": FakeVariable("", evaluated[f"$Turn_{i}"]) for i in range(1, 4)},
            "$coil_inner_Diameter": FakeVariable("", 0.02),
            **{f"$fill_factor_{i}": FakeVariable("", 0.65) for i in range(1, 4)},
            "$rho_1": FakeVariable("", 1.724e-8),
            "$rho_2": FakeVariable("", 2.82e-8),
            "$rho_3": FakeVariable("", 1.724e-8),
            "$gap": FakeVariable("mm", evaluated["$gap"] * 1000, evaluated["$gap"]),
            **{f"$POSon_{i}": FakeVariable("", 0.0) for i in range(1, 4)},
            "$u_2": FakeVariable("", 0.5),
            "$POSdur_1": FakeVariable("mm", 10.0, 0.01),
            "$C_1": FakeVariable("uF", 1000.0, 0.001),
        }
        self.modeler = object()
        self.variable_manager = FakeVariableManager(names)
        self.optimizations = FakeOptimizations()
        self.nominal_sweep = "Setup1 : Transient"
        self.evaluated = evaluated
        self.expressions = {}
        self.activated_variables = []
        self.activation_bounds = {}

    def get_evaluated_value(self, name, units=None):
        assert units == (None if name.startswith("$Turn_") else "mm")
        return self.evaluated[name]

    @staticmethod
    def value_with_units(value, units):
        return f"{value:g}{units}"

    def activate_variable_optimization(self, variable, minimum=None, maximum=None):
        self.activated_variables.append(variable)
        self.activation_bounds[variable] = (minimum, maximum)
        return True


def make_toolkit(app):
    toolkit = Maxwell2DToolkit(app)
    writes = []

    def set_global_variables(variables, *, verify=True):
        assert verify is True
        writes.append(dict(variables))
        for name, expression in variables.items():
            numeric_value = app.evaluated.get(name, 0.0)
            units = ""
            si_value = numeric_value
            if name.startswith("$is_copper_"):
                numeric_value = float(expression)
                si_value = numeric_value
            elif name.startswith("$rho_"):
                selector = app.variable_manager.variables[f"$is_copper_{name.rsplit('_', 1)[1]}"].numeric_value
                numeric_value = 1.724e-8 if selector == 1 else 2.82e-8
                si_value = numeric_value
            elif name == "$capacitor_energy_density":
                numeric_value = float(expression)
                si_value = numeric_value
            elif name.startswith(("$coil_center_", "$POSon_", "$POSdur_")):
                units = "mm"
                si_value = numeric_value if name.startswith("$coil_center_") else 0.01
                numeric_value = si_value * 1000
            elif name == "$proj_initZ":
                units = "mm"
                numeric_value = 0.0
                si_value = 0.0
            elif name.startswith("$C_"):
                units = "uF"
                numeric_value = 220.0
                si_value = 220e-6
            app.variable_manager.variables[name] = FakeVariable(units, numeric_value, si_value)
            app.expressions[name] = str(expression)

    toolkit.set_global_variables = set_global_variables
    return toolkit, writes


def test_accelerator_optimization_uses_unit_typed_setup_bounds_and_default_optimizer():
    app = FakeApp()
    app.optimizations.design_setups["AcceleratorScreeningOptimization"] = object()
    toolkit, writes = make_toolkit(app)
    setup, bounds = toolkit.create_accelerator_screening_optimization(stage_count=3)

    assert app.optimizations.add_kwargs == {
        "calculation": "Moving1.Speed",
        "ranges": {"Time": None},
        "optimization_type": "Optimization",
        "condition": "Maximize",
        "goal_value": 0,
        "solution": "Setup1 : Transient",
        "name": "AcceleratorScreeningOptimization",
    }
    assert setup.props["Optimizer"] == "Quasi Newton"
    assert app.optimizations.deleted == ["AcceleratorScreeningOptimization"]
    assert bounds["$u_2"] == (-0.2, 1.2)
    assert bounds["$u_3"] == (-0.2, 1.2)
    assert bounds["$POSdur_1"] == (0.001, 0.0275)
    assert bounds["$POSdur_2"] == (0.001, 0.0275)
    assert bounds["$POSdur_3"] == (0.001, 0.0325)
    assert bounds["$C_1"] == (0.0001, 0.0012)
    assert bounds["$coil_Len_1"] == (0.01, 0.03)
    assert bounds["$coil_Len_2"] == (0.0105, 0.0315)
    assert bounds["$coil_outer_Diameter_2"] == (0.021, 0.063)
    assert bounds["$Turn_2"] == (60.0, 180.0)
    assert bounds["$gap"] == (0.0025, 0.0075)
    assert bounds["$proj_initZ"] == (0.0, 0.012)
    assert not any("coil_inner_Diameter" in name or "fill_factor" in name for name in bounds)
    assert "$u_1" not in bounds
    assert setup.update_count == 1
    assert app.activated_variables == list(bounds)
    assert list(setup.props["Variables"]) == list(bounds)
    units = {
        **dict.fromkeys(
            [
                "$POSdur_1",
                "$POSdur_2",
                "$POSdur_3",
                "$coil_Len_1",
                "$coil_Len_2",
                "$coil_Len_3",
                "$coil_outer_Diameter_1",
                "$coil_outer_Diameter_2",
                "$coil_outer_Diameter_3",
                "$gap",
                "$proj_initZ",
            ],
            "mm",
        ),
        **dict.fromkeys(["$C_1", "$C_2", "$C_3"], "uF"),
    }
    scales = {"": 1.0, "mm": 1e-3, "uF": 1e-6}
    for variable, (minimum, maximum) in bounds.items():
        variation = setup.props["Variables"][variable]
        unit = units.get(variable, "")
        assert variation[variation.index("Min:=") + 1] == f"{minimum / scales[unit]:.12g}{unit}"
        assert variation[variation.index("Max:=") + 1] == f"{maximum / scales[unit]:.12g}{unit}"
        assert variation[variation.index("MinStep:=") + 1].endswith(unit)
        assert variation[variation.index("MaxStep:=") + 1].endswith(unit)
        assert variation[variation.index("MinFocus:=") + 1] == f"{minimum / scales[unit]:.12g}{unit}"
        assert variation[variation.index("MaxFocus:=") + 1] == f"{maximum / scales[unit]:.12g}{unit}"
        level = f"[{minimum / scales[unit]:.12g}: {maximum / scales[unit]:.12g}]" + (
            f" {unit}" if unit else ""
        )
        assert variation[variation.index("Level:=") + 1] == level
        assert setup.props["StartingPoint"][variable].endswith(unit)
    assert setup.props["Variables"]["$gap"][4:8] == ["Min:=", "2.5mm", "Max:=", "7.5mm"]
    assert setup.props["Variables"]["$C_1"][4:8] == ["Min:=", "100uF", "Max:=", "1200uF"]
    assert setup.props["Variables"]["$proj_initZ"][4:8] == ["Min:=", "0mm", "Max:=", "12mm"]
    assert app.activation_bounds["$proj_initZ"] == ("0mm", "12mm")
    assert setup.props["StartingPoint"]["$proj_initZ"] == "0mm"
    assert app.expressions["$coil_center_3"] == "$z_start_3+$coil_Len_3/2"
    assert app.expressions["$POSon_1"] == "-1000mm"
    assert app.expressions["$POSon_3"] == "$coil_center_2+$u_3*($coil_center_3-$coil_center_2)"
    assert app.expressions["$u_3"] == "0.5"
    assert app.expressions["$POSdur_2"] == "10mm"
    assert app.expressions["$C_3"] == "220uF"
    assert app.expressions["$proj_initZ"] == "0mm"
    assert "$u_2" not in writes[0]
    assert "$POSdur_1" not in writes[0]
    assert "$C_1" not in writes[0]


def test_accelerator_screening_optimization_rejects_missing_geometry_variables():
    app = FakeApp()
    del app.variable_manager.variables["$coil_Len_3"]
    toolkit, _ = make_toolkit(app)
    try:
        toolkit.create_accelerator_screening_optimization(stage_count=3)
    except ValueError as exc:
        assert "$coil_Len_3" in str(exc)
    else:
        raise AssertionError("Missing source geometry variables must be rejected")


def test_induction_mode_excludes_u1_and_uses_wider_factor_bounds():
    app = FakeApp()
    toolkit, _ = make_toolkit(app)
    _, bounds = toolkit.create_accelerator_screening_optimization(stage_count=3, position_mode="induction")

    assert "$u_1" not in bounds
    assert bounds["$u_2"] == (-0.3, 1.6)
    assert bounds["$u_3"] == (-0.3, 1.6)
    assert app.expressions["$POSon_1"] == "-1000mm"
    assert app.expressions["$POSon_3"] == "$coil_center_3-$coil_Len_3/2+$u_3*$coil_Len_3"


def test_independent_gap_variables_all_receive_numeric_half_to_one_and_half_bounds():
    app = FakeApp()
    app.evaluated.update({"$gap_1": 0.004, "$gap_2": 0.006})
    app.variable_manager.variables.update(
        {
            "$gap_1": FakeVariable("mm", 4.0, 0.004),
            "$gap_2": FakeVariable("mm", 6.0, 0.006),
        }
    )
    del app.variable_manager.variables["$gap"]
    toolkit, _ = make_toolkit(app)

    _, bounds = toolkit.create_accelerator_screening_optimization(
        stage_count=3,
        common_gap=False,
    )

    assert bounds["$gap_1"] == (0.002, 0.006)
    assert bounds["$gap_2"] == (0.003, 0.009)
    assert "$gap_3" not in bounds
    assert "$gap" not in bounds


def test_existing_projectile_initial_z_is_preserved_and_used_as_starting_point():
    app = FakeApp()
    app.variable_manager.variables["$proj_initZ"] = FakeVariable("mm", 5.0, 0.005)
    toolkit, writes = make_toolkit(app)

    setup, bounds = toolkit.create_accelerator_screening_optimization(stage_count=3)

    assert bounds["$proj_initZ"] == (0.0, 0.012)
    assert setup.props["Variables"]["$proj_initZ"][4:8] == [
        "Min:=",
        "0mm",
        "Max:=",
        "12mm",
    ]
    assert setup.props["StartingPoint"]["$proj_initZ"] == "5mm"
    assert not any("$proj_initZ" in variables for variables in writes)


def test_material_weight_optimization_uses_binary_material_selectors_and_weight_goal():
    app = FakeApp()
    toolkit, _ = make_toolkit(app)

    setup, bounds = toolkit.create_accelerator_screening_optimization(
        stage_count=3,
        material_weight_optimization=True,
        capacitor_energy_density_j_per_g=0.85,
    )

    assert bounds["$is_copper_1"] == (0.0, 1.0)
    assert bounds["$is_copper_3"] == (0.0, 1.0)
    assert "$rho_1" not in bounds
    selector_variation = setup.props["Variables"]["$is_copper_1"]
    assert selector_variation[selector_variation.index("int:=") + 1] is True
    assert selector_variation[selector_variation.index("Level:=") + 1] == "[0, 1]"
    assert setup.props["StartingPoint"]["$is_copper_1"] == "1"
    assert setup.props["StartingPoint"]["$is_copper_2"] == "0"
    assert app.expressions["$rho_1"] == (
        "if($is_copper_1==1,1.724e-08Ohmm,2.82e-08Ohmm)"
    )
    assert app.expressions["$material_density_1"] == (
        "if($is_copper_1==1,8960kg_per_m3,2700kg_per_m3)"
    )
    assert app.expressions["$coil_weight_1"] == (
        "3.141592653589793/4*$fill_factor_1*"
        "($coil_outer_Diameter_1^2-$coil_inner_Diameter^2)*"
        "$coil_Len_1*$material_density_1"
    )
    assert app.expressions["$capacitor_energy_density"] == "850"
    assert app.expressions["$capacitor_weight_1"] == (
        "0.5*$C_1*$V^2/$capacitor_energy_density"
    )
    assert app.expressions["$device_weight"] == (
        "$coil_weight_1+$capacitor_weight_1+"
        "$coil_weight_2+$capacitor_weight_2+"
        "$coil_weight_3+$capacitor_weight_3"
    )
    assert setup.goals == [
        {
            "calculation": "$device_weight",
            "ranges": {"Time": None},
            "solution": "Setup1 : Transient",
            "condition": "Minimize",
            "goal_value": 0,
        }
    ]
