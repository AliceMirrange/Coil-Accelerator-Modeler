"""Maxwell Circuit helpers for external-circuit construction.

The functions in this module follow the AEDT/PyAEDT external-circuit workflow:

1. Start from a Maxwell 2D/3D transient design whose windings are ``External``.
2. ``Maxwell2d.create_external_circuit()`` creates a Maxwell Circuit design and
   places all External windings on the schematic.
3. Add Maxwell Circuit Elements with the public schematic API.
4. Export the circuit as an ``.sph`` netlist.
5. ``Maxwell2d.edit_external_circuit()`` assigns that netlist back to Maxwell.

The accelerator-specific helper uses ``SW_V4`` (voltage-controlled switch with
controlling port), because that element can be wired directly to a ``VPulse``
source.  One ``SW_VModel`` is shared by every switch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
import re

from maxwell2d_toolkit.si import (
    normalize_aedt_scalar,
    verify_aedt_supported_real_variables,
)

Scalar = int | float | str
Progress = Callable[[str], None] | None

_DEFAULT_PREFIXES = {
    "length": "coil_Len",
    "z_start": "z_start",
    "pos_on": "POSon",
    "pos_dur": "POSdur",
    "r_dc": "coil_R",
    "capacitance": "C",
    "esr": "ESR",
    "initial_voltage": "V",
    "last_resistance": "Rlast",
}
_DEFAULT_SWITCH = {
    "model_name": "SW_Model_Common",
    "Ron": "0.001ohm",
    "Roff": "1000000ohm",
    "Von": "0.9*$V",
    "Voff": "0.1*$V",
}
_DEFAULT_PULSE = {
    "V1": "0V",
    "V2": "1.25*$V",
    "Tr": "0",
    "Tf": "0",
    "Period": "1e9mm",
    "shunt_resistance": "10000ohm",
}
_DEFAULT_PWL = {
    "fall_delta": "0.01mm",
    "end_position": "1e9mm",
}
_DEFAULT_INITIAL = {
    "pos_on": "0mm",
    "pos_dur": "10mm",
    "capacitance": "220uF",
    "initial_voltage": "390V",
    "esr": "400mOhm",
    "last_resistance": "1ohm",
}
_DEFAULT_DIODE = {"model_name": "DIODE_Model_Common"}
_DEFAULT_DIODE_PARAMETERS = {
    "IS": "1e-14A",
    "RS": "0ohm",
    "N": "1",
    "EG": "1.11V",
    "XTI": "3",
    "BV": "1e+30V",
    "IBV": "0.001A",
    "TNOM": "27",
}

TOPOLOGY_SINGLE_BOOST = "single_boost"
TOPOLOGY_ODD_EVEN_BOOST = "odd_even_boost"
TOPOLOGY_FILM_CAPACITOR_SCR = "film_capacitor_scr"
PROJECTILE_INITIAL_Z_VARIABLE = "$proj_initZ"
PROJECTILE_INITIAL_Z_DEFAULT = "0mm"
SCR_HOLD_PULSE_WIDTH = "1e9mm"
PROJECTILE_MOVE_VECTOR = "0,0,$proj_initZ"
PROJECTILE_MOVE_REMINDER = (
    "\u5728\u7ed8\u5236\u5b8c\u52a8\u5b50\u540e\uff0c\u6cbfZ+\u65b9\u5411\u6267\u884cMove\u64cd\u4f5c\uff0c"
    f"Moving Vector\u4e3a({PROJECTILE_MOVE_VECTOR})"
)
TOPOLOGY_DEFINITIONS: dict[str, str] = {
    TOPOLOGY_SINGLE_BOOST: "单路Boost",
    TOPOLOGY_ODD_EVEN_BOOST: "奇偶Boost",
    TOPOLOGY_FILM_CAPACITOR_SCR: "薄膜电容+模拟SCR",
}


def available_topologies() -> list[tuple[str, str]]:
    """Return ``[(key, display_name), ...]`` in GUI display order."""
    return list(TOPOLOGY_DEFINITIONS.items())


def topology_display_name(key: str) -> str:
    """Return the human-readable name for a topology key."""
    try:
        return TOPOLOGY_DEFINITIONS[str(key)]
    except KeyError as exc:
        raise ValueError(f"Unsupported circuit topology: {key!r}") from exc


class MaxwellCircuitToolkitError(RuntimeError):
    """Raised when a Maxwell Circuit operation cannot be completed."""


def _report(progress: Progress, message: str) -> None:
    if progress is not None:
        try:
            progress(message)
        except Exception:
            pass


def _script_object(app: Any, private_name: str, public_name: str) -> Any:
    obj = getattr(app, private_name, None)
    if obj is None:
        obj = getattr(app, public_name, None)
    if obj is None:
        raise MaxwellCircuitToolkitError(
            f"PyAEDT object exposes neither {private_name!r} nor {public_name!r}."
        )
    return obj


def get_project_variable_names(app: Any) -> list[str]:
    """Return AEDT project-variable names with one native GetVariables call."""
    project = _script_object(app, "_oproject", "oproject")
    return [str(v) for v in (project.GetVariables() or [])]


def get_independent_project_variable_names(app: Any) -> list[str]:
    """Return only independent AEDT project/global variable names.

    PyAEDT documents ``VariableManager.independent_project_variable_names``
    specifically for project variables whose values are independent constants.
    External Circuit Parameter Values can map these variables directly.  A
    dependent project variable (for example a geometry-derived ``$coil_R_n``)
    is intentionally excluded rather than being submitted to
    ``edit_external_circuit(parameters=...)``.
    """
    manager = getattr(app, "variable_manager", None)
    if manager is None:
        raise MaxwellCircuitToolkitError(
            "PyAEDT application exposes no variable_manager; cannot identify "
            "independent Maxwell Circuit project variables."
        )
    try:
        names = manager.independent_project_variable_names
    except Exception as exc:
        raise MaxwellCircuitToolkitError(
            "Could not read VariableManager.independent_project_variable_names: "
            f"{exc}"
        ) from exc
    return [str(v) for v in (names or [])]


def set_global_variables_low_level(
    app: Any,
    name_to_value: Mapping[str, Scalar],
    *,
    overwrite_existing: bool = True,
    verify: bool = True,
) -> None:
    """Batch-create/update project variables in one ``ChangeProperty`` call.

    New variables are placed under ``NewProps`` and declared ``VariableProp``;
    existing variables are placed under ``ChangedProps`` only when
    ``overwrite_existing`` is true.  The function performs one GetVariables
    call before the change and, when ``verify`` is true, one after it.
    """
    if not name_to_value:
        return

    normalized: list[tuple[str, str]] = []
    for raw_name, raw_value in name_to_value.items():
        name = str(raw_name).strip()
        if not name.startswith("$"):
            raise ValueError(f"Project/global variable must start with '$': {name!r}")
        normalized.append((name, str(raw_value)))

    project = _script_object(app, "_oproject", "oproject")
    existing = set(project.GetVariables() or [])

    new_props: list[Any] = ["NAME:NewProps"]
    changed_props: list[Any] = ["NAME:ChangedProps"]
    requested_names: list[str] = []

    for name, value in normalized:
        requested_names.append(name)
        if name in existing:
            if overwrite_existing:
                changed_props.append(["NAME:" + name, "Value:=", value])
        else:
            new_props.append(
                [
                    "NAME:" + name,
                    "PropType:=",
                    "VariableProp",
                    "UserDef:=",
                    True,
                    "Value:=",
                    value,
                ]
            )

    if len(new_props) == 1 and len(changed_props) == 1:
        return

    tab: list[Any] = [
        "NAME:ProjectVariableTab",
        ["NAME:PropServers", "ProjectVariables"],
    ]
    if len(new_props) > 1:
        tab.append(new_props)
    if len(changed_props) > 1:
        tab.append(changed_props)

    try:
        project.ChangeProperty(["NAME:AllTabs", tab])
    except Exception as exc:
        raise MaxwellCircuitToolkitError(
            f"Batch project-variable ChangeProperty failed: {exc}"
        ) from exc

    if verify:
        after = set(project.GetVariables() or [])
        missing = [name for name in requested_names if name not in after]
        if missing:
            raise MaxwellCircuitToolkitError(
                "AEDT did not retain requested project variables: " + ", ".join(missing[:10])
            )


def ensure_position_variables(
    app: Any,
    count: int,
    *,
    pos_on_prefix: str = _DEFAULT_PREFIXES["pos_on"],
    pos_dur_prefix: str = _DEFAULT_PREFIXES["pos_dur"],
    geometry_z_prefix: str = _DEFAULT_PREFIXES["z_start"],
    geometry_length_prefix: str = _DEFAULT_PREFIXES["length"],
    fallback_on: str = _DEFAULT_INITIAL["pos_on"],
    fallback_duration: str = _DEFAULT_INITIAL["pos_dur"],
    use_geometry_defaults: bool = False,
    verify: bool = True,
) -> dict[str, str]:
    """Create missing ``$POSon_n``/``$POSdur_n`` project variables.

    Existing POS variables are preserved.  Missing variables are independent
    numeric project variables by default (``fallback_on``/``fallback_duration``),
    which makes them suitable as later Optimetrics variables.  Set
    ``use_geometry_defaults=True`` only when dependent initial expressions such
    as ``$POSon_n=$z_start_n`` are specifically desired.
    """
    if count < 1:
        raise ValueError("count must be >= 1")
    existing = set(get_project_variable_names(app))
    create: dict[str, str] = {}
    for i in range(1, count + 1):
        on_name = f"${pos_on_prefix}_{i}"
        dur_name = f"${pos_dur_prefix}_{i}"
        if on_name not in existing:
            z_ref = f"${geometry_z_prefix}_{i}"
            create[on_name] = (
                z_ref
                if use_geometry_defaults and z_ref in existing
                else normalize_aedt_scalar(fallback_on, "length")
            )
        if dur_name not in existing:
            len_ref = f"${geometry_length_prefix}_{i}"
            create[dur_name] = (
                len_ref
                if use_geometry_defaults and len_ref in existing
                else normalize_aedt_scalar(fallback_duration, "length")
            )
    if create:
        set_global_variables_low_level(
            app,
            create,
            overwrite_existing=False,
            verify=verify,
        )
    return create


def natural_winding_key(name: str) -> tuple[int, int, str]:
    """Sort names like Winding_2 before Winding_10."""
    match = re.search(r"(\d+)$", str(name))
    if match:
        return (0, int(match.group(1)), str(name).lower())
    return (1, 0, str(name).lower())




def winding_stage_index(name: str) -> int | None:
    """Return the trailing stage number from a winding name, if present."""
    match = re.search(r"(\d+)$", str(name))
    return int(match.group(1)) if match else None


def ensure_position_variables_for_indices(
    app: Any,
    indices: Sequence[int],
    *,
    pos_on_prefix: str = _DEFAULT_PREFIXES["pos_on"],
    pos_dur_prefix: str = _DEFAULT_PREFIXES["pos_dur"],
    geometry_z_prefix: str = _DEFAULT_PREFIXES["z_start"],
    geometry_length_prefix: str = _DEFAULT_PREFIXES["length"],
    fallback_on: str = _DEFAULT_INITIAL["pos_on"],
    fallback_duration: str = _DEFAULT_INITIAL["pos_dur"],
    use_geometry_defaults: bool = False,
    verify: bool = True,
) -> dict[str, str]:
    """Create missing POS variables for explicit accelerator stage indices."""
    clean = [int(i) for i in indices]
    if not clean or any(i < 1 for i in clean) or len(set(clean)) != len(clean):
        raise ValueError("indices must contain unique positive stage numbers")
    existing = set(get_project_variable_names(app))
    create: dict[str, str] = {}
    for i in clean:
        on_name = f"${pos_on_prefix}_{i}"
        dur_name = f"${pos_dur_prefix}_{i}"
        if on_name not in existing:
            z_ref = f"${geometry_z_prefix}_{i}"
            create[on_name] = (
                z_ref
                if use_geometry_defaults and z_ref in existing
                else normalize_aedt_scalar(fallback_on, "length")
            )
        if dur_name not in existing:
            len_ref = f"${geometry_length_prefix}_{i}"
            create[dur_name] = (
                len_ref
                if use_geometry_defaults and len_ref in existing
                else normalize_aedt_scalar(fallback_duration, "length")
            )
    if create:
        set_global_variables_low_level(
            app, create, overwrite_existing=False, verify=verify
        )
    return create


def ensure_esr_variables_for_indices(
    app: Any,
    indices: Sequence[int],
    *,
    esr_prefix: str = _DEFAULT_PREFIXES["esr"],
    fallback_esr: str = _DEFAULT_INITIAL["esr"],
    verify: bool = True,
) -> dict[str, str]:
    """Create missing per-stage storage-capacitor ESR project variables.

    For stage ``n`` the fixed project variable name is ``$ESR_n`` by default.
    Existing variables are preserved exactly; only missing variables are created.
    """
    clean = [int(i) for i in indices]
    if not clean or any(i < 1 for i in clean) or len(set(clean)) != len(clean):
        raise ValueError("indices must contain unique positive stage numbers")
    existing = set(get_project_variable_names(app))
    create: dict[str, str] = {}
    for i in clean:
        name = f"${esr_prefix}_{i}"
        if name not in existing:
            create[name] = normalize_aedt_scalar(fallback_esr, "resistance")
    if create:
        set_global_variables_low_level(
            app, create, overwrite_existing=False, verify=verify
        )
    return create


def ensure_storage_capacitor_variables_for_indices(
    app: Any,
    indices: Sequence[int],
    *,
    capacitance_prefix: str = _DEFAULT_PREFIXES["capacitance"],
    initial_voltage_variable: str = _DEFAULT_PREFIXES["initial_voltage"],
    fallback_capacitance: str = _DEFAULT_INITIAL["capacitance"],
    fallback_initial_voltage: str = _DEFAULT_INITIAL["initial_voltage"],
    verify: bool = True,
) -> dict[str, str]:
    """Create missing per-stage capacitance variables and one shared IC variable.

    Stage ``n`` uses ``$C_n`` by default, while every storage capacitor uses
    the same project variable ``$V`` for its ``IC`` property. Existing
    variables are preserved exactly; only missing variables are created.
    """
    clean = [int(i) for i in indices]
    if not clean or any(i < 1 for i in clean) or len(set(clean)) != len(clean):
        raise ValueError("indices must contain unique positive stage numbers")
    existing = set(get_project_variable_names(app))
    create: dict[str, str] = {}
    for i in clean:
        name = f"${capacitance_prefix}_{i}"
        if name not in existing:
            create[name] = normalize_aedt_scalar(fallback_capacitance, "capacitance")
    vinit_name = str(initial_voltage_variable).strip()
    if not vinit_name:
        raise ValueError("initial_voltage_variable must not be empty")
    if not vinit_name.startswith("$"):
        vinit_name = "$" + vinit_name
    if vinit_name not in existing:
        create[vinit_name] = normalize_aedt_scalar(fallback_initial_voltage, "voltage")
    if create:
        set_global_variables_low_level(
            app, create, overwrite_existing=False, verify=verify
        )
    return create


def ensure_last_resistance_variable(
    app: Any,
    *,
    variable_name: str = _DEFAULT_PREFIXES["last_resistance"],
    fallback_value: str = _DEFAULT_INITIAL["last_resistance"],
    verify: bool = True,
) -> dict[str, str]:
    """Create the shared last-stage return-resistance variable when missing.

    ``variable_name`` may be supplied with or without the project-variable
    ``$`` prefix.  An existing value is preserved exactly.
    """
    name = str(variable_name).strip()
    if not name:
        raise ValueError("variable_name must not be empty")
    if not name.startswith("$"):
        name = "$" + name
    if name in set(get_project_variable_names(app)):
        return {}
    create = {name: normalize_aedt_scalar(fallback_value, "resistance")}
    set_global_variables_low_level(
        app, create, overwrite_existing=False, verify=verify
    )
    return create


def ensure_projectile_initial_z_variable(
    app: Any,
    *,
    variable_name: str = PROJECTILE_INITIAL_Z_VARIABLE,
    fallback_value: str = PROJECTILE_INITIAL_Z_DEFAULT,
    verify: bool = True,
) -> dict[str, str]:
    """Create the projectile initial-Z offset without replacing an existing value."""
    name = str(variable_name).strip()
    if not name:
        raise ValueError("variable_name must not be empty")
    if not name.startswith("$"):
        name = "$" + name
    if name in set(get_project_variable_names(app)):
        return {}
    create = {name: normalize_aedt_scalar(fallback_value, "length")}
    set_global_variables_low_level(
        app, create, overwrite_existing=False, verify=verify
    )
    return create


def _component_name(component: Any) -> str:
    for attr in ("name", "composed_name", "instance_name"):
        value = getattr(component, attr, None)
        if value:
            return str(value)
    try:
        return str(component.parameters["DeviceName"])
    except Exception:
        return str(component)


def _parameter_mapping(component: Any) -> Any:
    mapping = getattr(component, "parameters", None)
    if mapping is None:
        raise MaxwellCircuitToolkitError(
            f"Circuit component {_component_name(component)!r} has no parameters mapping."
        )
    return mapping


def set_component_parameter(
    component: Any,
    names: str | Sequence[str],
    value: Scalar,
    *,
    allow_new_key: bool = True,
) -> str:
    """Set one component parameter, matching existing keys case-insensitively.

    Returns the actual key used.  ``names`` can contain aliases such as
    ``("Ron", "RON")`` for compatibility across AEDT releases.
    """
    aliases = [names] if isinstance(names, str) else list(names)
    params = _parameter_mapping(component)
    try:
        keys = list(params.keys())
    except Exception:
        keys = []
    by_lower = {str(k).lower(): str(k) for k in keys}
    chosen = None
    for candidate in aliases:
        if str(candidate).lower() in by_lower:
            chosen = by_lower[str(candidate).lower()]
            break
    if chosen is None:
        if not allow_new_key:
            raise MaxwellCircuitToolkitError(
                f"None of parameter names {aliases!r} exist on {_component_name(component)!r}; "
                f"available={keys!r}"
            )
        chosen = str(aliases[0])
    try:
        params[chosen] = str(value)
    except Exception as exc:
        raise MaxwellCircuitToolkitError(
            f"Could not set {_component_name(component)!r}.{chosen}={value!r}: {exc}"
        ) from exc
    return chosen


def get_winding_components(circuit: Any) -> list[Any]:
    """Return winding components from a Maxwell Circuit, naturally sorted."""
    components = circuit.modeler.schematic.components
    result: list[Any] = []
    iterable = components.values() if hasattr(components, "values") else components
    for component in iterable:
        try:
            info = component.parameters["Info"]
        except Exception:
            info = None
        if str(info).strip().lower() == "winding":
            result.append(component)
    result.sort(key=lambda c: natural_winding_key(_component_name(c)))
    return result


def create_external_circuit_design(
    maxwell_app: Any,
    circuit_design: str,
    *,
    delete_existing: bool = True,
) -> Any:
    """Create a Maxwell Circuit containing all External windings.

    This wraps ``Maxwell2d.create_external_circuit``.  When requested, an
    existing design with the same name is deleted first so repeated builder
    runs are deterministic.
    """
    if delete_existing:
        try:
            design_list = list(getattr(maxwell_app, "design_list", []) or [])
            if circuit_design in design_list:
                maxwell_app.delete_design(circuit_design)
        except Exception as exc:
            raise MaxwellCircuitToolkitError(
                f"Could not delete existing circuit design {circuit_design!r}: {exc}"
            ) from exc
    circuit = maxwell_app.create_external_circuit(circuit_design=circuit_design)
    if not circuit:
        raise MaxwellCircuitToolkitError(
            "Maxwell2d.create_external_circuit() failed. Ensure all requested windings "
            "are Type=External and the Maxwell design is Transient."
        )
    return circuit



def _schematic_editor(circuit: Any) -> Any:
    """Return the native AEDT SchematicEditor object.

    Maxwell Scripting Guide documents ``oDesign.SetActiveEditor("SchematicEditor")``
    as the native entry point used by Schematic Editor commands such as
    GetComponentPins, GetComponentPinLocation, Wire, Rotate and FlipHorizontal.
    """
    design = _script_object(circuit, "_odesign", "odesign")
    try:
        return design.SetActiveEditor("SchematicEditor")
    except Exception as exc:
        raise MaxwellCircuitToolkitError(
            f"Could not activate AEDT SchematicEditor: {exc}"
        ) from exc


def _component_editor_id(component: Any) -> str:
    """Return the AEDT Schematic Editor ID for a CircuitComponent."""
    for attr in ("composed_name", "composedname"):
        value = getattr(component, attr, None)
        if value:
            return str(value)
    # Some PyAEDT/fake objects expose the raw schematic ID directly.
    for attr in ("schematic_id", "id"):
        value = getattr(component, attr, None)
        if value is not None and str(value):
            return str(value)
    raise MaxwellCircuitToolkitError(
        f"Could not determine Schematic Editor ID for {_component_name(component)!r}."
    )


def get_component_pin_names(circuit: Any, component: Any) -> list[str]:
    """Return native pin names using ``oEditor.GetComponentPins``.

    The function returns the native AEDT pin identifiers for diagnostics and
    one-terminal schematic objects.  v0.8.0 does not infer electrical meaning
    from returned order, spelling, or coordinates for supported components.
    """
    editor = _schematic_editor(circuit)
    comp_id = _component_editor_id(component)
    try:
        pins = list(editor.GetComponentPins(comp_id) or [])
    except Exception as exc:
        raise MaxwellCircuitToolkitError(
            f"GetComponentPins failed for {comp_id!r}: {exc}"
        ) from exc
    if not pins:
        raise MaxwellCircuitToolkitError(f"Component {comp_id!r} exposes no native schematic pins.")
    return [str(p) for p in pins]


def _parse_editor_coord(value: Any) -> float:
    """Parse GetComponentPinLocation output (number or 'X=number'/'Y=number')."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if "=" in text:
        text = text.split("=", 1)[1].strip()
    # AEDT returns schematic coordinates as plain SI numbers here.
    return float(text)


