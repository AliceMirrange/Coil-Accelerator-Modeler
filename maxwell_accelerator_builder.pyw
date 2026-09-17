# -*- coding: utf-8 -*-
"""Double-click Maxwell 2D straight electromagnetic accelerator builder.

Target: PyAEDT + Ansys Maxwell 2D.

Workflow
--------
1. Create a Maxwell 2D TransientZ design (Cylindrical About Z / RZ).
2. Create N annular-coil cross sections in X+ / Z+ as rectangles.
3. Register every model variable as an AEDT project variable (name begins with '$').
4. Create a DC-resistance project variable for each coil.
5. Assign one Maxwell 2D Coil excitation to each coil body and add it to the
   correspondingly numbered Winding Group.

The script intentionally uses the companion maxwell2d_toolkit/core.py library.
For the Coil conductor count only, it calls PyAEDT's public ``assign_coil``
directly so that the AEDT project variable for the user-selected turns prefix can remain parametric.
PyAEDT converts ``conductors_number`` to a string when creating the Coil
boundary, therefore a variable expression is preserved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from configparser import ConfigParser
import math
import os
import re
import sys
import traceback
import threading
import queue
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import Any, Callable

sys.dont_write_bytecode = True

# Make the companion toolkit importable when this .pyw is double-clicked.
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "default.ini"
LOG_DIR = SCRIPT_DIR / "logs"
GUI_FONT_FAMILY = "Microsoft YaHei UI"
APP_VERSION = "1.0.0"
WINDOW_TITLE = f"Coil Accelerator Modeler {APP_VERSION}"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


REQUIRED_OPTIONS = {
    "gui_defaults": (
        "project_filename",
        "last_project_path",
        "maxwell_design",
        "circuit_design",
        "aedt_version",
        "non_graphical",
        "delete_existing_circuit",
        "coil_count",
        "inner_mode",
        "gap_mode",
        "position_mode",
        "common_inner_diameter",
        "common_gap",
        "coil_inner_diameter",
        "coil_outer_diameter",
        "coil_length",
        "coil_turns",
        "coil_fill_factor",
        "coil_material",
        "coil_gap",
        "material_weight_optimization",
        "capacitor_energy_density_j_per_g",
    ),
    "variable_prefixes": (
        "inner_diameter",
        "outer_diameter",
        "length",
        "turns",
        "fill_factor",
        "rho",
        "is_copper",
        "gap",
        "z_start",
        "r_dc",
        "coil_center",
        "pos_on",
        "pos_dur",
        "position_factor",
        "capacitance",
        "esr",
        "initial_voltage",
        "last_resistance",
    ),
    "circuit": ("topology",),
    "initial_values": (
        "pos_on",
        "first_stage_pos_on",
        "pos_dur",
        "position_factor",
        "capacitance",
        "initial_voltage",
        "esr",
        "last_resistance",
    ),
    "voltage_controlled_switch": ("model_name", "Ron", "Roff", "Von", "Voff"),
    "vpulse": ("V1", "V2", "Tr", "Tf", "Period", "shunt_resistance"),
    "vpwl": ("fall_delta", "end_position"),
    "diode": ("model_name",),
}
EMPTY_OPTIONS = {("gui_defaults", "last_project_path"), ("gui_defaults", "aedt_version")}


def configure_gui_fonts(root: tk.Misc) -> None:
    """Use Microsoft YaHei UI while preserving each Tk named font's size."""
    for name in tkfont.names(root):
        if str(name).startswith("Tk"):
            tkfont.nametofont(name, root=root).configure(family=GUI_FONT_FAMILY)
    root.option_add("*Font", "TkDefaultFont")
    root.option_add("*TCombobox*Listbox.font", "TkDefaultFont")


def load_builder_settings(path: str | Path = CONFIG_PATH) -> ConfigParser:
    config_path = Path(path)
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    if not parser.read(config_path, encoding="utf-8"):
        raise FileNotFoundError(config_path)
    missing = [
        f"[{section}] {option}"
        for section, options in REQUIRED_OPTIONS.items()
        for option in options
        if not parser.has_option(section, option)
        or ((section, option) not in EMPTY_OPTIONS and not parser.get(section, option).strip())
    ]
    if missing:
        raise ValueError("Missing or empty INI options: " + ", ".join(missing))

    prefixes = dict(parser.items("variable_prefixes"))
    invalid = [value for value in prefixes.values() if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)]
    if invalid:
        raise ValueError("Invalid variable prefixes in INI: " + ", ".join(invalid))
    folded = [value.casefold() for value in prefixes.values()]
    if len(folded) != len(set(folded)):
        raise ValueError("Variable prefixes in INI must be unique")

    gui = parser["gui_defaults"]
    if gui["inner_mode"] not in {"common", "independent"}:
        raise ValueError("gui_defaults.inner_mode must be common or independent")
    if gui["gap_mode"] not in {"common", "independent"}:
        raise ValueError("gui_defaults.gap_mode must be common or independent")
    if gui["position_mode"] not in {"reluctance", "induction"}:
        raise ValueError("gui_defaults.position_mode must be reluctance or induction")
    if gui["coil_material"] not in {"铜", "铝"}:
        raise ValueError("gui_defaults.coil_material must be 铜 or 铝")
    if int(gui["coil_count"]) < 1:
        raise ValueError("gui_defaults.coil_count must be positive")
    gui.getboolean("non_graphical")
    gui.getboolean("delete_existing_circuit")
    gui.getboolean("material_weight_optimization")
    if float(gui["capacitor_energy_density_j_per_g"]) <= 0:
        raise ValueError("gui_defaults.capacitor_energy_density_j_per_g must be positive")

    topology = parser.get("circuit", "topology").strip()
    if topology not in {"single_boost", "odd_even_boost", "film_capacitor_scr"}:
        raise ValueError(
            "circuit.topology must be single_boost, odd_even_boost, or "
            "film_capacitor_scr"
        )
    return parser


def diode_model_parameters(settings: ConfigParser) -> dict[str, str]:
    return {
        key: value.strip()
        for key, value in settings.items("diode")
        if key != "model_name" and value.strip()
    }


def initial_project_path(
    settings: ConfigParser,
    *,
    base_dir: str | Path = SCRIPT_DIR,
    now: datetime | None = None,
) -> Path:
    gui = settings["gui_defaults"]
    previous = gui["last_project_path"].strip()
    if previous:
        return Path(os.path.expandvars(os.path.expanduser(previous)))
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    filename = gui["project_filename"].format(timestamp=timestamp)
    path = Path(os.path.expandvars(os.path.expanduser(filename)))
    return path if path.is_absolute() else Path(base_dir) / path


