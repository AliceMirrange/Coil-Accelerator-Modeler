"""Build a disposable AEDT project and validate generated variable units."""

from __future__ import annotations

from datetime import datetime
from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
PROJECT_PATH = LOG_DIR / f"aedt_si_validation_{RUN_ID}.aedt"
REPORT_PATH = LOG_DIR / f"aedt_si_validation_{RUN_ID}.txt"
AEDT_VERSION = "2024.2"

sys.path.insert(0, str(ROOT))

from maxwell2d_toolkit.core import PROJECTILE_INITIAL_Z_VARIABLE
from maxwell2d_toolkit.si import verify_aedt_supported_real_variables
from maxwell_circuit_toolkit.core import (
    AcceleratorCircuitOptions,
    TOPOLOGY_SINGLE_BOOST,
    build_accelerator_external_circuit,
    ensure_esr_variables_for_indices,
    ensure_last_resistance_variable,
    ensure_projectile_initial_z_variable,
)


def load_builder():
    loader = SourceFileLoader("si_validation_builder", str(ROOT / "maxwell_accelerator_builder.pyw"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    builder = load_builder()
    settings = builder.load_builder_settings()
    config = builder.BuildConfig(
        project_path=PROJECT_PATH,
        design_name="SIValidation2D",
        aedt_version=AEDT_VERSION,
        non_graphical=True,
        common_inner_diameter=True,
        common_gap=True,
        prefixes=builder.variable_prefixes_from_settings(settings),
        coils=(
            builder.CoilInput(1, 20, 40, 20, 100, 0.65, "\u94dc", 5),
            builder.CoilInput(2, 20, 42, 21, 120, 0.60, "\u94dd", 0),
        ),
        material_weight_optimization=True,
    )
    model_variables = builder.build_project_variable_map(config)
    builder.build_maxwell_model(config)

    app = builder.launch_maxwell2d(
        project=PROJECT_PATH,
        design=config.design_name,
        solution_type="TransientZ",
        version=AEDT_VERSION,
        non_graphical=True,
        new_desktop=True,
        close_on_exit=False,
    )
    try:
        toolkit = builder.Maxwell2DToolkit(app)
        toolkit.create_transient_setup(name="Setup1", stop_time="20ms", time_step="0.1ms")
        optimization_variables = toolkit.prepare_accelerator_optimization_variables(
            stage_count=2,
            material_weight_optimization=True,
        )
        optimization_setup, optimization_bounds = toolkit.create_accelerator_screening_optimization(
            stage_count=2,
            calculation="$device_weight",
            prepare_variables=False,
        )
        projectile_variation = optimization_setup.props["Variables"][PROJECTILE_INITIAL_Z_VARIABLE]
        assert optimization_bounds[PROJECTILE_INITIAL_Z_VARIABLE] == (0.0, 0.012)
        assert projectile_variation[projectile_variation.index("Min:=") + 1] == "0mm"
        assert projectile_variation[projectile_variation.index("Max:=") + 1] == "12mm"
        circuit_variables = {}
        circuit_variables.update(ensure_esr_variables_for_indices(app, (1, 2)))
        circuit_variables.update(ensure_last_resistance_variable(app))
        circuit_variables.update(ensure_projectile_initial_z_variable(app))
        names = list(dict.fromkeys([*model_variables, *optimization_variables, *circuit_variables]))
        values = verify_aedt_supported_real_variables(app, names)
        circuit_result = build_accelerator_external_circuit(
            app,
            options=AcceleratorCircuitOptions(topology=TOPOLOGY_SINGLE_BOOST),
            netlist_path=PROJECT_PATH.with_name(f"{PROJECT_PATH.stem}_circuit.sph"),
        )
        assert circuit_result.resistors[0].parameters["R"] == "$coil_R_1"
        assert circuit_result.storage_capacitors[0].parameters["C"] == "$C_1"
        assert circuit_result.storage_capacitors[0].parameters["IC"] == "$V"
        assert circuit_result.storage_esr_resistors[0].parameters["R"] == "$ESR_1"
        variables = app.variable_manager.variables
        lines = [
            "AEDT SUPPORTED-UNIT VARIABLE VALIDATION: PASS",
            "AEDT OPTIMETRICS UNIT-TYPE VALIDATION: PASS",
            "AEDT PROJ_INITZ OPTIMETRICS RANGE 0MM-12MM: PASS",
            "AEDT DIRECT CIRCUIT VARIABLE REFERENCES: PASS",
            f"PROJECT={PROJECT_PATH}",
            f"COUNT={len(names)}",
        ]
        for name in names:
            data = variables[name]
            expression = str(getattr(data, "expression", ""))
            numeric = getattr(data, "si_value", None)
            lines.append(
                f"{name}\texpression={expression}\tunits={getattr(data, 'units', '')!r}"
                f"\tsi_value={numeric!r}\ttype={type(numeric).__name__}"
            )
        lines.append(f"REAL_FINITE_COUNT={len(values)}")
        REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="ascii")
        app.save_project(str(PROJECT_PATH))
    finally:
        app.release_desktop(close_projects=True, close_desktop=True)
    print(REPORT_PATH)


if __name__ == "__main__":
    main()