def get_component_pin_location(circuit: Any, component: Any, pin_name: str) -> tuple[float, float]:
    """Return one native schematic pin location with GetComponentPinLocation."""
    editor = _schematic_editor(circuit)
    comp_id = _component_editor_id(component)
    try:
        x = _parse_editor_coord(editor.GetComponentPinLocation(comp_id, str(pin_name), True))
        y = _parse_editor_coord(editor.GetComponentPinLocation(comp_id, str(pin_name), False))
    except Exception as exc:
        raise MaxwellCircuitToolkitError(
            f"GetComponentPinLocation failed for {comp_id!r}/{pin_name!r}: {exc}"
        ) from exc
    return x, y


# Fixed native pin identities for the Maxwell Circuit components used by this toolkit.
#
# SW_V4 was established by the user's four-distinct-net experiment.  These
# names are identities, not screen positions; Rotate changes only coordinates.
SW_V4_MAIN_1_PIN = "n1"
SW_V4_MAIN_2_PIN = "n2"
SW_V4_CONTROL_PLUS_PIN = "n3"
SW_V4_CONTROL_MINUS_PIN = "n4"

# The 2026-08-08 AEDT component-library survey established the native names
# and terminal order for every component used below.  These are fixed identities.
WINDING_LEFT_PIN = "n1"
WINDING_RIGHT_PIN = "n2"
RESISTOR_LEFT_PIN = "n1"
RESISTOR_RIGHT_PIN = "n2"
DIODE_ANODE_PIN = "n1"
DIODE_CATHODE_PIN = "n2"
VPULSE_POSITIVE_PIN = "n1"
VPULSE_NEGATIVE_PIN = "n2"
VPWL_POSITIVE_PIN = "n1"
VPWL_NEGATIVE_PIN = "n2"

