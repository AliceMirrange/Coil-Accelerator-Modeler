from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


__version__ = "0.1.0"


@dataclass(frozen=True, slots=True)
class FrontReferencedModel:
    stage_count: int = 10
    projectile_name: str = "Projectile"
    projectile_material: str = "1020 Steel"
    projectile_front_variable: str = "$projectile_front_0"
    projectile_front_initial: str = "-20mm"
    projectile_length_variable: str = "$projectile_Len"
    projectile_length: str = "20mm"
    projectile_radius_variable: str = "$projectile_R"
    projectile_radius: str = "4mm"
    band_name: str = "Tube"
    band_z_min_variable: str = "$band_z_min"
    band_z_min: str = "-40mm"
    band_length_variable: str = "$band_Len"
    band_length: str = "400mm"
    band_radius_variable: str = "$band_R"
    band_radius: str = "4.10mm"
    motion_name: str = "Moving1"
    negative_limit: str = "-20mm"
    positive_limit: str = "340mm"
    initial_velocity: str = "0m_per_sec"
    mass: str = "7.85g"
    damping: float = 0.0003
    load_force: str = "-0.03N"
    force_name: str = "Force1"
    region_name: str = "Region"
    region_padding: tuple[str, str, str] = ("20mm", "40mm", "40mm")
    setup_name: str = "Setup1"
    stop_time: str = "10ms"
    time_step: str = "20us"
    coil_mesh_length: str = "1mm"
    projectile_mesh_length: str = "1mm"
    band_mesh_length: str = "3mm"
    region_mesh_length: str = "20mm"


def _require(value: Any, action: str) -> Any:
    if value is False or value is None:
        raise RuntimeError(f"AEDT failed to {action}.")
    return value


def _require_objects(app: Any, names: Iterable[str]) -> None:
    available = set(app.modeler.object_names)
    missing = [name for name in names if name not in available]
    if missing:
        raise ValueError("Missing model objects: " + ", ".join(missing))


def set_front_reference_variables(app: Any, config: FrontReferencedModel) -> None:
    variables = {
        config.projectile_front_variable: config.projectile_front_initial,
        config.projectile_length_variable: config.projectile_length,
        config.projectile_radius_variable: config.projectile_radius,
        config.band_z_min_variable: config.band_z_min,
        config.band_length_variable: config.band_length,
        config.band_radius_variable: config.band_radius,
    }
    for name, expression in variables.items():
        _require(
            app.variable_manager.set_variable(name, expression=expression, overwrite=True),
            f"set project variable {name}",
        )


def create_front_referenced_projectile(app: Any, config: FrontReferencedModel) -> Any:
    """Create an RZ projectile whose front face is fixed at model Z=0."""
    if config.projectile_name in app.modeler.object_names:
        raise ValueError(f"Object already exists: {config.projectile_name}")
    return _require(
        app.modeler.create_rectangle(
            origin=[0, 0, f"0mm-{config.projectile_length_variable}"],
            sizes=[config.projectile_length_variable, config.projectile_radius_variable],
            name=config.projectile_name,
            material=config.projectile_material,
        ),
        f"create {config.projectile_name}",
    )


def create_motion_band(app: Any, config: FrontReferencedModel) -> Any:
    if config.band_name in app.modeler.object_names:
        raise ValueError(f"Object already exists: {config.band_name}")
    return _require(
        app.modeler.create_rectangle(
            origin=[0, 0, config.band_z_min_variable],
            sizes=[config.band_length_variable, config.band_radius_variable],
            name=config.band_name,
            material="air",
        ),
        f"create {config.band_name}",
    )


def assign_front_referenced_motion(app: Any, config: FrontReferencedModel) -> Any:
    """Make Moving1.Position equal the projectile-front coordinate."""
    _require_objects(app, [config.band_name, config.projectile_name])
    return _require(
        app.assign_translate_motion(
            assignment=config.band_name,
            coordinate_system="Global",
            axis="Z",
            positive_movement=True,
            start_position=config.projectile_front_variable,
            periodic_translate=False,
            negative_limit=config.negative_limit,
            positive_limit=config.positive_limit,
            velocity=config.initial_velocity,
            mechanical_transient=True,
            mass=config.mass,
            damping=config.damping,
            load_force=config.load_force,
            motion_name=config.motion_name,
        ),
        f"assign translation motion {config.motion_name}",
    )


def create_region_and_balloon(app: Any, config: FrontReferencedModel) -> tuple[Any, Any]:
    if config.region_name in app.modeler.object_names:
        raise ValueError(f"Object already exists: {config.region_name}")
    region = _require(
        app.modeler.create_region(
            pad_value=list(config.region_padding),
            pad_type="Absolute Offset",
            name=config.region_name,
        ),
        f"create {config.region_name}",
    )
    boundary = _require(
        app.assign_balloon(assignment=region.edges, boundary="Balloon1"),
        "assign Balloon1",
    )
    return region, boundary