def save_last_project_path(project_path: str | Path, path: str | Path = CONFIG_PATH) -> None:
    config_path = Path(path)
    value = str(project_path).strip().replace("\r", "").replace("\n", "")
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    section_start = next(
        (index for index, line in enumerate(lines) if line.strip().casefold() == "[gui_defaults]"),
        None,
    )
    if section_start is None:
        ending = "" if not lines or lines[-1].endswith(("\n", "\r")) else "\n"
        lines.extend([ending, "[gui_defaults]\n", f"last_project_path = {value}\n"])
    else:
        section_end = next(
            (index for index in range(section_start + 1, len(lines)) if lines[index].lstrip().startswith("[")),
            len(lines),
        )
        option_index = next(
            (
                index
                for index in range(section_start + 1, section_end)
                if lines[index].partition("=")[0].strip().casefold() == "last_project_path"
            ),
            None,
        )
        if option_index is None:
            lines.insert(section_end, f"last_project_path = {value}\n")
        else:
            newline = "\r\n" if lines[option_index].endswith("\r\n") else "\n"
            lines[option_index] = f"last_project_path = {value}{newline}"
    config_path.write_text("".join(lines), encoding="utf-8")


def get_log_path(filename: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / filename

try:
    from maxwell2d_toolkit.core import Maxwell2DToolkit, Maxwell2DToolkitError, launch_maxwell2d
    from maxwell2d_toolkit.si import format_si_number, normalize_aedt_scalar
except Exception as _toolkit_import_error:  # handled cleanly by main()
    Maxwell2DToolkit = None  # type: ignore[assignment]
    Maxwell2DToolkitError = RuntimeError  # type: ignore[assignment,misc]
    launch_maxwell2d = None  # type: ignore[assignment]
    TOOLKIT_IMPORT_ERROR = _toolkit_import_error
else:
    TOOLKIT_IMPORT_ERROR = None


# Resistivity at 20 degC, used by the DC resistance formula.
# Values are intentionally explicit project-variable constants per coil so a
# generated project remains self-contained and editable.
COPPER_RHO_OHM_M = 1.724e-8
ALUMINUM_RHO_OHM_M = 2.82e-8
RHO_OHM_M = {"铜": COPPER_RHO_OHM_M, "铝": ALUMINUM_RHO_OHM_M}
AEDT_MATERIAL = {
    "铜": "copper",
    "铝": "aluminum",
}

DEFAULT_WINDING_TYPE = "External"
MAX_COILS = 200
POSITION_MODE_RELUCTANCE = "reluctance"
POSITION_MODE_INDUCTION = "induction"


@dataclass(frozen=True)
class CoilInput:
    index: int
    inner_diameter_mm: float
    outer_diameter_mm: float
    length_mm: float
    turns: int
    fill_factor: float
    material_cn: str
    gap_after_mm: float

    @property
    def rho(self) -> float:
        return RHO_OHM_M[self.material_cn]

    @property
    def material_aedt(self) -> str:
        return AEDT_MATERIAL[self.material_cn]

    @property
    def resistance_ohm(self) -> float:
        """DC resistance from the checked multilayer-solenoid volume formula."""
        di = self.inner_diameter_mm / 1000.0
        do = self.outer_diameter_mm / 1000.0
        length = self.length_mm / 1000.0
        return (
            self.rho
            * math.pi
            * self.turns**2
            * (do + di)
            / (self.fill_factor * length * (do - di))
        )


@dataclass(frozen=True)
class VariablePrefixes:
    """User-editable AEDT project-variable prefixes.

    ``indexed(key, n)`` always returns ``$<prefix>_<n>``.  For the two values
    that can be shared across all coils (inner diameter and gap), ``shared``
    returns ``$<prefix>`` when the GUI is in common-value mode.
    """

    inner_diameter: str
    outer_diameter: str
    length: str
    turns: str
    fill_factor: str
    rho: str
    is_copper: str
    gap: str
    z_start: str
    r_dc: str
    coil_center: str
    pos_on: str
    pos_dur: str
    position_factor: str
    capacitance: str
    esr: str
    initial_voltage: str
    last_resistance: str

    def indexed(self, key: str, index: int) -> str:
        return f"${getattr(self, key)}_{index}"

    def shared(self, key: str) -> str:
        return f"${getattr(self, key)}"


def variable_prefixes_from_settings(settings: ConfigParser | None = None) -> VariablePrefixes:
    values = (settings or load_builder_settings())["variable_prefixes"]
    return VariablePrefixes(**{key: values[key] for key in VariablePrefixes.__dataclass_fields__})


@dataclass(frozen=True)
class BuildConfig:
    project_path: Path
    design_name: str
    aedt_version: str | None
    non_graphical: bool
    common_inner_diameter: bool
    common_gap: bool
    prefixes: VariablePrefixes
    coils: tuple[CoilInput, ...]
    position_mode: str = POSITION_MODE_RELUCTANCE
    first_stage_position_on: str = "-1000mm"
    default_position_factor: str = "0.5"
    default_position_duration: str = "10mm"
    default_capacitance: str = "220uF"
    default_initial_voltage: str = "390V"
    material_weight_optimization: bool = False
    capacitor_energy_density_j_per_g: float = 0.85


@dataclass
class RowVars:
    index: int
    inner: tk.StringVar
    outer: tk.StringVar
    length: tk.StringVar
    turns: tk.StringVar
    fill: tk.StringVar
    material: tk.StringVar
    gap_after: tk.StringVar
    inner_entry: ttk.Entry | None = None
    gap_entry: ttk.Entry | None = None


def _fmt_number(value: float) -> str:
    """Compact deterministic decimal formatting for AEDT expressions."""
    return format_si_number(value)


def _millimeters(value: float) -> str:
    return f"{_fmt_number(value)}mm"


def _rho_expr(value: float) -> str:
    """Return resistivity using AEDT's official Ohmm unit."""
    return f"{_fmt_number(value)}Ohmm"


def build_project_variable_map(config: BuildConfig) -> dict[str, str]:
    """Build all AEDT project-variable expressions in dependency order."""
    if config.position_mode not in {POSITION_MODE_RELUCTANCE, POSITION_MODE_INDUCTION}:
        raise ValueError(f"Unsupported POSon mode: {config.position_mode!r}")
    variables: dict[str, str] = {}
    p = config.prefixes

    inner_shared = p.shared("inner_diameter")
    gap_shared = p.shared("gap")

    # Shared values still use the user-selected prefix, but without _n.
    if config.common_inner_diameter:
        variables[inner_shared] = _millimeters(config.coils[0].inner_diameter_mm)
    if config.common_gap and len(config.coils) > 1:
        variables[gap_shared] = _millimeters(config.coils[0].gap_after_mm)

    for coil in config.coils:
        i = coil.index
        inner_i = p.indexed("inner_diameter", i)
        outer_i = p.indexed("outer_diameter", i)
        length_i = p.indexed("length", i)
        turns_i = p.indexed("turns", i)
        fill_i = p.indexed("fill_factor", i)
        rho_i = p.indexed("rho", i)
        is_copper_i = p.indexed("is_copper", i)
        gap_i = p.indexed("gap", i)
        z_i = p.indexed("z_start", i)
        r_i = p.indexed("r_dc", i)
        center_i = p.indexed("coil_center", i)
        pos_on_i = p.indexed("pos_on", i)

        if not config.common_inner_diameter:
            variables[inner_i] = _millimeters(coil.inner_diameter_mm)

        variables[outer_i] = _millimeters(coil.outer_diameter_mm)
        variables[length_i] = _millimeters(coil.length_mm)
        variables[turns_i] = str(coil.turns)
        variables[fill_i] = _fmt_number(coil.fill_factor)
        if config.material_weight_optimization:
            variables[is_copper_i] = "1" if coil.material_cn == "铜" else "0"
            variables[rho_i] = (
                f"if({is_copper_i}==1,{_rho_expr(COPPER_RHO_OHM_M)},"
                f"{_rho_expr(ALUMINUM_RHO_OHM_M)})"
            )
        else:
            variables[rho_i] = _rho_expr(coil.rho)

        if not config.common_gap and i < len(config.coils):
            variables[gap_i] = _millimeters(coil.gap_after_mm)

        if i == 1:
            variables[z_i] = "0mm"
        else:
            previous = i - 1
            previous_z = p.indexed("z_start", previous)
            previous_length = p.indexed("length", previous)
            gap_ref = gap_shared if config.common_gap else p.indexed("gap", previous)
            variables[z_i] = f"{previous_z}+{previous_length}+{gap_ref}"

        variables[center_i] = f"{z_i}+{length_i}/2"
        if i == 1:
            variables[pos_on_i] = normalize_aedt_scalar(
                config.first_stage_position_on, "length"
            )
        elif config.position_mode == POSITION_MODE_RELUCTANCE:
            factor_i = p.indexed("position_factor", i)
            previous_center = p.indexed("coil_center", i - 1)
            variables[factor_i] = config.default_position_factor
            variables[pos_on_i] = f"{previous_center}+{factor_i}*({center_i}-{previous_center})"
        else:
            factor_i = p.indexed("position_factor", i)
            variables[factor_i] = config.default_position_factor
            variables[pos_on_i] = f"{center_i}-{length_i}/2+{factor_i}*{length_i}"

        di_ref = inner_shared if config.common_inner_diameter else inner_i
        variables[r_i] = (
            f"3.141592653589793*{rho_i}*{turns_i}^2*"
            f"({outer_i}+{di_ref})/"
            f"({fill_i}*{length_i}*({outer_i}-{di_ref}))"
        )

    return variables


def _release_aedt_control(app: Any, *, non_graphical: bool) -> None:
    """Release PyAEDT control after a build.

    Graphical mode keeps the AEDT desktop and project open so the user can
    continue working in AEDT and close it normally from its GUI.  Non-graphical
    mode closes the hidden AEDT session after the project has been saved.

    PyAEDT releases have used both ``close_desktop`` and ``close_on_exit`` as
    the second keyword name on related release APIs.  Try the Maxwell app API
    first and keep a compatibility fallback because this toolkit deliberately
    does not enforce a particular PyAEDT package version.
    """
    if app is None:
        return

    close_projects = bool(non_graphical)
    close_desktop = bool(non_graphical)
    release = getattr(app, "release_desktop", None)
    if not callable(release):
        raise RuntimeError("The active PyAEDT Maxwell2d object has no release_desktop() method.")

    try:
        ok = release(close_projects=close_projects, close_desktop=close_desktop)
    except TypeError:
        # Compatibility with Desktop.release_desktop variants that expose the
        # second option as close_on_exit rather than close_desktop.
        ok = release(close_projects=close_projects, close_on_exit=close_desktop)

    if ok is False:
        mode = "non-graphical" if non_graphical else "graphical"
        raise RuntimeError(f"PyAEDT reported failure while releasing the {mode} AEDT session.")


def build_maxwell_model(
    config: BuildConfig,
    progress: Callable[[str], None] | None = None,
) -> list[float]:
    """Create the Maxwell 2D model, save it, release AEDT, and return R values.

    The function owns the AEDT session it creates.  It therefore guarantees a
    release attempt in ``finally`` so the GUI process does not retain control
    of AEDT after modeling completes or fails.
    """
    def report(message: str) -> None:
        if progress is not None:
            try:
                progress(message)
            except Exception:
                pass

    if launch_maxwell2d is None or Maxwell2DToolkit is None:
        raise RuntimeError(f"Cannot import companion toolkit: {TOOLKIT_IMPORT_ERROR}")

    app = None
    primary_error: BaseException | None = None
    try:
        config.project_path.parent.mkdir(parents=True, exist_ok=True)
        # TransientZ is the PyAEDT Maxwell 2D solution type for cylindrical
        # symmetry about the Z axis (RZ / "about Z").
        report("正在启动 Ansys Electronics Desktop…")
        app = launch_maxwell2d(
            project=config.project_path,
            design=config.design_name,
            solution_type="TransientZ",
            version=config.aedt_version,
            non_graphical=config.non_graphical,
            new_desktop=True,
            close_on_exit=False,
        )
        report("AEDT 已连接，正在初始化 Maxwell 2D Design…")
        toolkit = Maxwell2DToolkit(app)
        toolkit.set_model_units("mm")

        # Defensive check: TransientZ should create Cylindrical About Z geometry.
        geometry_mode = str(getattr(app, "geometry_mode", ""))
        if geometry_mode and geometry_mode.lower() not in {"about z", "rz"}:
            raise RuntimeError(
                "Maxwell 2D design was not created in Cylindrical About Z mode. "
                f"Reported geometry_mode={geometry_mode!r}."
            )

        variables = build_project_variable_map(config)
        report(f"正在一次性注册 {len(variables)} 个项目全局变量…")
        toolkit.set_global_variables(variables, verify=True)
        toolkit.verify_supported_real_variables(variables)

        resistances = [coil.resistance_ohm for coil in config.coils]

        p = config.prefixes
        for coil in config.coils:
            i = coil.index
            report(f"正在建立第 {i}/{len(config.coils)} 级线圈…")
            di_ref = p.shared("inner_diameter") if config.common_inner_diameter else p.indexed("inner_diameter", i)
            outer_i = p.indexed("outer_diameter", i)
            length_i = p.indexed("length", i)
            turns_i = p.indexed("turns", i)
            z_i = p.indexed("z_start", i)
            r_i = p.indexed("r_dc", i)

            # In Maxwell 2D RZ geometry, create_rectangle() sizes are [Z, R].
            body = toolkit.rectangle(
                origin=[
                    f"{di_ref}/2",
                    "0mm",
                    z_i,
                ],
                sizes=[
                    length_i,
                    f"({outer_i}-{di_ref})/2",
                ],
                name=f"CoilBody_{i}",
                material=coil.material_aedt,
                is_covered=True,
            )

            coil_boundary = app.assign_coil(
                assignment=[body.name],
                conductors_number=turns_i,
                polarity="Positive",
                name=f"Coil_{i}",
            )
            if not coil_boundary:
                raise RuntimeError(f"Failed to assign Coil_{i} to {body.name}.")

            winding = toolkit.create_empty_winding(
                name=f"Winding_{i}",
                winding_type=DEFAULT_WINDING_TYPE,
                is_solid=False,
                current=0,
                resistance=r_i,
                inductance=0,
                voltage=0,
                parallel_branches=1,
                phase=0,
            )
            toolkit.add_coils_to_winding(winding, coil_boundary)

        report("正在保存 AEDT 工程…")
        toolkit.save(config.project_path)
        report("工程已保存，正在释放 PyAEDT/AEDT 会话…")
        return resistances
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if app is not None:
            try:
                report("正在释放 AEDT 会话…")
                _release_aedt_control(app, non_graphical=config.non_graphical)
            except Exception as release_exc:
                # Never hide the actual model-building error.  On an otherwise
                # successful build, however, failure to release is important
                # enough to surface because it is exactly what can leave AEDT
                # under script control.
                if primary_error is None:
                    raise
                try:
                    logger = getattr(app, "logger", None)
                    if logger is not None:
                        logger.error("Failed to release AEDT after build error: %s", release_exc)
                except Exception:
                    pass


def add_screening_optimization(
    config: BuildConfig,
    progress: Callable[[str], None] | None = None,
) -> dict[str, tuple[float, float]]:
    """Add or replace the accelerator optimization setup in a saved project."""

    def report(message: str) -> None:
        if progress is not None:
            progress(message)

    if launch_maxwell2d is None or Maxwell2DToolkit is None:
        raise RuntimeError(f"Cannot import companion toolkit: {TOOLKIT_IMPORT_ERROR}")
    if not config.project_path.is_file():
        raise FileNotFoundError(config.project_path)

    app = None
    primary_error: BaseException | None = None
    try:
        report("Connecting to the Maxwell 2D project for Optimetrics...")
        app = launch_maxwell2d(
            project=config.project_path,
            design=config.design_name,
            solution_type="TransientZ",
            version=config.aedt_version,
            non_graphical=config.non_graphical,
            new_desktop=bool(config.non_graphical),
            close_on_exit=False,
        )
        toolkit = Maxwell2DToolkit(app)
        p = config.prefixes
        variable_options = {
            "stage_count": len(config.coils),
            "z_start_prefix": p.z_start,
            "coil_length_prefix": p.length,
            "outer_diameter_prefix": p.outer_diameter,
            "turns_prefix": p.turns,
            "gap_prefix": p.gap,
            "common_gap": config.common_gap,
            "center_prefix": p.coil_center,
            "position_on_prefix": p.pos_on,
            "position_factor_prefix": p.position_factor,
            "position_mode": config.position_mode,
            "first_stage_position_on": config.first_stage_position_on,
            "position_duration_prefix": p.pos_dur,
            "capacitance_prefix": p.capacitance,
            "inner_diameter_prefix": p.inner_diameter,
            "common_inner_diameter": config.common_inner_diameter,
            "fill_factor_prefix": p.fill_factor,
            "resistivity_prefix": p.rho,
            "material_selector_prefix": p.is_copper,
            "initial_voltage_variable": p.initial_voltage,
            "default_position_factor": config.default_position_factor,
            "default_position_duration": config.default_position_duration,
            "default_capacitance": config.default_capacitance,
            "default_initial_voltage": config.default_initial_voltage,
            "material_weight_optimization": config.material_weight_optimization,
            "capacitor_energy_density_j_per_g": config.capacitor_energy_density_j_per_g,
        }
        report("Preparing and saving project variables required by Optimetrics...")
        toolkit.prepare_accelerator_optimization_variables(**variable_options)
        toolkit.save(config.project_path)
        report("Evaluating coil centers and writing numeric optimization bounds...")
        setup, bounds = toolkit.create_accelerator_screening_optimization(
            **variable_options,
            prepare_variables=False,
        )
        report(f"Saving Optimetrics setup {setup.name}...")
        toolkit.save(config.project_path)
        return bounds
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if app is not None:
            try:
                report("Releasing the AEDT session...")
                _release_aedt_control(app, non_graphical=config.non_graphical)
            except Exception:
                if primary_error is None:
                    raise


class AcceleratorBuilderGUI:
    def __init__(
        self,
        root: tk.Misc,
        *,
        settings: ConfigParser | None = None,
        manage_window: bool = True,
    ) -> None:
        self.root = root
        self.settings = settings or load_builder_settings()
        defaults = self.settings["gui_defaults"]
        if manage_window:
            self.root.title(WINDOW_TITLE)
            self.root.minsize(1120, 650)

        self.project_path = tk.StringVar(value=str(initial_project_path(self.settings)))
        self.design_name = tk.StringVar(value=defaults["maxwell_design"])
        self.aedt_version = tk.StringVar(value=defaults["aedt_version"])
        self.non_graphical = tk.BooleanVar(value=defaults.getboolean("non_graphical"))
        self.coil_count = tk.StringVar(value=defaults["coil_count"])

        self.inner_mode = tk.StringVar(value=defaults["inner_mode"])
        self.gap_mode = tk.StringVar(value=defaults["gap_mode"])
        self.position_mode = tk.StringVar(value=defaults["position_mode"])
        self.material_weight_optimization = tk.BooleanVar(
            value=defaults.getboolean("material_weight_optimization")
        )
        self.capacitor_energy_density = tk.StringVar(
            value=defaults["capacitor_energy_density_j_per_g"]
        )
        self.common_inner = tk.StringVar(value=defaults["common_inner_diameter"])
        self.common_gap = tk.StringVar(value=defaults["common_gap"])
        self.row_defaults = (
            defaults["coil_inner_diameter"],
            defaults["coil_outer_diameter"],
            defaults["coil_length"],
            defaults["coil_turns"],
            defaults["coil_fill_factor"],
            defaults["coil_material"],
            defaults["coil_gap"],
        )

        self.prefixes = variable_prefixes_from_settings(self.settings)

        self.rows: list[RowVars] = []
        self.status = tk.StringVar(value="就绪。先设置线圈数量，然后点“刷新参数表”。")
        self._build_thread: threading.Thread | None = None
        self._build_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

        self._build_ui()
        self._rebuild_rows()

    def _build_ui(self) -> None:
        self.model_tab = ttk.Frame(self.root)
        self.model_tab.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        top = ttk.Frame(self.model_tab, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="工程文件").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(top, textvariable=self.project_path, width=75).grid(
            row=0, column=1, columnspan=5, sticky="ew", padx=4, pady=4
        )
        ttk.Button(top, text="浏览…", command=self._choose_project).grid(row=0, column=6, padx=4, pady=4)

        ttk.Label(top, text="Design 名称").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(top, textvariable=self.design_name, width=24).grid(row=1, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(top, text="AEDT 版本").grid(row=1, column=2, sticky="e", padx=4, pady=4)
        ttk.Entry(top, textvariable=self.aedt_version, width=12).grid(row=1, column=3, sticky="w", padx=4, pady=4)
        ttk.Label(top, text="线圈数量 n").grid(row=1, column=4, sticky="e", padx=4, pady=4)
        ttk.Entry(top, textvariable=self.coil_count, width=8).grid(row=1, column=5, sticky="w", padx=4, pady=4)
        ttk.Button(
            top,
            text="刷新参数表",
            command=self._refresh_and_validate,
        ).grid(row=1, column=6, padx=4, pady=4)

        ttk.Checkbutton(
            top,
            text="非图形化模式",
            variable=self.non_graphical,
        ).grid(row=2, column=0, columnspan=4, sticky="w", padx=4, pady=(6, 2))
        ttk.Button(top, text="打开 default.ini", command=lambda: os.startfile(CONFIG_PATH)).grid(
            row=2, column=6, padx=4, pady=(6, 2)
        )
        top.columnconfigure(1, weight=1)

        modes = ttk.LabelFrame(self.model_tab, text="建模与优化参数", padding=8)
        modes.pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(modes, text="内径：").grid(row=0, column=0, sticky="w", padx=4)
        ttk.Radiobutton(
            modes, text="统一", variable=self.inner_mode, value="common", command=self._update_row_states
        ).grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(
            modes, text="各线圈独立", variable=self.inner_mode, value="independent", command=self._update_row_states
        ).grid(row=0, column=2, sticky="w")
        ttk.Label(modes, text="统一内径 Di (mm)").grid(row=0, column=3, sticky="e", padx=(20, 4))
        self.common_inner_entry = ttk.Entry(modes, textvariable=self.common_inner, width=12)
        self.common_inner_entry.grid(row=0, column=4, sticky="w")

        ttk.Label(modes, text="线圈间距：").grid(row=0, column=5, sticky="w", padx=(30, 4))
        ttk.Radiobutton(
            modes, text="统一", variable=self.gap_mode, value="common", command=self._update_row_states
        ).grid(row=0, column=6, sticky="w")
        ttk.Radiobutton(
            modes, text="各间距独立", variable=self.gap_mode, value="independent", command=self._update_row_states
        ).grid(row=0, column=7, sticky="w")
        ttk.Label(modes, text="统一间距 (mm)").grid(row=0, column=8, sticky="e", padx=(20, 4))
        self.common_gap_entry = ttk.Entry(modes, textvariable=self.common_gap, width=12)
        self.common_gap_entry.grid(row=0, column=9, sticky="w")

        ttk.Label(modes, text="POSon 模式：").grid(row=1, column=0, sticky="w", padx=4, pady=(8, 0))
        ttk.Radiobutton(
            modes, text="磁阻", variable=self.position_mode, value=POSITION_MODE_RELUCTANCE
        ).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Radiobutton(
            modes, text="感应", variable=self.position_mode, value=POSITION_MODE_INDUCTION
        ).grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            modes,
            text="材料及重量优化",
            variable=self.material_weight_optimization,
            command=self._update_optimization_state,
        ).grid(row=1, column=5, columnspan=2, sticky="w", padx=(30, 4), pady=(8, 0))
        ttk.Label(modes, text="电容储能密度 (J/g)").grid(
            row=1, column=7, sticky="e", padx=(20, 4), pady=(8, 0)
        )
        self.capacitor_energy_density_entry = ttk.Entry(
            modes, textvariable=self.capacitor_energy_density, width=12
        )
        self.capacitor_energy_density_entry.grid(
            row=1, column=8, columnspan=2, sticky="w", pady=(8, 0)
        )
        self._update_optimization_state()

        table_frame = ttk.Frame(self.model_tab, padding=(10, 0, 10, 10))
        table_frame.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(table_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.canvas.yview)
        self.table = ttk.Frame(self.canvas)
        self.table.bind(
            "<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas_window = self.canvas.create_window((0, 0), window=self.table, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.bind(
            "<Configure>", lambda e: self.canvas.itemconfigure(self.canvas_window, width=e.width)
        )
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status).pack(side="left", fill="x", expand=True)
        self.progress_bar = ttk.Progressbar(bottom, mode="indeterminate", length=170)
        self.progress_bar.pack(side="right", padx=(6, 2))
        self.optimetrics_button = ttk.Button(
            bottom,
            text="添加 Optimization Setup",
            command=self._add_optimetrics,
        )
        self.optimetrics_button.pack(side="right", padx=5)
        self.build_button = ttk.Button(bottom, text="建立 Maxwell2D 模型", command=self._build_model)
        self.build_button.pack(side="right", padx=5)

    def _choose_project(self) -> None:
        current = Path(self.project_path.get()).expanduser()
        path = filedialog.asksaveasfilename(
            title="保存 Maxwell AEDT 工程",
            defaultextension=".aedt",
            filetypes=[("Ansys Electronics Desktop project", "*.aedt"), ("All files", "*.*")],
            initialfile=current.name,
            initialdir=str(current.parent if current.parent.exists() else SCRIPT_DIR),
            confirmoverwrite=False,
        )
        if path:
            self.project_path.set(path)

    def _parse_count(self) -> int:
        try:
            n = int(self.coil_count.get().strip())
        except ValueError as exc:
            raise ValueError("线圈数量 n 必须为整数。") from exc
        if not 1 <= n <= MAX_COILS:
            raise ValueError(f"线圈数量必须在 1~{MAX_COILS} 之间。")
        return n

    def _rebuild_rows(self) -> bool:
        try:
            n = self._parse_count()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.root)
            return False

        old_values = []
        for row in self.rows:
            old_values.append(
                (
                    row.inner.get(), row.outer.get(), row.length.get(), row.turns.get(),
                    row.fill.get(), row.material.get(), row.gap_after.get()
                )
            )

        for child in self.table.winfo_children():
            child.destroy()
        self.rows.clear()

        headers = [
            "序号", "内径 Di (mm)", "外径 Do (mm)", "轴向长度 l (mm)",
            "匝数 N", "导体填充率 k", "导体", "与下一级间距 (mm)", "Rdc 预览 (Ω)"
        ]
        for col, text in enumerate(headers):
            ttk.Label(self.table, text=text, anchor="center").grid(
                row=0, column=col, sticky="ew", padx=3, pady=4
            )
            self.table.columnconfigure(col, weight=1 if col in (1, 2, 3, 7, 8) else 0)

        for idx in range(1, n + 1):
            if idx <= len(old_values):
                vals = old_values[idx - 1]
            else:
                vals = self.row_defaults

            row = RowVars(
                index=idx,
                inner=tk.StringVar(value=vals[0]),
                outer=tk.StringVar(value=vals[1]),
                length=tk.StringVar(value=vals[2]),
                turns=tk.StringVar(value=vals[3]),
                fill=tk.StringVar(value=vals[4]),
                material=tk.StringVar(value=vals[5]),
                gap_after=tk.StringVar(value=vals[6]),
            )
            self.rows.append(row)

            ttk.Label(self.table, text=str(idx), anchor="center").grid(row=idx, column=0, sticky="ew", padx=3, pady=2)
            row.inner_entry = ttk.Entry(self.table, textvariable=row.inner, width=13)
            row.inner_entry.grid(row=idx, column=1, sticky="ew", padx=3, pady=2)
            ttk.Entry(self.table, textvariable=row.outer, width=13).grid(row=idx, column=2, sticky="ew", padx=3, pady=2)
            ttk.Entry(self.table, textvariable=row.length, width=13).grid(row=idx, column=3, sticky="ew", padx=3, pady=2)
            ttk.Entry(self.table, textvariable=row.turns, width=10).grid(row=idx, column=4, sticky="ew", padx=3, pady=2)
            ttk.Entry(self.table, textvariable=row.fill, width=12).grid(row=idx, column=5, sticky="ew", padx=3, pady=2)
            material_cell = ttk.Frame(self.table)
            material_cell.grid(row=idx, column=6, sticky="ew", padx=3, pady=2)
            for column, material in enumerate(RHO_OHM_M):
                ttk.Radiobutton(
                    material_cell,
                    text=material,
                    variable=row.material,
                    value=material,
                ).grid(row=0, column=column, sticky="w", padx=(0, 4))
            row.gap_entry = ttk.Entry(self.table, textvariable=row.gap_after, width=14)
            row.gap_entry.grid(row=idx, column=7, sticky="ew", padx=3, pady=2)
            ttk.Label(self.table, text="—", anchor="e", name=f"rpreview_{idx}").grid(
                row=idx, column=8, sticky="ew", padx=3, pady=2
            )

        self._update_row_states()
        self.status.set(f"已生成 {n} 级线圈参数表。")
        return True

    def _update_row_states(self) -> None:
        common_inner = self.inner_mode.get() == "common"
        common_gap = self.gap_mode.get() == "common"
        self.common_inner_entry.configure(state="normal" if common_inner else "disabled")
        self.common_gap_entry.configure(state="normal" if common_gap else "disabled")

        for row in self.rows:
            if row.inner_entry is not None:
                row.inner_entry.configure(state="disabled" if common_inner else "normal")
            if row.gap_entry is not None:
                if row.index == len(self.rows):
                    row.gap_entry.configure(state="disabled")
                else:
                    row.gap_entry.configure(state="disabled" if common_gap else "normal")

    def _update_optimization_state(self) -> None:
        state = "normal" if self.material_weight_optimization.get() else "disabled"
        self.capacitor_energy_density_entry.configure(state=state)

    @staticmethod
    def _float(text: str, label: str) -> float:
        try:
            return float(text.strip())
        except ValueError as exc:
            raise ValueError(f"{label} 必须是数字。") from exc

    @staticmethod
    def _positive_int(text: str, label: str) -> int:
        try:
            value = int(text.strip())
        except ValueError as exc:
            raise ValueError(f"{label} 必须是正整数。") from exc
        if value <= 0:
            raise ValueError(f"{label} 必须 > 0。")
        return value

    @staticmethod
    def _normalize_prefix(text: str, label: str) -> str:
        value = text.strip()
        if value.startswith("$"):
            value = value[1:]
        if not value:
            raise ValueError(f"{label}前缀不能为空。")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(
                f"{label}前缀 {value!r} 非法。只允许英文字母、数字、下划线，且不能以数字开头。"
            )
        return value

    def _collect_prefixes(self) -> VariablePrefixes:
        labels = {
            "inner_diameter": "内径",
            "outer_diameter": "外径",
            "length": "长度",
            "turns": "匝数",
            "fill_factor": "填充率",
            "rho": "电阻率",
            "is_copper": "material selector is_copper",
            "gap": "间距",
            "z_start": "Z 起点",
            "r_dc": "直流电阻",
        }
        labels.update(
            {
                "coil_center": "coil center",
                "pos_on": "POSon",
                "pos_dur": "POSdur",
                "position_factor": "position factor u",
                "capacitance": "capacitance",
                "esr": "ESR",
                "initial_voltage": "initial voltage",
                "last_resistance": "last-stage return resistance",
            }
        )
        values = {
            key: self._normalize_prefix(getattr(self.prefixes, key), labels[key])
            for key in labels
        }
        folded: dict[str, str] = {}
        for key, value in values.items():
            low = value.casefold()
            if low in folded:
                other = folded[low]
                raise ValueError(
                    f"变量前缀重复：{labels[other]} 与 {labels[key]} 都使用 {value!r}。"
                )
            folded[low] = key
        return VariablePrefixes(**values)

    def _collect_config(self) -> BuildConfig:
        n = self._parse_count()
        if len(self.rows) != n:
            raise ValueError("线圈数量与当前参数表不一致，请先点“刷新参数表”。")

        common_inner = self.inner_mode.get() == "common"
        common_gap = self.gap_mode.get() == "common"

        common_di = None
        if common_inner:
            common_di = self._float(self.common_inner.get(), "统一内径")
            if common_di <= 0:
                raise ValueError("统一内径必须 > 0。")

        common_gap_value = None
        if common_gap and n > 1:
            common_gap_value = self._float(self.common_gap.get(), "统一间距")
            if common_gap_value < 0:
                raise ValueError("统一间距必须 >= 0。")

        coils: list[CoilInput] = []
        for row in self.rows:
            i = row.index
            di = common_di if common_di is not None else self._float(row.inner.get(), f"第{i}级内径")
            do = self._float(row.outer.get(), f"第{i}级外径")
            length = self._float(row.length.get(), f"第{i}级长度")
            turns = self._positive_int(row.turns.get(), f"第{i}级匝数")
            fill = self._float(row.fill.get(), f"第{i}级导体填充率")
            material = row.material.get().strip()

            if di <= 0:
                raise ValueError(f"第{i}级内径必须 > 0。")
            if do <= di:
                raise ValueError(f"第{i}级外径必须大于内径。")
            if length <= 0:
                raise ValueError(f"第{i}级长度必须 > 0。")
            if not 0 < fill <= 1:
                raise ValueError(f"第{i}级导体填充率必须满足 0 < k <= 1。")
            if material not in RHO_OHM_M:
                raise ValueError(f"第{i}级导体只能选择铜或铝。")

            if i == n:
                gap = 0.0
            elif common_gap_value is not None:
                gap = common_gap_value
            else:
                gap = self._float(row.gap_after.get(), f"第{i}级与第{i+1}级间距")
                if gap < 0:
                    raise ValueError(f"第{i}级与第{i+1}级间距必须 >= 0。")

            coils.append(
                CoilInput(
                    index=i,
                    inner_diameter_mm=di,
                    outer_diameter_mm=do,
                    length_mm=length,
                    turns=turns,
                    fill_factor=fill,
                    material_cn=material,
                    gap_after_mm=gap,
                )
            )

        project_text = self.project_path.get().strip()
        if not project_text:
            raise ValueError("请选择 AEDT 工程保存路径。")
        project_path = Path(os.path.expandvars(os.path.expanduser(project_text))).resolve()
        if project_path.suffix.lower() != ".aedt":
            project_path = project_path.with_suffix(".aedt")
        project_path.parent.mkdir(parents=True, exist_ok=True)

        design_name = self.design_name.get().strip()
        if not design_name:
            raise ValueError("Design 名称不能为空。")

        version = self.aedt_version.get().strip() or None
        prefixes = self._collect_prefixes()
        position_mode = self.position_mode.get()
        if position_mode not in {POSITION_MODE_RELUCTANCE, POSITION_MODE_INDUCTION}:
            raise ValueError(f"未知 POSon 模式：{position_mode}")
        energy_density = self._float(self.capacitor_energy_density.get(), "电容储能密度")
        if energy_density <= 0:
            raise ValueError("电容储能密度必须 > 0。")
        return BuildConfig(
            project_path=project_path,
            design_name=design_name,
            aedt_version=version,
            non_graphical=bool(self.non_graphical.get()),
            common_inner_diameter=common_inner,
            common_gap=common_gap,
            prefixes=prefixes,
            coils=tuple(coils),
            position_mode=position_mode,
            first_stage_position_on=normalize_aedt_scalar(
                self.settings.get("initial_values", "first_stage_pos_on"), "length"
            ),
            default_position_factor=self.settings.get("initial_values", "position_factor").strip(),
            default_position_duration=normalize_aedt_scalar(
                self.settings.get("initial_values", "pos_dur"), "length"
            ),
            default_capacitance=normalize_aedt_scalar(
                self.settings.get("initial_values", "capacitance"), "capacitance"
            ),
            default_initial_voltage=normalize_aedt_scalar(
                self.settings.get("initial_values", "initial_voltage"), "voltage"
            ),
            material_weight_optimization=bool(self.material_weight_optimization.get()),
            capacitor_energy_density_j_per_g=energy_density,
        )

    def _refresh_resistance_preview(self, config: BuildConfig) -> None:
        for coil in config.coils:
            widget = self.table.nametowidget(f"rpreview_{coil.index}")
            widget.configure(text=f"{coil.resistance_ohm:.6g}")

    def _refresh_and_validate(self) -> None:
        if not self._rebuild_rows():
            return
        try:
            config = self._collect_config()
            self._refresh_resistance_preview(config)
            variable_map = build_project_variable_map(config)
        except Exception as exc:
            messagebox.showerror("校验失败", str(exc), parent=self.root)
            self.status.set("参数校验失败。")
            return

        self.status.set(f"参数校验通过；将创建 {len(variable_map)} 个 AEDT 项目全局变量。")
        messagebox.showinfo(
            "校验通过",
            "参数有效。\n\n"
            "Rdc 公式：\n"
            "R = ρ·π·N²·(Do+Di) / [k·l·(Do-Di)]\n\n"
            "所有项目变量均使用无单位的 SI 数值；对象属性在引用时附加物理单位。表格右侧已显示每级数值预览。",
            parent=self.root,
        )

    def _build_model(self) -> None:
        """Validate input and start AEDT work on a worker thread.

        AEDT startup/modeling is intentionally kept off the Tk main thread so
        Windows never marks the GUI as "Not responding" while gRPC/Desktop is
        starting or while Maxwell objects are being created.
        """
        if self._build_thread is not None and self._build_thread.is_alive():
            messagebox.showinfo("正在建模", "当前建模任务仍在运行，请等待完成。", parent=self.root)
            return

        try:
            config = self._collect_config()
            self._refresh_resistance_preview(config)
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.root)
            return

        if TOOLKIT_IMPORT_ERROR is not None:
            messagebox.showerror(
                "函数库加载失败",
                "无法加载 maxwell2d_toolkit/core.py。请把本 .pyw 放在函数库根目录下。\n\n"
                f"详细错误：{TOOLKIT_IMPORT_ERROR}",
                parent=self.root,
            )
            return

        # Discard any stale messages from a previous run.
        try:
            while True:
                self._build_queue.get_nowait()
        except queue.Empty:
            pass

        self.build_button.configure(state="disabled", text="正在建模…")
        self.optimetrics_button.configure(state="disabled")
        self.progress_bar.start(12)
        mode_text = "非图形化后台模式" if config.non_graphical else "图形模式"
        self.status.set(f"正在以{mode_text}启动 AEDT…")

        self._build_thread = threading.Thread(
            target=self._build_worker,
            args=(config,),
            name="Maxwell2DBuilderWorker",
            daemon=True,
        )
        self._build_thread.start()
        self.root.after(100, self._poll_build_queue)

    def _add_optimetrics(self) -> None:
        if self._build_thread is not None and self._build_thread.is_alive():
            messagebox.showinfo("任务运行中", "当前 AEDT 任务仍在运行，请等待完成。", parent=self.root)
            return
        try:
            config = self._collect_config()
            if len(config.coils) < 2:
                raise ValueError("The optimization setup requires at least two coil stages.")
            if not config.project_path.is_file():
                raise FileNotFoundError(config.project_path)
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.root)
            return
        if TOOLKIT_IMPORT_ERROR is not None:
            messagebox.showerror("函数库加载失败", str(TOOLKIT_IMPORT_ERROR), parent=self.root)
            return

        try:
            while True:
                self._build_queue.get_nowait()
        except queue.Empty:
            pass
        self.build_button.configure(state="disabled")
        self.optimetrics_button.configure(state="disabled", text="正在添加 Optimization Setup…")
        self.progress_bar.start(12)
        self.status.set("正在连接 AEDT 并创建 Optimization setup…")
        self._build_thread = threading.Thread(
            target=self._optimetrics_worker,
            args=(config,),
            name="Maxwell2DOptimetricsWorker",
            daemon=True,
        )
        self._build_thread.start()
        self.root.after(100, self._poll_build_queue)

    def _optimetrics_worker(self, config: BuildConfig) -> None:
        try:
            bounds = add_screening_optimization(
                config,
                progress=lambda message: self._build_queue.put(("progress", message)),
            )
        except BaseException:
            self._build_queue.put(("optimetrics_error", (config, traceback.format_exc())))
        else:
            self._build_queue.put(("optimetrics_success", (config, bounds)))
        finally:
            self._build_queue.put(("done", None))

    def _build_worker(self, config: BuildConfig) -> None:
        """Worker-thread entry point. Never touches Tk widgets directly."""
        try:
            resistances = build_maxwell_model(
                config,
                progress=lambda message: self._build_queue.put(("progress", message)),
            )
        except BaseException:
            self._build_queue.put(("error", (config, traceback.format_exc())))
        else:
            self._build_queue.put(("success", (config, resistances)))
        finally:
            self._build_queue.put(("done", None))

    def _poll_build_queue(self) -> None:
        """Consume worker messages on the Tk main thread."""
        finished = False
        while True:
            try:
                kind, payload = self._build_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "progress":
                self.status.set(str(payload))
            elif kind == "error":
                config, tb_text = payload
                self._show_build_error(config, tb_text)
            elif kind == "success":
                config, resistances = payload
                self._show_build_success(config, resistances)
            elif kind == "optimetrics_error":
                config, tb_text = payload
                self._show_optimetrics_error(config, tb_text)
            elif kind == "optimetrics_success":
                config, bounds = payload
                self._show_optimetrics_success(config, bounds)
            elif kind == "done":
                finished = True

        if finished:
            self.progress_bar.stop()
            self.build_button.configure(state="normal", text="建立 Maxwell2D 模型")
            self.optimetrics_button.configure(state="normal", text="添加 Optimization Setup")
            self._build_thread = None
        elif self._build_thread is not None:
            # Keep polling while the worker is alive or until its final queued
            # messages have been consumed. Tk remains fully responsive here.
            self.root.after(100, self._poll_build_queue)

    def _show_build_error(self, config: BuildConfig, tb_text: str) -> None:
        log_path = get_log_path("linear_accelerator_builder_error.log")
        try:
            log_path.write_text(tb_text, encoding="utf-8")
        except Exception:
            pass

        # Show the final exception line prominently, with the full traceback in
        # the log file. This avoids an empty-looking GUI when AEDT startup fails.
        meaningful = [line.strip() for line in tb_text.splitlines() if line.strip()]
        last_line = meaningful[-1] if meaningful else "未知错误"
        self.status.set("建模失败。")
        messagebox.showerror(
            "建模失败",
            f"{last_line}\n\n完整 traceback 已尝试写入：\n{log_path}\n\n"
            f"目标工程：{config.project_path}",
            parent=self.root,
        )

    def _show_build_success(self, config: BuildConfig, resistances: list[float]) -> None:
        self.status.set(f"完成：{config.project_path}")
        r_text = ", ".join(f"R{i+1}={r:.5g}Ω" for i, r in enumerate(resistances[:8]))
        if len(resistances) > 8:
            r_text += ", …"
        messagebox.showinfo(
            "建模完成",
            f"已建立 {len(config.coils)} 级 Maxwell2D 线圈。\n"
            f"求解类型：TransientZ / Cylindrical About Z\n"
            f"运行模式：{'非图形化后台模式' if config.non_graphical else '图形模式'}\n"
            f"绕组类型：{DEFAULT_WINDING_TYPE}\n"
            f"工程：{config.project_path}\n"
            f"会话：{'后台 AEDT 已释放并关闭' if config.non_graphical else 'PyAEDT 控制已释放；AEDT 保持打开'}\n\n"
            f"直流电阻预览：{r_text}",
            parent=self.root,
        )

    def _show_optimetrics_error(self, config: BuildConfig, tb_text: str) -> None:
        log_path = get_log_path("linear_accelerator_optimetrics_error.log")
        try:
            log_path.write_text(tb_text, encoding="utf-8")
        except Exception:
            pass
        meaningful = [line.strip() for line in tb_text.splitlines() if line.strip()]
        last_line = meaningful[-1] if meaningful else "Unknown error"
        self.status.set("Optimetrics 创建失败。")
        messagebox.showerror(
            "Optimetrics 创建失败",
            f"{last_line}\n\n完整 traceback：\n{log_path}\n\n目标工程：{config.project_path}",
            parent=self.root,
        )

    def _show_optimetrics_success(
        self,
        config: BuildConfig,
        bounds: dict[str, tuple[float, float]],
    ) -> None:
        self.status.set(f"Optimetrics 已写入：{config.project_path}")
        messagebox.showinfo(
            "Optimetrics 创建完成",
            "已添加 AcceleratorScreeningOptimization。\n"
            "Optimizer：保留 AEDT 默认值\n"
            "目标：Maximize Moving1.Speed"
            + ("；Minimize $device_weight\n" if config.material_weight_optimization else "\n")
            + f"优化变量：{len(bounds)} 个；范围已作为纯数字写入 Optimetrics setup。",
            parent=self.root,
        )