# Passive Elements:Cap is defined as Terminal n1=%0 and n2=%1.  The user
# requires each storage capacitor to be rotated once (+90 deg CCW).  The AEDT
# survey shows that after this exact rotation n2 is the upper pin and n1 the
# lower pin.  We therefore assign fixed circuit roles (not inferred semantics):
# upper n2 -> stage input/positive node, lower n1 -> ESR/negative branch.
CAPACITOR_STAGE_NODE_PIN = "n2"
CAPACITOR_ESR_PIN = "n1"
ESR_CAPACITOR_PIN = "n2"
ESR_GROUND_PIN = "n1"


def _ground_pin_name(component: Any) -> str:
    """Return PyAEDT's synthetic Ground/GPort pin name directly."""
    return str(component.pins[0].name)


def rotate_ccw_once(circuit: Any, component: Any) -> None:
    """Apply one +90 degree counter-clockwise Schematic Editor rotation.

    Used for the fixed VPULSE shunt resistor and, in v0.9.0, for each
    storage capacitor and its ESR resistor.  The rotation changes geometry
    only; native pin identities remain fixed.
    """
    editor = _schematic_editor(circuit)
    comp_id = _component_editor_id(component)
    selections = ["NAME:Selections", "Selections:=", [comp_id]]
    try:
        editor.Rotate(
            selections,
            [
                "NAME:RotateParameters",
                "Degrees:=",
                90,
                "Disconnect:=",
                False,
                "Rubberband:=",
                False,
            ],
        )
    except Exception as exc:
        raise MaxwellCircuitToolkitError(
            f"Could not rotate {_component_name(component)!r} once CCW: {exc}"
        ) from exc
    if hasattr(component, "_pins"):
        try:
            component._pins = []
        except Exception:
            pass


def rotate_ccw_three_times(circuit: Any, component: Any) -> None:
    """Apply three discrete +90-degree rotations without mirroring.

    This matches the manual GUI sequence confirmed for SW_V4 and is also used
    to orient the simulated-SCR diode vertically while preserving native pin
    identities.
    """
    editor = _schematic_editor(circuit)
    comp_id = _component_editor_id(component)
    selections = ["NAME:Selections", "Selections:=", [comp_id]]
    try:
        for _ in range(3):
            editor.Rotate(
                selections,
                [
                    "NAME:RotateParameters",
                    "Degrees:=",
                    90,
                    "Disconnect:=",
                    False,
                    "Rubberband:=",
                    False,
                ],
            )
    except Exception as exc:
        raise MaxwellCircuitToolkitError(
            f"Could not rotate {_component_name(component)!r} three times CCW: {exc}"
        ) from exc

    if hasattr(component, "_pins"):
        try:
            component._pins = []
        except Exception:
            pass


def rotate_ccw_and_flip_horizontal(circuit: Any, component: Any) -> None:
    """Legacy transform retained for API compatibility.

    New accelerator topologies must use :func:`rotate_ccw_three_times`.
    """
    editor = _schematic_editor(circuit)
    comp_id = _component_editor_id(component)
    selections = ["NAME:Selections", "Selections:=", [comp_id]]
    try:
        editor.Rotate(
            selections,
            [
                "NAME:RotateParameters", "Degrees:=", 90,
                "Disconnect:=", False, "Rubberband:=", False,
            ],
        )
        editor.FlipHorizontal(
            selections,
            ["NAME:FlipParameters", "Disconnect:=", False, "Rubberband:=", False],
        )
    except Exception as exc:
        raise MaxwellCircuitToolkitError(
            f"Could not apply legacy rotate/flip to {_component_name(component)!r}: {exc}"
        ) from exc
    if hasattr(component, "_pins"):
        try:
            component._pins = []
        except Exception:
            pass

def _pin_object(component: Any, pin_name: str) -> Any:
    """Return a CircuitPins-like object by *name*, never by list index."""
    try:
        pins = list(component.pins)
    except Exception as exc:
        raise MaxwellCircuitToolkitError(
            f"Could not read pins on {_component_name(component)!r}: {exc}"
        ) from exc
    for pin in pins:
        if str(getattr(pin, "name", "")) == str(pin_name):
            return pin
    raise MaxwellCircuitToolkitError(
        f"Pin {pin_name!r} not found on {_component_name(component)!r}; "
        f"available={[getattr(p, 'name', '') for p in pins]!r}."
    )


def _pin_xy_in_schematic_units(pin: Any) -> tuple[float, float]:
    try:
        loc = list(pin.location)
        if len(loc) != 2:
            raise ValueError(loc)
        return float(loc[0]), float(loc[1])
    except Exception as exc:
        raise MaxwellCircuitToolkitError(
            f"Could not read schematic pin location for {getattr(pin, 'name', '<pin>')!r}: {exc}"
        ) from exc


def _create_wire_points(circuit: Any, points: Sequence[Sequence[float]], *, label: str) -> Any:
    """Create one real schematic wire with PyAEDT ``create_wire``.

    PyAEDT 0.25 maps this directly to Schematic Editor ``oEditor.CreateWire``
    and performs the schematic-unit-to-meter conversion required by AEDT.
    """
    clean: list[list[float]] = []
    for p in points:
        pt = [float(p[0]), float(p[1])]
        if not clean or pt != clean[-1]:
            clean.append(pt)
    if len(clean) < 2:
        raise MaxwellCircuitToolkitError(f"Degenerate wire requested for {label}: {clean!r}")
    try:
        wire = circuit.modeler.schematic.create_wire(clean)
    except Exception as exc:
        raise MaxwellCircuitToolkitError(f"CreateWire failed for {label}: {exc}") from exc
    if not wire:
        raise MaxwellCircuitToolkitError(f"CreateWire returned failure for {label}: {clean!r}")
    return wire


def _schematic_unit_meter_scale(circuit: Any) -> float:
    """Return meters per current schematic drawing unit.

    ``GetComponentPinLocation`` returns native editor coordinates in meters,
    while PyAEDT ``create_wire(points)`` expects points in ``schematic_units``
    and converts them back to meters before calling ``oEditor.CreateWire``.
    """
    unit = str(
        getattr(getattr(circuit, "modeler", None), "schematic_units", "")
        or getattr(getattr(getattr(circuit, "modeler", None), "schematic", None), "schematic_units", "")
        or "meter"
    ).strip().lower()
    scales = {
        "meter": 1.0, "m": 1.0,
        "mm": 1e-3, "millimeter": 1e-3,
        "cm": 1e-2, "centimeter": 1e-2,
        "um": 1e-6, "micrometer": 1e-6,
        "nm": 1e-9, "nanometer": 1e-9,
        "in": 0.0254, "inch": 0.0254,
        "mil": 0.0000254,
    }
    if unit not in scales:
        raise MaxwellCircuitToolkitError(
            f"Unsupported schematic unit {unit!r} while converting native pin coordinates."
        )
    return scales[unit]