def assign_projectile_force(app: Any, config: FrontReferencedModel) -> Any:
    _require_objects(app, [config.projectile_name])
    return _require(
        app.assign_force(
            assignment=[config.projectile_name],
            coordinate_system="Global",
            is_virtual=True,
            force_name=config.force_name,
        ),
        f"assign force {config.force_name}",
    )


def assign_baseline_meshes(app: Any, config: FrontReferencedModel) -> dict[str, Any]:
    coils = [f"CoilBody_{index}" for index in range(1, config.stage_count + 1)]
    _require_objects(app, coils + [config.projectile_name, config.band_name, config.region_name])
    assignments = {
        "Mesh_Coils": (coils, config.coil_mesh_length),
        "Mesh_Projectile": ([config.projectile_name], config.projectile_mesh_length),
        "Mesh_Band": ([config.band_name], config.band_mesh_length),
        "Mesh_Region": ([config.region_name], config.region_mesh_length),
    }
    return {
        name: _require(
            app.mesh.assign_length_mesh(
                assignment=objects,
                inside_selection=True,
                maximum_length=length,
                maximum_elements=None,
                name=name,
            ),
            f"assign mesh {name}",
        )
        for name, (objects, length) in assignments.items()
    }


def create_transient_setup(app: Any, config: FrontReferencedModel) -> Any:
    if config.setup_name in app.setup_names:
        raise ValueError(f"Analysis setup already exists: {config.setup_name}")
    setup = _require(app.create_setup(name=config.setup_name), f"create {config.setup_name}")
    setup.props["StopTime"] = config.stop_time
    setup.props["TimeStep"] = config.time_step
    _require(setup.update(), f"update {config.setup_name}")
    return setup


def complete_manual_model_steps(app: Any, config: FrontReferencedModel | None = None) -> dict[str, Any]:
    """Add the non-GUI model steps without saving or solving the project."""
    cfg = config or FrontReferencedModel()
    set_front_reference_variables(app, cfg)
    band = create_motion_band(app, cfg)
    projectile = create_front_referenced_projectile(app, cfg)
    motion = assign_front_referenced_motion(app, cfg)
    region, balloon = create_region_and_balloon(app, cfg)
    force = assign_projectile_force(app, cfg)
    meshes = assign_baseline_meshes(app, cfg)
    setup = create_transient_setup(app, cfg)
    return {
        "band": band,
        "projectile": projectile,
        "motion": motion,
        "region": region,
        "balloon": balloon,
        "force": force,
        "meshes": meshes,
        "setup": setup,
    }


def add_front_position_to_existing_optimization(
    app: Any,
    *,
    minimum_mm: float,
    maximum_mm: float,
    setup_name: str = "AcceleratorScreeningOptimization",
    variable: str = "$projectile_front_0",
) -> tuple[float, float]:
    """Add numeric projectile-front bounds without changing the optimizer."""
    minimum = float(minimum_mm)
    maximum = float(maximum_mm)
    if minimum >= maximum:
        raise ValueError("minimum_mm must be smaller than maximum_mm")
    variables = app.variable_manager.variables
    if variable not in variables:
        raise ValueError(f"Create the project variable before Optimetrics: {variable}")
    setups = app.optimizations.design_setups
    if setup_name not in setups:
        raise ValueError(f"Optimization setup does not exist: {setup_name}")

    setup = setups[setup_name]
    current = float(app.get_evaluated_value(variable, units="mm"))
    start = current if minimum <= current <= maximum else (minimum + maximum) / 2
    minimum_value = app.value_with_units(minimum, "mm")
    maximum_value = app.value_with_units(maximum, "mm")
    step = maximum - minimum

    _require(app.activate_variable_optimization(variable), f"activate {variable} for optimization")
    setup.auto_update = False
    setup.props.setdefault("Variables", {})[variable] = [
        "i:=", True,
        "int:=", False,
        "Min:=", minimum_value,
        "Max:=", maximum_value,
        "MinStep:=", app.value_with_units(step / 100, "mm"),
        "MaxStep:=", app.value_with_units(step / 10, "mm"),
        "MinFocus:=", minimum_value,
        "MaxFocus:=", maximum_value,
        "UseManufacturableValues:=", "false",
        "Level:=", f"[{minimum}: {maximum}] mm",
    ]
    setup.props.setdefault("StartingPoint", {})[variable] = app.value_with_units(start, "mm")
    setup.auto_update = True
    _require(setup.update(), f"update {setup_name}")
    return minimum, maximum


def import_external_circuit(
    app: Any,
    *,
    netlist_path: str | Path,
    circuit_design: str,
    parameters: dict[str, str] | None = None,
) -> None:
    """Import an exported Maxwell Circuit netlist without solving the design."""
    path = Path(netlist_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    _require(
        app.edit_external_circuit(
            netlist_file_path=str(path),
            schematic_design_name=circuit_design,
            parameters=parameters,
        ),
        "import the external circuit",
    )