def main() -> None:
    root = tk.Tk()
    try:
        # Native Windows appearance where available.
        if sys.platform.startswith("win"):
            try:
                from ctypes import windll

                windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        configure_gui_fonts(root)
        from maxwell_circuit_toolkit.gui import CircuitBuilderGUI

        settings = load_builder_settings()
        root.title(WINDOW_TITLE)
        root.minsize(1160, 700)
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)
        model_page = ttk.Frame(notebook)
        circuit_page = ttk.Frame(notebook)
        notebook.add(model_page, text="Maxwell2D 建模器")
        notebook.add(circuit_page, text="External Circuit 生成器")

        model_gui = AcceleratorBuilderGUI(model_page, settings=settings, manage_window=False)
        CircuitBuilderGUI(
            circuit_page,
            settings=settings,
            config_path=CONFIG_PATH,
            log_dir=LOG_DIR,
            diode_parameters=diode_model_parameters(settings),
            project_path=model_gui.project_path,
            maxwell_design=model_gui.design_name,
            aedt_version=model_gui.aedt_version,
            non_graphical=model_gui.non_graphical,
            manage_window=False,
        )
        root.protocol(
            "WM_DELETE_WINDOW",
            lambda: (save_last_project_path(model_gui.project_path.get()), root.destroy()),
        )
        root.mainloop()
    except Exception:
        log_path = get_log_path("linear_accelerator_builder_error.log")
        try:
            log_path.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        try:
            messagebox.showerror("启动失败", f"程序启动失败。\n错误日志：{log_path}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