def _native_pin_xy_in_schematic_units(
    circuit: Any, component: Any, pin_name: str
) -> tuple[float, float]:
    """Get a transformed native pin coordinate and convert meters to sheet units."""
    x_m, y_m = get_component_pin_location(circuit, component, pin_name)
    scale = _schematic_unit_meter_scale(circuit)
    return x_m / scale, y_m / scale


def _wire_endpoint_xy(circuit: Any, component: Any, pin_name: str) -> tuple[float, float]:
    """Return the authoritative endpoint coordinate used for drawing wires.

    For ordinary schematic components, use Schematic Editor
    ``GetComponentPinLocation`` and convert its meter coordinates into the
    active schematic drawing units.  This avoids a real PyAEDT/AEDT failure
    observed after SW_V4 rotation where ``CircuitPins.location`` pointed at a
    neighbouring transformed pin and both switch terminal pairs were shorted.

    GPort/PagePort/IPort objects are different: ``GetComponentPins`` can be
    empty and PyAEDT intentionally exposes a synthetic port pin, so retain the
    synthetic ``CircuitPins.location`` path for those objects.
    """
    comp_id = _component_editor_id(component)
    if "Port@" in comp_id:
        return _pin_xy_in_schematic_units(_pin_object(component, pin_name))
    return _native_pin_xy_in_schematic_units(circuit, component, pin_name)

def wire_component_pins(
    circuit: Any,
    first_component: Any,
    first_pin: str,
    second_component: Any,
    second_pin: str,
    *,
    route: str = "manhattan_x_first",
) -> Any:
    """Wire two *named* pins with explicit geometric wire points.

    Pin identities are supplied explicitly by the caller.  The function only
    resolves their current post-transform coordinates and calls PyAEDT
    ``create_wire`` (native ``oEditor.CreateWire``); it performs no pin-semantic
    inference or remapping.

    ``route='straight'`` creates one segment.  The default Manhattan route goes
    horizontally from the first pin to the second pin's X coordinate and then
    vertically to the second pin.  If the pins already share X or Y, a straight
    wire is used.
    """
    # Use native editor coordinates for ordinary components.  Do not use
    # CircuitPins.location here after symbol transforms; real AEDT runs showed
    # that stale PyAEDT pin coordinates can cause a wire intended for one pin to
    # land on its neighbour, shorting the SW_V4 main/control pairs.
    a = _wire_endpoint_xy(circuit, first_component, first_pin)
    b = _wire_endpoint_xy(circuit, second_component, second_pin)
    tol = 1e-9
    if route == "straight" or abs(a[0] - b[0]) < tol or abs(a[1] - b[1]) < tol:
        points = [a, b]
    elif route == "manhattan_y_first":
        points = [a, [a[0], b[1]], b]
    else:
        points = [a, [b[0], a[1]], b]
    return _create_wire_points(
        circuit,
        points,
        label=f"{_component_name(first_component)}:{first_pin} -> "
        f"{_component_name(second_component)}:{second_pin}",
    )

def create_voltage_switch_model(
    circuit: Any,
    *,
    name: str = _DEFAULT_SWITCH["model_name"],
    ron: str = _DEFAULT_SWITCH["Ron"],
    roff: str = _DEFAULT_SWITCH["Roff"],
    von: str = _DEFAULT_SWITCH["Von"],
    voff: str = _DEFAULT_SWITCH["Voff"],
    location: Sequence[float] = (0.0, 0.0),
) -> Any:
    """Place one shared ``SW_VModel`` and set Ron/Roff/Von/Voff."""
    model = circuit.modeler.schematic.create_component(
        name=name,
        component_library="Passive Elements",
        component_name="SW_VModel",
        location=list(location),
        angle=0,
    )
    if not model:
        raise MaxwellCircuitToolkitError("Failed to create SW_VModel component.")
    # ``name=`` sets the schematic instance label only.  The model definition
    # itself is exported through @DeviceName, whose library default is
    # literally ``ModelName``.  Set DeviceName explicitly so every SW_V4 MOD
    # reference resolves to the intended shared model name.
    set_component_parameter(model, ("DeviceName",), name)
    set_component_parameter(model, ("Ron", "RON"), ron)
    set_component_parameter(model, ("Roff", "ROFF"), roff)
    set_component_parameter(model, ("Von", "VON"), von)
    set_component_parameter(model, ("Voff", "VOFF"), voff)
    return model




def create_resistor(
    circuit: Any,
    *,
    name: str,
    value: str,
    location: Sequence[float],
    angle: int | float = 0,
) -> Any:
    """Place a Maxwell Circuit resistor with a numeric or AEDT-expression value."""
    schematic = circuit.modeler.schematic
    creator = getattr(schematic, "create_resistor", None)
    if creator is not None:
        resistor = creator(name=name, value=value, location=list(location), angle=angle)
    else:
        resistor = schematic.create_component(
            name=name,
            component_library="Passive Elements",
            component_name="Res",
            location=list(location),
            angle=angle,
        )
        if resistor:
            set_component_parameter(resistor, ("R", "Resistance"), value)
    if not resistor:
        raise MaxwellCircuitToolkitError(f"Failed to create resistor {name!r}.")
    set_component_parameter(resistor, ("R", "Resistance"), value)
    return resistor


def create_storage_capacitor(
    circuit: Any,
    *,
    name: str,
    capacitance: str,
    initial_voltage: str,
    location: Sequence[float],
    angle: int | float = 0,
) -> Any:
    """Create one storage capacitor and rotate it exactly once CCW.

    The fixed AEDT native terminal names are n1/n2.  After the required +90°
    rotation, n2 is physically above n1.  The generator wires n2 to the stage
    input node and n1 to the ESR branch.
    """
    capacitor = circuit.modeler.schematic.create_component(
        name=name,
        component_library="Passive Elements",
        component_name="Cap",
        location=list(location),
        angle=angle,
    )
    if not capacitor:
        raise MaxwellCircuitToolkitError(f"Failed to create capacitor {name!r}.")
    set_component_parameter(capacitor, ("C", "Capacitance"), capacitance)
    set_component_parameter(capacitor, ("IC",), initial_voltage)
    rotate_ccw_once(circuit, capacitor)
    return capacitor


def create_storage_esr_resistor(
    circuit: Any,
    *,
    name: str,
    value: str,
    location: Sequence[float],
    angle: int | float = 0,
) -> Any:
    """Create one storage-capacitor ESR resistor and rotate it once CCW."""
    resistor = create_resistor(
        circuit, name=name, value=value, location=location, angle=angle
    )
    rotate_ccw_once(circuit, resistor)
    return resistor


def create_vpulse_shunt_resistor(
    circuit: Any,
    *,
    name: str,
    location: Sequence[float],
    value: str = _DEFAULT_PULSE["shunt_resistance"],
    angle: int | float = 0,
) -> Any:
    """Create the VPULSE shunt and rotate it once counter-clockwise.

    The accelerator generator always calls this with ``angle=0``.  One native
    +90 degree Rotate is then applied so the resistor is vertical while its
    fixed native pins remain n1/n2.
    """
    resistor = create_resistor(
        circuit, name=name, value=value, location=location, angle=angle
    )
    rotate_ccw_once(circuit, resistor)
    return resistor


def create_coil_series_resistor(
    circuit: Any,
    *,
    name: str,
    value: str,
    location: Sequence[float],
    angle: int | float = 0,
) -> Any:
    """Place the explicit coil DC-resistance element, usually ``$coil_R_n``."""
    return create_resistor(
        circuit, name=name, value=value, location=location, angle=angle
    )


def create_diode_model(
    circuit: Any,
    *,
    name: str = _DEFAULT_DIODE["model_name"],
    parameters: Mapping[str, Scalar] | None = None,
    location: Sequence[float] = (0.0, 0.0),
) -> Any:
    """Place one shared Maxwell Circuit ``DIODE_Model`` data element.

    AEDT requires every DIODE to reference a diode model through ``MOD``.
    One model data element may be shared by any number of diode instances.
    Native AEDT defaults are retained for parameters not supplied by the caller.
    """
    model = circuit.modeler.schematic.create_component(
        name=name,
        component_library="Passive Elements",
        component_name="DIODE_Model",
        location=list(location),
        angle=0,
    )
    if not model:
        raise MaxwellCircuitToolkitError("Failed to create DIODE_Model component.")
    # As with SW_VModel, the schematic instance name does not populate the
    # model-data ``DeviceName`` parameter.  DIODE instances reference this
    # exact string through MOD, so set it explicitly instead of leaving the
    # AEDT library default ``ModelName``.
    set_component_parameter(model, ("DeviceName",), name)
    model_parameters = _DEFAULT_DIODE_PARAMETERS if parameters is None else parameters
    for key, value in model_parameters.items():
        set_component_parameter(model, (str(key),), value, allow_new_key=False)
    return model


def create_diode(
    circuit: Any,
    *,
    name: str,
    model_name: str,
    location: Sequence[float],
    angle: int | float = 0,
) -> Any:
    """Place a Maxwell Circuit DIODE and reference ``model_name`` through MOD."""
    schematic = circuit.modeler.schematic
    creator = getattr(schematic, "create_diode", None)
    if creator is not None:
        diode = creator(name=name, location=list(location), angle=angle)
    else:
        diode = schematic.create_component(
            name=name,
            component_library="Passive Elements",
            component_name="DIODE",
            location=list(location),
            angle=angle,
        )
    if not diode:
        raise MaxwellCircuitToolkitError(f"Failed to create diode {name!r}.")
    set_component_parameter(diode, ("MOD", "Mod", "mod"), model_name)
    return diode


def create_ground(
    circuit: Any,
    *,
    location: Sequence[float],
    angle: int | float = 0,
) -> Any:
    """Create a Maxwell Circuit ground symbol using the public schematic API."""
    ground = circuit.modeler.schematic.create_gnd(location=list(location), angle=angle)
    if not ground:
        raise MaxwellCircuitToolkitError("Failed to create circuit ground.")
    return ground


def create_voltage_controlled_switch(
    circuit: Any,
    *,
    name: str,
    model_name: str,
    location: Sequence[float],
    angle: int | float = 0,
) -> Any:
    """Place ``SW_V4`` and point its MOD parameter at the shared model."""
    switch = circuit.modeler.schematic.create_component(
        name=name,
        component_library="Passive Elements",
        component_name="SW_V4",
        location=list(location),
        angle=angle,
    )
    if not switch:
        raise MaxwellCircuitToolkitError(f"Failed to create voltage-controlled switch {name!r}.")
    set_component_parameter(switch, ("MOD", "Mod", "mod"), model_name)
    # Visual placement only: three separate +90 degree counter-clockwise
    # Rotate operations, no flip. Pin identities remain the fixed n1..n4 map.
    rotate_ccw_three_times(circuit, switch)
    return switch


def create_position_vpulse(
    circuit: Any,
    *,
    name: str,
    td: str,
    pw: str,
    location: Sequence[float],
    period: str = _DEFAULT_PULSE["Period"],
    v1: str = _DEFAULT_PULSE["V1"],
    v2: str = _DEFAULT_PULSE["V2"],
    tr: str = _DEFAULT_PULSE["Tr"],
    tf: str = _DEFAULT_PULSE["Tf"],
    angle: int | float = 0,
) -> Any:
    """Place a Maxwell ``VPulse`` whose independent variable is position."""
    pulse = circuit.modeler.schematic.create_component(
        name=name,
        component_library="Sources",
        component_name="VPulse",
        location=list(location),
        angle=angle,
    )
    if not pulse:
        raise MaxwellCircuitToolkitError(f"Failed to create VPulse {name!r}.")
    for key, aliases, val in (
        ("Type", ("Type", "TYPE"), "POS"),
        ("V1", ("V1",), v1),
        ("V2", ("V2",), v2),
        ("Td", ("Td", "TD"), td),
        ("Tr", ("Tr", "TR"), tr),
        ("Tf", ("Tf", "TF"), tf),
        ("Pw", ("Pw", "PW"), pw),
        ("Period", ("Period", "PERIOD"), period),
    ):
        set_component_parameter(pulse, aliases, val)
    return pulse


def create_position_vpwl(
    circuit: Any,
    *,
    name: str,
    points: Sequence[tuple[str, str]],
    location: Sequence[float],
    angle: int | float = 0,
) -> Any:
    """Place a position-controlled VPWL using the surveyed AEDT property names."""
    if not 1 <= len(points) <= 20:
        raise ValueError("VPWL requires between 1 and 20 position-voltage points.")
    source = circuit.modeler.schematic.create_component(
        name=name,
        component_library="Sources",
        component_name="VPWL",
        location=list(location),
        angle=angle,
    )
    if not source:
        raise MaxwellCircuitToolkitError(f"Failed to create VPWL {name!r}.")
    set_component_parameter(source, ("Type", "TYPE"), "POS", allow_new_key=False)
    for index, (position, voltage) in enumerate(points, start=1):
        set_component_parameter(source, f"T{index}", position, allow_new_key=False)
        set_component_parameter(source, f"V{index}", voltage, allow_new_key=False)
    return source


def connect_winding_to_switch(circuit: Any, winding: Any, switch: Any) -> None:
    """Connect fixed Winding n2 to fixed SW_V4 main terminal n1."""
    wire_component_pins(circuit, winding, WINDING_RIGHT_PIN, switch, SW_V4_MAIN_1_PIN)


def connect_vpulse_to_switch_control(circuit: Any, pulse: Any, switch: Any) -> None:
    """Connect a surveyed two-terminal voltage source to the SW_V4 control pair.

    Fixed mapping established by the user's four-distinct-net experiment:
    n3 is control+, n4 is control-.
    """
    wire_component_pins(
        circuit, pulse, VPULSE_POSITIVE_PIN, switch, SW_V4_CONTROL_PLUS_PIN,
        route="manhattan_y_first",
    )
    wire_component_pins(
        circuit, pulse, VPULSE_NEGATIVE_PIN, switch, SW_V4_CONTROL_MINUS_PIN
    )


def connect_resistor_across_vpulse(
    circuit: Any,
    pulse: Any,
    resistor: Any,
) -> None:
    """Connect a resistor directly across source n2(-)/n1(+)."""
    wire_component_pins(
        circuit, pulse, VPULSE_NEGATIVE_PIN, resistor, RESISTOR_LEFT_PIN
    )
    wire_component_pins(
        circuit, pulse, VPULSE_POSITIVE_PIN, resistor, RESISTOR_RIGHT_PIN
    )


def connect_storage_capacitor_branch(
    circuit: Any,
    winding: Any,
    capacitor: Any,
    esr_resistor: Any,
    ground: Any,
) -> None:
    """Connect one fixed storage-capacitor/ESR branch for a Boost stage.

    Winding.n1 is the stage-input node.  For n>1 it is also the node fed by
    DIODE_(n-1).n2; for n=1 it is simply the first-stage input node.

    Both Cap and ESR have already been rotated once, so their fixed roles are::

        Winding.n1 -- Cap.n2
        Cap.n1 -- ESR.n2
        ESR.n1 -- GND

    No pin-order, coordinate, name-candidate, or semantic inference is used.
    """
    gp = _ground_pin_name(ground)
    wire_component_pins(
        circuit, winding, WINDING_LEFT_PIN, capacitor, CAPACITOR_STAGE_NODE_PIN,
        route="manhattan_y_first",
    )
    wire_component_pins(
        circuit, capacitor, CAPACITOR_ESR_PIN, esr_resistor, ESR_CAPACITOR_PIN
    )
    wire_component_pins(
        circuit, esr_resistor, ESR_GROUND_PIN, ground, gp
    )


def connect_single_boost_stage(
    circuit: Any,
    winding: Any,
    resistor: Any,
    switch: Any,
    pulse: Any,
    diode: Any,
    pulse_ground: Any,
    control_ground: Any,
    main_ground: Any,
) -> None:
    """Wire one Boost stage using only fixed native pin identities.

    Fixed topology::

        Winding.n2 -- R_coil.n1
        R_coil.n2 -- DIODE.n1
        R_coil.n2 -- SW_V4.n1       # main switching node (top after 270°)
        SW_V4.n2 -- GND              # bottom main terminal

        VPULSE.n1(+) -- SW_V4.n3    # control +
        SW_V4.n4 -- GND             # control -
        VPULSE.n2(-) -- GND

    No pin-name fallback, coordinate-based semantic inference, geometry sanity
    check, WireId verification, or automatic remapping is performed.
    """
    pg = _ground_pin_name(pulse_ground)
    cg = _ground_pin_name(control_ground)
    mg = _ground_pin_name(main_ground)

    wire_component_pins(
        circuit, winding, WINDING_RIGHT_PIN, resistor, RESISTOR_LEFT_PIN
    )
    wire_component_pins(
        circuit, resistor, RESISTOR_RIGHT_PIN, diode, DIODE_ANODE_PIN
    )
    wire_component_pins(
        circuit, resistor, RESISTOR_RIGHT_PIN, switch, SW_V4_MAIN_1_PIN,
        route="manhattan_y_first",
    )

    wire_component_pins(
        circuit, switch, SW_V4_MAIN_2_PIN, main_ground, mg,
        route="manhattan_y_first",
    )
    wire_component_pins(circuit, switch, SW_V4_CONTROL_MINUS_PIN, control_ground, cg)
    wire_component_pins(circuit, pulse, VPULSE_NEGATIVE_PIN, pulse_ground, pg)
    wire_component_pins(
        circuit, pulse, VPULSE_POSITIVE_PIN, switch, SW_V4_CONTROL_PLUS_PIN,
        route="manhattan_y_first",
    )


def connect_thin_film_scr_stage(
    circuit: Any,
    winding: Any,
    capacitor: Any,
    diode: Any,
    switch: Any,
    pulse: Any,
    main_ground: Any,
    control_ground: Any,
) -> None:
    """Wire one isolated C-Winding-DIODE-SW loop and its VPULSE control."""
    mg = _ground_pin_name(main_ground)
    cg = _ground_pin_name(control_ground)
    wire_component_pins(
        circuit, capacitor, CAPACITOR_STAGE_NODE_PIN, winding, WINDING_LEFT_PIN,
        route="manhattan_y_first",
    )
    wire_component_pins(
        circuit, winding, WINDING_RIGHT_PIN, diode, DIODE_ANODE_PIN
    )
    wire_component_pins(
        circuit, diode, DIODE_CATHODE_PIN, switch, SW_V4_MAIN_1_PIN
    )
    wire_component_pins(
        circuit, switch, SW_V4_MAIN_2_PIN, capacitor, CAPACITOR_ESR_PIN
    )
    wire_component_pins(circuit, capacitor, CAPACITOR_ESR_PIN, main_ground, mg)
    connect_vpulse_to_switch_control(circuit, pulse, switch)
    wire_component_pins(
        circuit, pulse, VPULSE_NEGATIVE_PIN, control_ground, cg
    )


def connect_boost_cascade(circuit: Any, diode: Any, next_winding: Any) -> None:
    """Connect fixed DIODE n2 (cathode) directly to next-Winding n1."""
    wire_component_pins(
        circuit, diode, DIODE_CATHODE_PIN, next_winding, WINDING_LEFT_PIN
    )


def connect_odd_even_boost_cascade(
    circuit: Any, diode: Any, next_same_parity_capacitor: Any
) -> None:
    """Connect DIODE_i.n2 to the positive pin of storage capacitor i+2."""
    wire_component_pins(
        circuit,
        diode,
        DIODE_CATHODE_PIN,
        next_same_parity_capacitor,
        CAPACITOR_STAGE_NODE_PIN,
    )


def connect_last_diode_return(
    circuit: Any,
    diode: Any,
    resistor: Any,
    winding: Any,
    *,
    loop_clearance: float = 300.0,
) -> None:
    """Connect the final diode cathode through ``resistor`` to Winding.n1.

    The return from Res.n2 is routed above the resistor before travelling back
    to the winding.  This avoids drawing a same-height return wire through
    Res.n1, which would electrically short the new resistor in AEDT.
    """
    _connect_diode_return(
        circuit,
        diode,
        resistor,
        winding,
        WINDING_LEFT_PIN,
        loop_clearance=loop_clearance,
    )


def connect_diode_return_to_capacitor(
    circuit: Any,
    diode: Any,
    resistor: Any,
    capacitor: Any,
    *,
    loop_clearance: float = 300.0,
) -> None:
    """Connect a terminal diode through a resistor to a capacitor positive pin."""
    _connect_diode_return(
        circuit,
        diode,
        resistor,
        capacitor,
        CAPACITOR_STAGE_NODE_PIN,
        loop_clearance=loop_clearance,
    )


def _connect_diode_return(
    circuit: Any,
    diode: Any,
    resistor: Any,
    target: Any,
    target_pin: str,
    *,
    loop_clearance: float,
) -> None:
    """Wire a diode/resistor return without routing through either resistor pin."""
    wire_component_pins(
        circuit, diode, DIODE_CATHODE_PIN, resistor, RESISTOR_LEFT_PIN
    )
    a = _wire_endpoint_xy(circuit, resistor, RESISTOR_RIGHT_PIN)
    b = _wire_endpoint_xy(circuit, target, target_pin)
    loop_y = max(a[1], b[1]) + float(loop_clearance)
    _create_wire_points(
        circuit,
        [a, [a[0], loop_y], [b[0], loop_y], b],
        label=(
            f"{_component_name(resistor)}:{RESISTOR_RIGHT_PIN} -> "
            f"{_component_name(target)}:{target_pin}"
        ),
    )


def export_and_assign_external_circuit(
    maxwell_app: Any,
    circuit: Any,
    *,
    circuit_design: str,
    netlist_path: str | Path,
) -> Path:
    """Export Maxwell Circuit to ``.sph`` and assign it to the Maxwell design."""
    path = Path(netlist_path)
    if path.suffix.lower() != ".sph":
        path = path.with_suffix(".sph")
    path.parent.mkdir(parents=True, exist_ok=True)
    ok = circuit.export_netlist_from_schematic(output_file=str(path))
    if ok is False:
        raise MaxwellCircuitToolkitError(f"Failed to export Maxwell Circuit netlist: {path}")
    ok = maxwell_app.edit_external_circuit(
        netlist_file_path=str(path),
        schematic_design_name=circuit_design,
    )
    if not ok:
        raise MaxwellCircuitToolkitError(
            "Maxwell2d.edit_external_circuit() failed after netlist export."
        )
    return path


_PROJECT_VAR_TOKEN = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")


def project_variables_referenced_by_netlist(netlist_path: str | Path) -> list[str]:
    """Return project/global variable tokens referenced by an exported ``.sph``.

    Maxwell project variables are identified by their native leading ``$``.
    The function intentionally derives the parameter list from the exported
    circuit netlist itself rather than from accelerator-specific variable
    names, so the External Circuit Parameter Values update mirrors exactly the
    project-variable references that the circuit actually contains.
    """
    path = Path(netlist_path)
    if not path.is_file():
        raise FileNotFoundError(f"External Circuit netlist does not exist: {path}")
    data = path.read_text(encoding="utf-8", errors="replace")
    names = set(_PROJECT_VAR_TOKEN.findall(data))
    return sorted(names, key=natural_winding_key)


def mirror_external_circuit_project_variables(
    maxwell_app: Any,
    *,
    circuit_design: str,
    netlist_path: str | Path,
) -> dict[str, str]:
    """Map only independent project variables used by Maxwell Circuit to themselves.

    The exported ``.sph`` identifies which ``$...`` project variables are
    actually used by the Circuit design.  That set is intersected with
    PyAEDT's documented ``independent_project_variable_names``.  This is
    deliberate: dependent project variables such as a geometry-derived
    ``$coil_R_n`` may appear as tokens in the netlist but cannot be assigned in
    Edit External Circuit -> Parameter Values and must not be submitted.

    No fallback to all netlist tokens is performed.
    """
    referenced = project_variables_referenced_by_netlist(netlist_path)
    independent = set(get_independent_project_variable_names(maxwell_app))
    names = [name for name in referenced if name in independent]
    if not names:
        raise MaxwellCircuitToolkitError(
            "No independent Maxwell Circuit project/global variables were found "
            f"in {netlist_path}."
        )
    parameters = {name: name for name in names}
    ok = maxwell_app.edit_external_circuit(
        netlist_file_path="",
        schematic_design_name=circuit_design,
        parameters=parameters,
    )
    if not ok:
        raise MaxwellCircuitToolkitError(
            "Maxwell2d.edit_external_circuit(parameters=...) failed while updating "
            "External Circuit Parameter Values."
        )
    return parameters


@dataclass(slots=True)
class AcceleratorCircuitResult:
    circuit: Any
    topology: str = TOPOLOGY_SINGLE_BOOST
    windings: list[Any] = field(default_factory=list)
    switches: list[Any] = field(default_factory=list)
    pulses: list[Any] = field(default_factory=list)
    vpwl_sources: list[Any] = field(default_factory=list)
    vpulse_sources: list[Any] = field(default_factory=list)
    diodes: list[Any] = field(default_factory=list)
    resistors: list[Any] = field(default_factory=list)
    pulse_shunt_resistors: list[Any] = field(default_factory=list)
    storage_capacitors: list[Any] = field(default_factory=list)
    storage_esr_resistors: list[Any] = field(default_factory=list)
    return_resistors: list[Any] = field(default_factory=list)
    last_resistor: Any | None = None
    grounds: list[Any] = field(default_factory=list)
    switch_model: Any | None = None
    diode_model: Any | None = None
    created_variables: dict[str, str] = field(default_factory=dict)
    netlist_path: Path | None = None


@dataclass(slots=True)
class AcceleratorCircuitOptions:
    circuit_design: str = "AcceleratorExternalCircuit"
    topology: str = TOPOLOGY_SINGLE_BOOST
    switch_model_name: str = _DEFAULT_SWITCH["model_name"]
    diode_model_name: str = _DEFAULT_DIODE["model_name"]
    diode_model_parameters: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_DIODE_PARAMETERS)
    )
    von: str = _DEFAULT_SWITCH["Von"]
    voff: str = _DEFAULT_SWITCH["Voff"]
    ron: str = _DEFAULT_SWITCH["Ron"]
    roff: str = _DEFAULT_SWITCH["Roff"]
    v1: str = _DEFAULT_PULSE["V1"]
    v2: str = _DEFAULT_PULSE["V2"]
    tr: str = _DEFAULT_PULSE["Tr"]
    tf: str = _DEFAULT_PULSE["Tf"]
    period: str = _DEFAULT_PULSE["Period"]
    vpwl_fall_delta: str = _DEFAULT_PWL["fall_delta"]
    vpwl_end_position: str = _DEFAULT_PWL["end_position"]
    pulse_shunt_resistance: str = _DEFAULT_PULSE["shunt_resistance"]
    pos_on_prefix: str = _DEFAULT_PREFIXES["pos_on"]
    pos_dur_prefix: str = _DEFAULT_PREFIXES["pos_dur"]
    coil_resistance_prefix: str = _DEFAULT_PREFIXES["r_dc"]
    capacitance_prefix: str = _DEFAULT_PREFIXES["capacitance"]
    esr_prefix: str = _DEFAULT_PREFIXES["esr"]
    initial_voltage_variable: str = _DEFAULT_PREFIXES["initial_voltage"]
    last_resistance_variable: str = _DEFAULT_PREFIXES["last_resistance"]
    fallback_capacitance: str = _DEFAULT_INITIAL["capacitance"]
    fallback_initial_voltage: str = _DEFAULT_INITIAL["initial_voltage"]
    fallback_esr: str = _DEFAULT_INITIAL["esr"]
    fallback_last_resistance: str = _DEFAULT_INITIAL["last_resistance"]
    fallback_pos_on: str = _DEFAULT_INITIAL["pos_on"]
    fallback_pos_dur: str = _DEFAULT_INITIAL["pos_dur"]
    first_stage_position_on: str = "-1000mm"
    projectile_initial_z_variable: str = PROJECTILE_INITIAL_Z_VARIABLE
    fallback_projectile_initial_z: str = PROJECTILE_INITIAL_Z_DEFAULT
    scr_hold_pulse_width: str = SCR_HOLD_PULSE_WIDTH
    delete_existing_circuit: bool = True


def _validate_stage_indices(stage_indices: Sequence[int], *, topology: str) -> None:
    if len(set(stage_indices)) != len(stage_indices):
        raise MaxwellCircuitToolkitError(
            f"Winding stage numbers are not unique: {list(stage_indices)!r}"
        )
    if topology in TOPOLOGY_DEFINITIONS:
        expected = list(range(1, len(stage_indices) + 1))
        if list(stage_indices) != expected:
            raise MaxwellCircuitToolkitError(
                "电路拓扑要求 Winding 编号连续且从 1 开始；"
                f"检测到 {list(stage_indices)!r}，期望 {expected!r}."
            )
    if topology == TOPOLOGY_ODD_EVEN_BOOST and len(stage_indices) < 2:
        raise MaxwellCircuitToolkitError(
            "奇偶Boost至少需要 2 个连续 Winding，才能同时建立奇数链和偶数链。"
        )


def _voltage_property(value: Scalar) -> str:
    """Return a voltage property without adding unit constants."""
    try:
        return normalize_aedt_scalar(value, "voltage")
    except (TypeError, ValueError):
        expression = str(value).strip()
        if "$" not in expression:
            raise
        return expression


def _position_property(value: Scalar) -> str:
    """Return a position property without adding unit constants."""
    try:
        return normalize_aedt_scalar(value, "length")
    except (TypeError, ValueError):
        expression = str(value).strip()
        if "$" not in expression:
            raise
        return expression


def _vpulse_td_expression(stage_index: int, opt: AcceleratorCircuitOptions) -> str:
    base = f"${opt.pos_on_prefix}_{stage_index}"
    if stage_index == 1:
        return base
    offset = str(opt.projectile_initial_z_variable).strip()
    if not offset:
        raise ValueError("projectile_initial_z_variable must not be empty")
    if not offset.startswith("$"):
        offset = "$" + offset
    return f"{base}-{offset}"


def _place_boost_topology(
    circuit: Any,
    windings: Sequence[Any],
    stage_indices: Sequence[int],
    opt: AcceleratorCircuitOptions,
    progress: Progress,
) -> tuple[
    list[Any], list[Any], list[Any], list[Any], list[Any],
    list[Any], list[Any], list[Any], list[Any], Any, Any
]:
    """Place and wire the selected single-path or odd/even Boost topology."""
    # AEDT schematic Y increases upward. Keep Winding/R/DIODE on the upper
    # rail, the switch plus default-vertical VPULSE below it, and grounds lower still.
    # Compact layout: preserve the validated relative arrangement while
    # shortening local leads and stage pitch further for dense multi-stage schematics.
    rail_y = 2400
    control_y = 1800
    ground_y = 1250
    storage_cap_y = 2050
    storage_esr_y = 1500
    storage_ground_y = 950
    parity_row_offset = 3600
    model_y = 3400 + (
        parity_row_offset if opt.topology == TOPOLOGY_ODD_EVEN_BOOST else 0
    )
    stage_pitch = 2400
    winding_x0 = 750
    resistor_dx = 550
    switch_dx = 1050
    pulse_dx = 450
    diode_dx = 1550
    storage_branch_dx = -450

    switch_model = create_voltage_switch_model(
        circuit,
        name=opt.switch_model_name,
        ron=opt.ron,
        roff=opt.roff,
        von=_voltage_property(opt.von),
        voff=_voltage_property(opt.voff),
        location=[950, model_y],
    )
    diode_model = create_diode_model(
        circuit,
        name=opt.diode_model_name,
        parameters=opt.diode_model_parameters,
        location=[2300, model_y],
    )

    switches: list[Any] = []
    pulses: list[Any] = []
    diodes: list[Any] = []
    resistors: list[Any] = []
    pulse_shunts: list[Any] = []
    storage_capacitors: list[Any] = []
    storage_esr_resistors: list[Any] = []
    grounds: list[Any] = []
    stage_layout: dict[int, tuple[float, float]] = {}

    for row_number, (stage_i, winding) in enumerate(zip(stage_indices, windings), start=1):
        _report(
            progress,
            f"正在建立{topology_display_name(opt.topology)}第 {stage_i} 级"
            f"（{row_number}/{len(windings)}）…",
        )
        if opt.topology == TOPOLOGY_ODD_EVEN_BOOST:
            # Lay out both electrical chains as separate rows. Odd stages are
            # on the upper row, even stages on the lower row; within each row
            # consecutive same-parity stages are adjacent on the schematic.
            chain_position = (stage_i - 1) // 2
            row_offset = parity_row_offset if stage_i % 2 else 0
        else:
            chain_position = row_number - 1
            row_offset = 0
        winding_x = winding_x0 + chain_position * stage_pitch
        stage_rail_y = rail_y + row_offset
        stage_layout[stage_i] = (winding_x, stage_rail_y)
        try:
            winding.location = [winding_x, stage_rail_y]
        except Exception:
            pass

        resistor = create_coil_series_resistor(
            circuit,
            name=f"R_coil_{stage_i}",
            value=f"${opt.coil_resistance_prefix}_{stage_i}",
            location=[winding_x + resistor_dx, stage_rail_y],
            angle=0,
        )
        switch = create_voltage_controlled_switch(
            circuit,
            name=f"SW_{stage_i}",
            model_name=opt.switch_model_name,
            location=[winding_x + switch_dx, control_y + row_offset],
            angle=0,
        )
        # The pin survey confirms that VPWL and VPULSE at 0 degrees both use
        # n1 as the upper positive pin and n2 as the lower negative pin.
        if stage_i == 1:
            pulse = create_position_vpwl(
                circuit,
                name="VPWL_1",
                points=(
                    (f"${opt.pos_on_prefix}_1", _voltage_property(opt.v2)),
                    (f"${opt.pos_dur_prefix}_1", _voltage_property(opt.v2)),
                    (
                        f"${opt.pos_dur_prefix}_1+"
                        f"{normalize_aedt_scalar(opt.vpwl_fall_delta, 'length')}",
                        _voltage_property(opt.v1),
                    ),
                    (_position_property(opt.vpwl_end_position), _voltage_property(opt.v1)),
                ),
                location=[winding_x + pulse_dx, control_y + row_offset],
                angle=0,
            )
        else:
            pulse = create_position_vpulse(
                circuit,
                name=f"VPULSE_{stage_i}",
                td=_vpulse_td_expression(stage_i, opt),
                pw=f"${opt.pos_dur_prefix}_{stage_i}",
                period=_position_property(opt.period),
                v1=_voltage_property(opt.v1),
                v2=_voltage_property(opt.v2),
                tr=opt.tr,
                tf=opt.tf,
                location=[winding_x + pulse_dx, control_y + row_offset],
                angle=0,
            )
        pulse_shunt = create_vpulse_shunt_resistor(
            circuit,
            name=f"R_VPULSE_{stage_i}",
            value=opt.pulse_shunt_resistance,
            location=[winding_x + pulse_dx - 350, control_y + row_offset],
            angle=0,
        )
        connect_resistor_across_vpulse(circuit, pulse, pulse_shunt)
        diode = create_diode(
            circuit,
            name=f"DIODE_{stage_i}",
            model_name=opt.diode_model_name,
            location=[winding_x + diode_dx, stage_rail_y],
            angle=0,
        )

        # Per-stage storage branch.  Cap and ESR are both placed at 0° and
        # then rotated exactly once +90° CCW.  Their fixed post-rotation pin
        # roles are Cap.n2(stage node)/n1(ESR) and ESR.n2(cap)/n1(ground).
        vinit_ref = str(opt.initial_voltage_variable).strip()
        if not vinit_ref.startswith("$"):
            vinit_ref = "$" + vinit_ref
        storage_capacitor = create_storage_capacitor(
            circuit,
            name=f"C_storage_{stage_i}",
            capacitance=f"${opt.capacitance_prefix}_{stage_i}",
            initial_voltage=vinit_ref,
            location=[winding_x + storage_branch_dx, storage_cap_y + row_offset],
            angle=0,
        )
        storage_esr = create_storage_esr_resistor(
            circuit,
            name=f"R_ESR_{stage_i}",
            value=f"${opt.esr_prefix}_{stage_i}",
            location=[winding_x + storage_branch_dx, storage_esr_y + row_offset],
            angle=0,
        )
        storage_ground = create_ground(
            circuit,
            location=[winding_x + storage_branch_dx, storage_ground_y + row_offset],
            angle=0,
        )
        connect_storage_capacitor_branch(
            circuit, winding, storage_capacitor, storage_esr, storage_ground
        )

        sw_n2_xy = _native_pin_xy_in_schematic_units(
            circuit, switch, SW_V4_MAIN_2_PIN
        )
        sw_control_minus_xy = _native_pin_xy_in_schematic_units(
            circuit, switch, SW_V4_CONTROL_MINUS_PIN
        )
        pulse_minus_xy = _native_pin_xy_in_schematic_units(
            circuit, pulse, VPULSE_NEGATIVE_PIN
        )

        # Ground placement is purely geometric. Electrical pin identities are
        # fixed constants and are never inferred from these coordinates.
        ground_side_offset = 140
        pulse_ground = create_ground(
            circuit, location=[pulse_minus_xy[0], ground_y + row_offset], angle=0
        )
        control_ground = create_ground(
            circuit,
            location=[sw_control_minus_xy[0] - ground_side_offset, ground_y + row_offset],
            angle=0,
        )
        main_ground = create_ground(
            circuit,
            location=[sw_n2_xy[0] + ground_side_offset, ground_y + row_offset],
            angle=0,
        )

        connect_single_boost_stage(
            circuit,
            winding,
            resistor,
            switch,
            pulse,
            diode,
            pulse_ground,
            control_ground,
            main_ground,
        )
        switches.append(switch)
        pulses.append(pulse)
        diodes.append(diode)
        resistors.append(resistor)
        pulse_shunts.append(pulse_shunt)
        storage_capacitors.append(storage_capacitor)
        storage_esr_resistors.append(storage_esr)
        grounds.extend([pulse_ground, control_ground, main_ground, storage_ground])

    if opt.topology == TOPOLOGY_SINGLE_BOOST:
        # DIODE_n.n2 feeds Winding_(n+1).n1, which is also that stage's
        # storage-capacitor positive node.
        for diode, next_winding in zip(diodes[:-1], windings[1:]):
            connect_boost_cascade(circuit, diode, next_winding)
    elif opt.topology == TOPOLOGY_ODD_EVEN_BOOST:
        # Split the cascade into odd and even chains:
        # DIODE_i.n2 -> C_(i+2).positive for every existing i+2 stage.
        for diode, next_capacitor in zip(diodes[:-2], storage_capacitors[2:]):
            connect_odd_even_boost_cascade(circuit, diode, next_capacitor)
    else:  # topology_display_name() validates before placement; defensive only.
        raise ValueError(f"Unsupported circuit topology: {opt.topology!r}")

    last_variable = str(opt.last_resistance_variable).strip()
    if not last_variable.startswith("$"):
        last_variable = "$" + last_variable
    return_resistors: list[Any] = []
    if opt.topology == TOPOLOGY_SINGLE_BOOST:
        last_stage = stage_indices[-1]
        last_winding_x, last_rail_y = stage_layout[last_stage]
        resistor = create_resistor(
            circuit,
            name="R_last",
            value=last_variable,
            location=[last_winding_x + diode_dx + 600, last_rail_y + 450],
            angle=0,
        )
        connect_last_diode_return(
            circuit, diodes[-1], resistor, windings[-1]
        )
        return_resistors.append(resistor)
    else:
        # Close both parity chains independently. For each chain, its terminal
        # diode returns through an Rlast-valued resistor to the positive pin of
        # that same terminal stage's storage capacitor.
        stage_position = {stage: pos for pos, stage in enumerate(stage_indices)}
        parity_terminals: list[tuple[int, str]] = []
        for parity, label in ((1, "odd"), (0, "even")):
            chain = [stage for stage in stage_indices if stage % 2 == parity]
            parity_terminals.append((chain[-1], label))
        for terminal_stage, label in sorted(parity_terminals):
            terminal_pos = stage_position[terminal_stage]
            terminal_x, terminal_rail_y = stage_layout[terminal_stage]
            resistor = create_resistor(
                circuit,
                name=f"R_last_{label}",
                value=last_variable,
                location=[terminal_x + diode_dx + 600, terminal_rail_y + 450],
                angle=0,
            )
            connect_diode_return_to_capacitor(
                circuit,
                diodes[terminal_pos],
                resistor,
                storage_capacitors[terminal_pos],
            )
            return_resistors.append(resistor)

    return (
        switches, pulses, diodes, resistors, pulse_shunts,
        storage_capacitors, storage_esr_resistors, grounds,
        return_resistors, switch_model, diode_model,
    )


def _place_thin_film_scr_topology(
    circuit: Any,
    windings: Sequence[Any],
    stage_indices: Sequence[int],
    opt: AcceleratorCircuitOptions,
    progress: Progress,
) -> tuple[
    list[Any], list[Any], list[Any], list[Any], list[Any],
    list[Any], list[Any], list[Any], list[Any], Any, Any
]:
    """Place independent capacitor-Winding-diode-switch loops."""
    switch_model = create_voltage_switch_model(
        circuit,
        name=opt.switch_model_name,
        ron=opt.ron,
        roff=opt.roff,
        von=_voltage_property(opt.von),
        voff=_voltage_property(opt.voff),
        location=[900, 4300],
    )
    diode_model = create_diode_model(
        circuit,
        name=opt.diode_model_name,
        parameters=opt.diode_model_parameters,
        location=[2300, 4300],
    )
    vinit_ref = str(opt.initial_voltage_variable).strip()
    if not vinit_ref.startswith("$"):
        vinit_ref = "$" + vinit_ref

    switches: list[Any] = []
    pulses: list[Any] = []
    diodes: list[Any] = []
    capacitors: list[Any] = []
    grounds: list[Any] = []
    for row_number, (stage_i, winding) in enumerate(
        zip(stage_indices, windings), start=1
    ):
        _report(
            progress,
            f"\u6b63\u5728\u5efa\u7acb{topology_display_name(opt.topology)}\u7b2c {stage_i} \u7ea7"
            f"\uff08{row_number}/{len(windings)}\uff09\u2026",
        )
        base_x = 650 + (row_number - 1) * 3200
        winding.location = [base_x + 850, 3000]
        capacitor = create_storage_capacitor(
            circuit,
            name=f"C_film_{stage_i}",
            capacitance=f"${opt.capacitance_prefix}_{stage_i}",
            initial_voltage=vinit_ref,
            location=[base_x, 2450],
            angle=0,
        )
        diode = create_diode(
            circuit,
            name=f"DIODE_{stage_i}",
            model_name=opt.diode_model_name,
            location=[base_x + 1700, 2700],
            angle=0,
        )
        rotate_ccw_three_times(circuit, diode)
        switch = create_voltage_controlled_switch(
            circuit,
            name=f"SW_{stage_i}",
            model_name=opt.switch_model_name,
            location=[base_x + 1700, 1900],
            angle=0,
        )
        pulse = create_position_vpulse(
            circuit,
            name=f"VPULSE_{stage_i}",
            td=_vpulse_td_expression(stage_i, opt),
            pw=_position_property(opt.scr_hold_pulse_width),
            period=_position_property(opt.period),
            v1=_voltage_property(opt.v1),
            v2=_voltage_property(opt.v2),
            tr=opt.tr,
            tf=opt.tf,
            location=[base_x + 950, 1550],
            angle=0,
        )
        main_ground = create_ground(circuit, location=[base_x, 850], angle=0)
        control_ground = create_ground(
            circuit, location=[base_x + 950, 850], angle=0
        )
        connect_thin_film_scr_stage(
            circuit,
            winding,
            capacitor,
            diode,
            switch,
            pulse,
            main_ground,
            control_ground,
        )
        switches.append(switch)
        pulses.append(pulse)
        diodes.append(diode)
        capacitors.append(capacitor)
        grounds.extend([main_ground, control_ground])

    return (
        switches, pulses, diodes, [], [], capacitors, [], grounds, [],
        switch_model, diode_model,
    )


def build_accelerator_external_circuit(
    maxwell_app: Any,
    *,
    options: AcceleratorCircuitOptions | None = None,
    netlist_path: str | Path | None = None,
    progress: Progress = None,
) -> AcceleratorCircuitResult:
    """Build one supported accelerator external-circuit topology and assign it.

    ``options.topology`` selects either Boost variant or the isolated
    thin-film-capacitor and simulated-SCR topology.
    """
    opt = options or AcceleratorCircuitOptions()
    topology_display_name(opt.topology)  # validate early

    _report(progress, "正在由 External Windings 创建 Maxwell Circuit Design…")
    circuit = create_external_circuit_design(
        maxwell_app,
        opt.circuit_design,
        delete_existing=opt.delete_existing_circuit,
    )
    circuit.modeler.schematic_units = "mil"

    windings = get_winding_components(circuit)
    if not windings:
        raise MaxwellCircuitToolkitError(
            "Create Circuit succeeded but no Winding components were found. "
            "Confirm Maxwell windings are Type=External."
        )

    stage_indices: list[int] = []
    for fallback_i, winding in enumerate(windings, start=1):
        parsed = winding_stage_index(_component_name(winding))
        stage_indices.append(parsed if parsed is not None else fallback_i)
    _validate_stage_indices(stage_indices, topology=opt.topology)

    _report(progress, f"检测到 {len(windings)} 个 External Winding，正在创建位置变量…")
    created_variables = ensure_position_variables_for_indices(
        maxwell_app,
        stage_indices,
        pos_on_prefix=opt.pos_on_prefix,
        pos_dur_prefix=opt.pos_dur_prefix,
        fallback_on=opt.fallback_pos_on,
        fallback_duration=opt.fallback_pos_dur,
        verify=True,
    )
    first_position_name = f"${opt.pos_on_prefix}_1"
    first_position_value = normalize_aedt_scalar(opt.first_stage_position_on, "length")
    set_global_variables_low_level(
        maxwell_app,
        {first_position_name: first_position_value},
        overwrite_existing=True,
        verify=True,
    )
    created_variables[first_position_name] = first_position_value
    created_variables.update(
        ensure_projectile_initial_z_variable(
            maxwell_app,
            variable_name=opt.projectile_initial_z_variable,
            fallback_value=opt.fallback_projectile_initial_z,
            verify=True,
        )
    )

    _report(progress, f"正在生成拓扑：{topology_display_name(opt.topology)}…")
    created_storage = ensure_storage_capacitor_variables_for_indices(
        maxwell_app,
        stage_indices,
        capacitance_prefix=opt.capacitance_prefix,
        initial_voltage_variable=opt.initial_voltage_variable,
        fallback_capacitance=opt.fallback_capacitance,
        fallback_initial_voltage=opt.fallback_initial_voltage,
        verify=True,
    )
    created_variables.update(created_storage)
    if opt.topology == TOPOLOGY_FILM_CAPACITOR_SCR:
        placement = _place_thin_film_scr_topology
    else:
        existing_vars = set(get_project_variable_names(maxwell_app))
        missing_r = [
            f"${opt.coil_resistance_prefix}_{i}"
            for i in stage_indices
            if f"${opt.coil_resistance_prefix}_{i}" not in existing_vars
        ]
        if missing_r:
            raise MaxwellCircuitToolkitError(
                "Missing coil resistance project variables required by Boost: "
                + ", ".join(missing_r)
            )
        created_variables.update(
            ensure_esr_variables_for_indices(
                maxwell_app,
                stage_indices,
                esr_prefix=opt.esr_prefix,
                fallback_esr=opt.fallback_esr,
                verify=True,
            )
        )
        created_variables.update(
            ensure_last_resistance_variable(
                maxwell_app,
                variable_name=opt.last_resistance_variable,
                fallback_value=opt.fallback_last_resistance,
                verify=True,
            )
        )
        placement = _place_boost_topology
    required_variables = [
        *(f"${opt.pos_on_prefix}_{i}" for i in stage_indices),
        *(f"${opt.pos_dur_prefix}_{i}" for i in stage_indices),
        *(f"${opt.capacitance_prefix}_{i}" for i in stage_indices),
        "$" + str(opt.initial_voltage_variable).lstrip("$"),
        "$" + str(opt.projectile_initial_z_variable).lstrip("$"),
    ]
    if opt.topology != TOPOLOGY_FILM_CAPACITOR_SCR:
        required_variables.extend(
            [
                *(f"${opt.coil_resistance_prefix}_{i}" for i in stage_indices),
                *(f"${opt.esr_prefix}_{i}" for i in stage_indices),
                "$" + str(opt.last_resistance_variable).lstrip("$"),
            ]
        )
    verify_aedt_supported_real_variables(maxwell_app, required_variables)
    (
        switches, pulses, diodes, resistors, pulse_shunts,
        storage_capacitors, storage_esr_resistors, grounds,
        return_resistors, switch_model, diode_model,
    ) = placement(circuit, windings, stage_indices, opt, progress)

    if netlist_path is None:
        project_file = Path(str(getattr(maxwell_app, "project_file", "external_circuit.aedt")))
        netlist_path = project_file.with_name(
            f"{project_file.stem}_{opt.circuit_design}.sph"
        )

    _report(progress, "正在导出 .sph 并回写 Edit External Circuit…")
    assigned_path = export_and_assign_external_circuit(
        maxwell_app,
        circuit,
        circuit_design=opt.circuit_design,
        netlist_path=netlist_path,
    )

    try:
        maxwell_app.save_project()
    except TypeError:
        maxwell_app.save_project(str(getattr(maxwell_app, "project_file", "")))

    scr_topology = opt.topology == TOPOLOGY_FILM_CAPACITOR_SCR
    return AcceleratorCircuitResult(
        circuit=circuit,
        topology=opt.topology,
        windings=list(windings),
        switches=switches,
        pulses=pulses,
        vpwl_sources=[] if scr_topology else pulses[:1],
        vpulse_sources=pulses if scr_topology else pulses[1:],
        diodes=diodes,
        resistors=resistors,
        pulse_shunt_resistors=pulse_shunts,
        storage_capacitors=storage_capacitors,
        storage_esr_resistors=storage_esr_resistors,
        return_resistors=return_resistors,
        last_resistor=return_resistors[-1] if return_resistors else None,
        grounds=grounds,
        switch_model=switch_model,
        diode_model=diode_model,
        created_variables=created_variables,
        netlist_path=assigned_path,
    )
