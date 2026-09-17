"""Reusable Maxwell 2D modeling helpers for PyAEDT 0.25.x.

The module deliberately keeps a thin boundary around the public PyAEDT API.
It does not import PyAEDT until :func:`launch_maxwell2d` is called, which makes
it possible to lint, unit-test, and reuse the model-description logic on a
machine without AEDT installed.

The most important winding helpers are:

* :meth:`Maxwell2DToolkit.set_geometry_as_winding`: one-step PyAEDT 0.25 route.
* :meth:`Maxwell2DToolkit.create_winding_from_coils`: explicit coil terminals,
  turns, polarity, and winding membership.
* :meth:`Maxwell2DToolkit.create_paired_winding`: convenient positive/negative
  coil construction for a sectioned 2D coil.

PyAEDT 0.25 implements ``assign_winding(assignment=...)`` by creating a Winding
Group, assigning a Coil boundary to every selected 2D object, and adding those
coil boundaries to the winding.  The explicit route is preferable whenever
turn count or polarity differs between objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence
import logging

from .si import (
    format_aedt_value_from_si,
    format_si_number,
    normalize_aedt_scalar,
    verify_aedt_supported_real_variables,
)

Scalar = int | float | str
Selection = str | Any
POSITION_MODES = {"reluctance", "induction"}
COPPER_RESISTIVITY_OHM_M = 1.724e-8
ALUMINUM_RESISTIVITY_OHM_M = 2.82e-8
COPPER_DENSITY_KG_M3 = 8960
ALUMINUM_DENSITY_KG_M3 = 2700
CAPACITOR_ENERGY_DENSITY_VARIABLE = "$capacitor_energy_density"
DEVICE_WEIGHT_VARIABLE = "$device_weight"
PROJECTILE_INITIAL_Z_VARIABLE = "$proj_initZ"
PROJECTILE_INITIAL_Z_DEFAULT = "0mm"

LOGGER = logging.getLogger(__name__)


class Maxwell2DToolkitError(RuntimeError):
    """Raised when a toolkit operation cannot be completed."""


@dataclass(frozen=True, slots=True)
class CoilSpec:
    """Description of one Maxwell 2D Coil boundary.

    Parameters
    ----------
    assignment:
        One object, one object name, or a sequence of objects/object names.
        Maxwell 2D coil assignments are object selections, not face selections.
    turns:
        Number of conductors/turns stored in the Coil boundary.
    polarity:
        ``"Positive"`` or ``"Negative"``.
    name:
        Optional Coil boundary name.  A deterministic name is generated when
        omitted.
    """

    assignment: Selection | Sequence[Selection]
    turns: int = 1
    polarity: str = "Positive"
    name: str | None = None


@dataclass(slots=True)
class WindingResult:
    """Objects returned after creating a winding and its coil boundaries."""

    winding: Any
    coils: list[Any] = field(default_factory=list)

    @property
    def winding_name(self) -> str:
        return _name_of(self.winding)

    @property
    def coil_names(self) -> list[str]:
        return [_name_of(coil) for coil in self.coils]


@dataclass(slots=True)
class BuildResult:
    """Registry returned by :func:`build_from_spec`."""

    objects: dict[str, Any] = field(default_factory=dict)
    windings: dict[str, WindingResult] = field(default_factory=dict)
    setups: dict[str, Any] = field(default_factory=dict)
    region: Any | None = None


def check_pyaedt_version(
    expected_prefix: str = "0.25",
    *,
    strict: bool = False,
) -> str | None:
    """Return the installed PyAEDT version and optionally enforce 0.25.x.

    Parameters
    ----------
    expected_prefix:
        Required version prefix.  The default accepts every 0.25 patch release.
    strict:
        When ``True``, raise if PyAEDT is absent or the version does not match.
    """

    try:
        installed = package_version("pyaedt")
    except PackageNotFoundError:
        if strict:
            raise Maxwell2DToolkitError(
                "PyAEDT is not installed. Install it with "
                "`python -m pip install 'pyaedt>=0.25,<0.26'`."
            ) from None
        return None

    if not installed.startswith(expected_prefix):
        message = (
            f"This library targets PyAEDT {expected_prefix}.x, but {installed} "
            "is installed. Review the PyAEDT release notes before proceeding."
        )
        if strict:
            raise Maxwell2DToolkitError(message)
        LOGGER.warning(message)
    return installed


def launch_maxwell2d(
    *,
    project: str | Path | None = None,
    design: str | None = None,
    solution_type: str = "TransientXY",
    version: str | None = None,
    non_graphical: bool = False,
    new_desktop: bool = True,
    close_on_exit: bool = False,
    student_version: bool = False,
    machine: str = "",
    port: int = 0,
    remove_lock: bool = False,
    strict_pyaedt_025: bool = False,
    **kwargs: Any,
) -> Any:
    """Launch or connect to a Maxwell 2D design.

    ``version`` is the AEDT desktop version (for example ``"2025.2"``), not
    the PyAEDT package version.  Leave it as ``None`` to let PyAEDT choose an
    installed AEDT release.
    """

    # No PyAEDT package-version gate is enforced here.  The argument is kept
    # only for backward compatibility with scripts that may still pass it.
    # If explicitly requested, the optional helper can still perform a check.
    if strict_pyaedt_025:
        check_pyaedt_version(strict=True)
    try:
        from ansys.aedt.core import Maxwell2d
    except ImportError as exc:  # pragma: no cover - requires AEDT environment
        raise Maxwell2DToolkitError(
            "Cannot import `ansys.aedt.core.Maxwell2d`. Confirm that PyAEDT "
            "is installed in this Python environment."
        ) from exc

    project_value = str(project) if project is not None else None
    return Maxwell2d(
        project=project_value,
        design=design,
        solution_type=solution_type,
        version=version,
        non_graphical=non_graphical,
        new_desktop=new_desktop,
        close_on_exit=close_on_exit,
        student_version=student_version,
        machine=machine,
        port=port,
        remove_lock=remove_lock,
        **kwargs,
    )


def _aedt_script_object(app: Any, private_name: str, public_name: str) -> Any:
    """Return an AEDT scripting object exposed by a PyAEDT application.

    PyAEDT has historically exposed the native scripting proxies through
    private attributes such as ``_oproject``/``_odesign`` while some releases
    also provide public aliases.  Prefer the low-level proxy requested by this
    toolkit and keep a public-name fallback for compatibility.
    """

    obj = getattr(app, private_name, None)
    if obj is None:
        obj = getattr(app, public_name, None)
    if obj is None:
        raise Maxwell2DToolkitError(
            f"PyAEDT application does not expose {private_name!r} or {public_name!r}."
        )
    return obj


def _batch_change_variables_low_level(
    host: Any,
    *,
    prop_tab: str,
    prop_server: str,
    name_to_value: Mapping[str, Scalar],
    require_project_prefix: bool,
    verify: bool = True,
) -> None:
    """Create and/or update many AEDT variables in one ``ChangeProperty`` call.

    AEDT's ``ChangedProps`` section edits properties that already exist;
    variables that do not yet exist must be placed in ``NewProps`` and declared
    as ``VariableProp``.  This helper queries the existing variable names once,
    partitions the mapping locally, and sends both arrays in a single native
    scripting call.  A final name-only verification is also one native call,
    so the number of AEDT round trips does not grow with the variable count.
    """

    if not name_to_value:
        return

    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_name, raw_value in name_to_value.items():
        name = str(raw_name).strip()
        if not name:
            raise ValueError("Variable name cannot be empty.")
        if require_project_prefix:
            if not name.startswith("$"):
                raise ValueError(f"Project/global variable must start with '$': {name!r}")
        elif name.startswith("$"):
            raise ValueError(f"Local/design variable must not start with '$': {name!r}")
        if name in seen:
            raise ValueError(f"Duplicate variable name: {name!r}")
        seen.add(name)
        normalized.append((name, str(raw_value)))

    try:
        existing = set(host.GetVariables() or [])
    except Exception as exc:
        raise Maxwell2DToolkitError(
            f"Could not query existing variables with GetVariables(): {exc}"
        ) from exc

    new_props: list[Any] = ["NAME:NewProps"]
    changed_props: list[Any] = ["NAME:ChangedProps"]

    for name, value in normalized:
        if name in existing:
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

    tab_args: list[Any] = [
        "NAME:" + prop_tab,
        ["NAME:PropServers", prop_server],
    ]
    if len(new_props) > 1:
        tab_args.append(new_props)
    if len(changed_props) > 1:
        tab_args.append(changed_props)

    try:
        host.ChangeProperty(["NAME:AllTabs", tab_args])
    except Exception as exc:
        raise Maxwell2DToolkitError(
            f"AEDT ChangeProperty failed while registering {len(normalized)} variables: {exc}"
        ) from exc

    if verify:
        try:
            after = set(host.GetVariables() or [])
        except Exception as exc:
            raise Maxwell2DToolkitError(
                f"Variables were sent to AEDT, but final GetVariables() verification failed: {exc}"
            ) from exc
        missing = [name for name, _ in normalized if name not in after]
        if missing:
            preview = ", ".join(missing[:8])
            if len(missing) > 8:
                preview += ", ..."
            raise Maxwell2DToolkitError(
                "AEDT did not retain all requested variables. Missing: " + preview
            )


def _set_local_variables_low_level(
    Mxw2d: Any,
    name_to_value: Mapping[str, Scalar],
    *,
    verify: bool = True,
) -> None:
    """Batch-create/update Maxwell design-local variables with one ChangeProperty.

    Variable names must not start with ``$``.  Existing variables are emitted
    under ``ChangedProps`` and new variables under ``NewProps``.
    """

    odesign = _aedt_script_object(Mxw2d, "_odesign", "odesign")
    _batch_change_variables_low_level(
        odesign,
        prop_tab="LocalVariableTab",
        prop_server="LocalVariables",
        name_to_value=name_to_value,
        require_project_prefix=False,
        verify=verify,
    )


def _set_global_variables_low_level(
    Mxw2d: Any,
    name_to_value: Mapping[str, Scalar],
    *,
    verify: bool = True,
) -> None:
    """Batch-create/update Maxwell project/global variables with one ChangeProperty.

    Project variable names must start with ``$``.  Existing variables are
    emitted under ``ChangedProps`` and new variables under ``NewProps``.
    """

    oproject = _aedt_script_object(Mxw2d, "_oproject", "oproject")
    _batch_change_variables_low_level(
        oproject,
        prop_tab="ProjectVariableTab",
        prop_server="ProjectVariables",
        name_to_value=name_to_value,
        require_project_prefix=True,
        verify=verify,
    )


# Public aliases for scripts/skills that should not depend on private names.
set_local_variables_low_level = _set_local_variables_low_level
set_global_variables_low_level = _set_global_variables_low_level


class Maxwell2DToolkit:
    """Thin, checked helpers around a live ``ansys.aedt.core.Maxwell2d`` app."""

    def __init__(self, app: Any, *, logger: logging.Logger | None = None) -> None:
        if app is None:
            raise ValueError("`app` must be a live Maxwell2d application object.")
        self.app = app
        self.modeler = app.modeler
        self.log = logger or LOGGER

    # ------------------------------------------------------------------
    # Design and parameter helpers
    # ------------------------------------------------------------------
    def set_model_units(self, units: str) -> None:
        """Set the active Maxwell 2D model units, for example ``"mm"``."""

        self.modeler.model_units = units

    def set_model_depth(self, depth: Scalar) -> None:
        """Set the out-of-plane depth used by a planar Maxwell 2D design."""

        self.app.model_depth = depth

    def set_local_variables(self, variables: Mapping[str, Scalar], *, verify: bool = True) -> None:
        """Batch-create/update design-local variables using native AEDT scripting."""

        _set_local_variables_low_level(self.app, variables, verify=verify)

    def set_global_variables(self, variables: Mapping[str, Scalar], *, verify: bool = True) -> None:
        """Batch-create/update project/global variables using native AEDT scripting."""

        _set_global_variables_low_level(self.app, variables, verify=verify)

    def verify_supported_real_variables(self, variables: Iterable[str]) -> dict[str, float]:
        """Verify official AEDT units and return finite real SI values."""

        return verify_aedt_supported_real_variables(self.app, variables)

    def set_variables(self, variables: Mapping[str, Scalar], *, verify: bool = True) -> None:
        """Batch-create/update a mixed mapping of local and project variables.

        Names beginning with ``$`` are project variables; all other names are
        design-local variables.  At most one low-level registration call is
        made for each variable scope.
        """

        local_vars: dict[str, Scalar] = {}
        global_vars: dict[str, Scalar] = {}
        for name, value in variables.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Invalid variable name: {name!r}")
            (global_vars if name.startswith("$") else local_vars)[name] = value
        if local_vars:
            self.set_local_variables(local_vars, verify=verify)
        if global_vars:
            self.set_global_variables(global_vars, verify=verify)

    # ------------------------------------------------------------------
    # Geometry creation
    # ------------------------------------------------------------------
    def rectangle(
        self,
        origin: Sequence[Scalar],
        sizes: Sequence[Scalar],
        *,
        name: str | None = None,
        material: str | None = None,
        is_covered: bool = True,
        non_model: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Create a covered Maxwell 2D rectangle."""

        return _require_result(
            self.modeler.create_rectangle(
                origin=origin,
                sizes=sizes,
                is_covered=is_covered,
                name=name,
                material=material,
                non_model=non_model,
                **kwargs,
            ),
            "create rectangle",
        )

    def circle(
        self,
        origin: Sequence[Scalar],
        radius: Scalar,
        *,
        name: str | None = None,
        material: str | None = None,
        num_sides: int = 0,
        is_covered: bool = True,
        non_model: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Create a covered circle."""

        return _require_result(
            self.modeler.create_circle(
                origin=origin,
                radius=radius,
                num_sides=num_sides,
                is_covered=is_covered,
                name=name,
                material=material,
                non_model=non_model,
                **kwargs,
            ),
            "create circle",
        )

    def ellipse(
        self,
        origin: Sequence[Scalar],
        major_radius: Scalar,
        ratio: Scalar,
        *,
        name: str | None = None,
        material: str | None = None,
        is_covered: bool = True,
        non_model: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Create a covered ellipse."""

        return _require_result(
            self.modeler.create_ellipse(
                origin=origin,
                major_radius=major_radius,
                ratio=ratio,
                is_covered=is_covered,
                name=name,
                material=material,
                non_model=non_model,
                **kwargs,
            ),
            "create ellipse",
        )

    def regular_polygon(
        self,
        center: Sequence[Scalar],
        start_point: Sequence[Scalar],
        *,
        num_sides: int = 6,
        name: str | None = None,
        material: str | None = None,
        is_covered: bool = True,
        non_model: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Create a covered regular polygon."""

        return _require_result(
            self.modeler.create_regular_polygon(
                origin=center,
                start_point=start_point,
                num_sides=num_sides,
                is_covered=is_covered,
                name=name,
                material=material,
                non_model=non_model,
                **kwargs,
            ),
            "create regular polygon",
        )

    def polyline(
        self,
        points: Sequence[Sequence[Scalar]],
        *,
        name: str | None = None,
        material: str | None = None,
        close_surface: bool = False,
        cover_surface: bool = False,
        non_model: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Create a line or a covered polygonal sheet from points."""

        return _require_result(
            self.modeler.create_polyline(
                points=points,
                name=name,
                material=material,
                close_surface=close_surface,
                cover_surface=cover_surface,
                non_model=non_model,
                **kwargs,
            ),
            "create polyline",
        )

    def ring(
        self,
        center: Sequence[Scalar],
        outer_radius: Scalar,
        inner_radius: Scalar,
        *,
        name: str,
        material: str | None = None,
        keep_inner: bool = False,
    ) -> Any:
        """Create an annulus by subtracting one circle from another."""

        outer = self.circle(center, outer_radius, name=name, material=material)
        inner = self.circle(
            center,
            inner_radius,
            name=f"{name}__inner_tool",
            material=material,
        )
        self.subtract(outer, inner, keep_originals=keep_inner)
        return outer

    def rectangular_frame(
        self,
        outer_origin: Sequence[Scalar],
        outer_sizes: Sequence[Scalar],
        inner_origin: Sequence[Scalar],
        inner_sizes: Sequence[Scalar],
        *,
        name: str,
        material: str | None = None,
        keep_inner: bool = False,
    ) -> Any:
        """Create a rectangular frame by Boolean subtraction."""

        outer = self.rectangle(
            outer_origin, outer_sizes, name=name, material=material
        )
        inner = self.rectangle(
            inner_origin,
            inner_sizes,
            name=f"{name}__inner_tool",
            material=material,
        )
        self.subtract(outer, inner, keep_originals=keep_inner)
        return outer

    # ------------------------------------------------------------------
    # Geometry editing and material assignment
    # ------------------------------------------------------------------
    def assign_material(self, assignment: Selection | Sequence[Selection], material: str) -> None:
        """Assign an AEDT material to one or more objects."""

        ok = self.app.assign_material(_names_of(assignment), material)
        _require_true(ok, f"assign material {material!r}")

    def unite(
        self,
        assignment: Selection | Sequence[Selection],
        *,
        purge: bool = False,
        keep_originals: bool = False,
    ) -> Any:
        """Unite selected objects."""

        return _require_result(
            self.modeler.unite(
                _names_of(assignment),
                purge=purge,
                keep_originals=keep_originals,
            ),
            "unite objects",
        )

    def subtract(
        self,
        blank: Selection | Sequence[Selection],
        tools: Selection | Sequence[Selection],
        *,
        keep_originals: bool = False,
    ) -> Any:
        """Subtract tool objects from blank objects."""

        return _require_result(
            self.modeler.subtract(
                _names_of(blank),
                _names_of(tools),
                keep_originals=keep_originals,
            ),
            "subtract objects",
        )

    def move(
        self,
        assignment: Selection | Sequence[Selection],
        vector: Sequence[Scalar],
    ) -> None:
        """Translate objects by a vector."""

        _require_true(
            self.modeler.move(_names_of(assignment), vector),
            "move objects",
        )

    def rotate(
        self,
        assignment: Selection | Sequence[Selection],
        *,
        axis: str = "Z",
        angle: Scalar = 90,
        units: str = "deg",
    ) -> None:
        """Rotate objects around an axis."""

        _require_true(
            self.modeler.rotate(
                _names_of(assignment), axis=axis, angle=angle, units=units
            ),
            "rotate objects",
        )

    def mirror(
        self,
        assignment: Selection | Sequence[Selection],
        *,
        origin: Sequence[Scalar],
        vector: Sequence[Scalar],
        duplicate: bool = False,
        duplicate_assignment: bool = True,
    ) -> Any:
        """Mirror objects about a line/plane definition accepted by PyAEDT."""

        return _require_result(
            self.modeler.mirror(
                _names_of(assignment),
                origin=origin,
                vector=vector,
                duplicate=duplicate,
                duplicate_assignment=duplicate_assignment,
            ),
            "mirror objects",
        )

    # ------------------------------------------------------------------
    # Region and boundaries
    # ------------------------------------------------------------------
    def create_region(
        self,
        *,
        pad_value: Scalar | Sequence[Scalar] = 100,
        pad_type: str = "Percentage Offset",
        name: str = "Region",
        assign_balloon: bool = True,
        balloon_name: str = "Balloon",
        is_voltage: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Create a 2D region and optionally assign Balloon to its edges."""

        region = _require_result(
            self.modeler.create_region(
                pad_value=pad_value,
                pad_type=pad_type,
                name=name,
                **kwargs,
            ),
            "create region",
        )
        if assign_balloon:
            boundary = self.app.assign_balloon(
                assignment=region.edges,
                boundary=balloon_name,
                is_voltage=is_voltage,
            )
            _require_result(boundary, "assign balloon boundary")
        return region

    # ------------------------------------------------------------------
    # Windings and coil terminals
    # ------------------------------------------------------------------
    def set_geometry_as_winding(
        self,
        assignment: Selection | Sequence[Selection],
        *,
        name: str,
        winding_type: str = "Current",
        is_solid: bool = False,
        current: Scalar = 0,
        resistance: Scalar = 0,
        inductance: Scalar = 0,
        voltage: Scalar = 0,
        parallel_branches: int = 1,
        phase: Scalar = 0,
    ) -> WindingResult:
        """Create one Winding Group directly from one or more 2D objects.

        This is the exact high-level workflow exposed by PyAEDT 0.25:
        ``assign_winding(assignment=[object names], ...)``.  PyAEDT creates one
        positive, one-turn Coil boundary per selected object and adds every Coil
        boundary to the new winding.

        Use :meth:`create_winding_from_coils` instead when individual objects
        need different turns or polarity.
        """

        object_names = _names_of(assignment)
        if not object_names:
            raise ValueError("At least one geometry object is required.")
        winding = self.app.assign_winding(
            assignment=object_names,
            winding_type=winding_type,
            is_solid=is_solid,
            current=current,
            resistance=resistance,
            inductance=inductance,
            voltage=voltage,
            parallel_branches=parallel_branches,
            phase=phase,
            name=name,
        )
        winding = _require_result(winding, f"assign winding {name!r}")
        return WindingResult(winding=winding, coils=[])

    # Alias with wording commonly used in scripts and generated skills.
    assign_geometry_as_winding = set_geometry_as_winding

    def create_empty_winding(
        self,
        *,
        name: str,
        winding_type: str = "Current",
        is_solid: bool = False,
        current: Scalar = 0,
        resistance: Scalar = 0,
        inductance: Scalar = 0,
        voltage: Scalar = 0,
        parallel_branches: int = 1,
        phase: Scalar = 0,
    ) -> Any:
        """Create a Winding Group without assigning geometry yet."""

        winding = self.app.assign_winding(
            assignment=None,
            winding_type=winding_type,
            is_solid=is_solid,
            current=current,
            resistance=resistance,
            inductance=inductance,
            voltage=voltage,
            parallel_branches=parallel_branches,
            phase=phase,
            name=name,
        )
        return _require_result(winding, f"create winding {name!r}")

    def assign_coil(
        self,
        assignment: Selection | Sequence[Selection],
        *,
        name: str,
        turns: int = 1,
        polarity: str = "Positive",
    ) -> Any:
        """Assign a Maxwell 2D Coil boundary to geometry objects."""

        if not isinstance(turns, int) or turns < 1:
            raise ValueError("`turns` must be a positive integer.")
        normalized_polarity = _normalize_polarity(polarity)
        coil = self.app.assign_coil(
            assignment=_names_of(assignment),
            conductors_number=turns,
            polarity=normalized_polarity,
            name=name,
        )
        return _require_result(coil, f"assign coil {name!r}")

    def add_coils_to_winding(
        self,
        winding: str | Any,
        coils: str | Any | Sequence[str | Any],
    ) -> None:
        """Add existing Coil boundaries to an existing Winding Group."""

        winding_name = _name_of(winding)
        coil_names = _names_of(coils)
        if not coil_names:
            raise ValueError("At least one Coil boundary is required.")
        ok = self.app.add_winding_coils(winding_name, coil_names)
        _require_true(ok, f"add coils to winding {winding_name!r}")

    def create_winding_from_coils(
        self,
        coil_specs: Sequence[CoilSpec],
        *,
        name: str,
        winding_type: str = "Current",
        is_solid: bool = False,
        current: Scalar = 0,
        resistance: Scalar = 0,
        inductance: Scalar = 0,
        voltage: Scalar = 0,
        parallel_branches: int = 1,
        phase: Scalar = 0,
    ) -> WindingResult:
        """Create explicit Coil boundaries and add them to one Winding Group.

        This is the safest reusable workflow because turn count and polarity are
        explicit for every coil object.
        """

        if not coil_specs:
            raise ValueError("`coil_specs` must contain at least one CoilSpec.")
        winding = self.create_empty_winding(
            name=name,
            winding_type=winding_type,
            is_solid=is_solid,
            current=current,
            resistance=resistance,
            inductance=inductance,
            voltage=voltage,
            parallel_branches=parallel_branches,
            phase=phase,
        )

        coils: list[Any] = []
        try:
            for index, spec in enumerate(coil_specs, start=1):
                coil_name = spec.name or f"{name}_Coil_{index}"
                coils.append(
                    self.assign_coil(
                        spec.assignment,
                        name=coil_name,
                        turns=spec.turns,
                        polarity=spec.polarity,
                    )
                )
            self.add_coils_to_winding(winding, coils)
        except Exception:
            self.log.exception(
                "Failed while building winding %s. The AEDT design may contain "
                "partially created boundaries; inspect or undo them before retrying.",
                name,
            )
            raise
        return WindingResult(winding=winding, coils=coils)

    def create_paired_winding(
        self,
        positive_assignment: Selection | Sequence[Selection],
        negative_assignment: Selection | Sequence[Selection],
        *,
        name: str,
        turns: int = 1,
        positive_coil_name: str | None = None,
        negative_coil_name: str | None = None,
        winding_type: str = "Current",
        is_solid: bool = False,
        current: Scalar = 0,
        resistance: Scalar = 0,
        inductance: Scalar = 0,
        voltage: Scalar = 0,
        parallel_branches: int = 1,
        phase: Scalar = 0,
    ) -> WindingResult:
        """Create a two-section winding with positive and negative polarity."""

        specs = [
            CoilSpec(
                assignment=positive_assignment,
                turns=turns,
                polarity="Positive",
                name=positive_coil_name or f"{name}_Positive",
            ),
            CoilSpec(
                assignment=negative_assignment,
                turns=turns,
                polarity="Negative",
                name=negative_coil_name or f"{name}_Negative",
            ),
        ]
        return self.create_winding_from_coils(
            specs,
            name=name,
            winding_type=winding_type,
            is_solid=is_solid,
            current=current,
            resistance=resistance,
            inductance=inductance,
            voltage=voltage,
            parallel_branches=parallel_branches,
            phase=phase,
        )

    # ------------------------------------------------------------------
    # Analysis setup and execution
    # ------------------------------------------------------------------
    def create_setup(
        self,
        *,
        name: str = "Setup1",
        setup_type: str | None = None,
        properties: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Create an analysis setup and update selected setup properties.

        Using a generic property mapping keeps this helper useful for Transient,
        Eddy Current, Magnetostatic, and Electric Transient solution types.
        """

        create_kwargs: dict[str, Any] = {"name": name, **kwargs}
        if setup_type is not None:
            create_kwargs["setup_type"] = setup_type
        setup = _require_result(
            self.app.create_setup(**create_kwargs),
            f"create setup {name!r}",
        )
        if properties:
            for key, value in properties.items():
                setup.props[key] = value
            _require_true(setup.update(), f"update setup {name!r}")
        return setup

    def create_transient_setup(
        self,
        *,
        name: str = "Setup1",
        stop_time: Scalar = "20ms",
        time_step: Scalar = "0.1ms",
        additional_properties: Mapping[str, Any] | None = None,
    ) -> Any:
        """Create a transient setup with ``StopTime`` and ``TimeStep``."""

        props: dict[str, Any] = {
            "StopTime": stop_time,
            "TimeStep": time_step,
        }
        if additional_properties:
            props.update(additional_properties)
        return self.create_setup(name=name, properties=props)

    def prepare_accelerator_optimization_variables(
        self,
        *,
        stage_count: int,
        z_start_prefix: str = "z_start",
        coil_length_prefix: str = "coil_Len",
        outer_diameter_prefix: str = "coil_outer_Diameter",
        turns_prefix: str = "Turn",
        gap_prefix: str = "gap",
        common_gap: bool = True,
        center_prefix: str = "coil_center",
        position_on_prefix: str = "POSon",
        position_factor_prefix: str = "u",
        position_mode: str = "reluctance",
        first_stage_position_on: Scalar = "-1000mm",
        position_duration_prefix: str = "POSdur",
        capacitance_prefix: str = "C",
        inner_diameter_prefix: str = "coil_inner_Diameter",
        common_inner_diameter: bool = True,
        fill_factor_prefix: str = "fill_factor",
        resistivity_prefix: str = "rho",
        material_selector_prefix: str = "is_copper",
        initial_voltage_variable: str = "V",
        default_position_factor: Scalar = "0.5",
        default_position_duration: Scalar = "10mm",
        default_capacitance: Scalar = "220uF",
        default_initial_voltage: Scalar = "390V",
        material_weight_optimization: bool = False,
        capacitor_energy_density_j_per_g: float = 0.85,
    ) -> dict[str, Scalar]:
        """Prepare project variables required by accelerator Optimetrics.

        Existing position-factor, duration, capacitance, and initial-voltage
        values are preserved. Coil-center and POSon expressions are always
        normalized for the selected reluctance or induction mode. Optional
        dependent variables calculate coil, capacitor, and total device weight.
        """

        if stage_count < 1:
            raise ValueError("At least one coil stage is required.")
        if position_mode not in POSITION_MODES:
            raise ValueError(f"Unsupported POSon mode: {position_mode!r}")
        first_stage_position_on = normalize_aedt_scalar(first_stage_position_on, "length")
        default_position_duration = normalize_aedt_scalar(default_position_duration, "length")
        default_capacitance = normalize_aedt_scalar(default_capacitance, "capacitance")
        default_initial_voltage = normalize_aedt_scalar(default_initial_voltage, "voltage")
        available = self.app.variable_manager.variables
        z_start_names = [f"${z_start_prefix}_{index}" for index in range(1, stage_count + 1)]
        length_names = [f"${coil_length_prefix}_{index}" for index in range(1, stage_count + 1)]
        missing_geometry = [name for name in z_start_names + length_names if name not in available]
        if missing_geometry:
            raise ValueError(
                "Cannot prepare Optimetrics variables because geometry variables are missing: "
                + ", ".join(missing_geometry)
            )

        center_names = [f"${center_prefix}_{index}" for index in range(1, stage_count + 1)]
        position_on_names = [f"${position_on_prefix}_{index}" for index in range(1, stage_count + 1)]
        duration_names = [f"${position_duration_prefix}_{index}" for index in range(1, stage_count + 1)]
        capacitance_names = [f"${capacitance_prefix}_{index}" for index in range(1, stage_count + 1)]
        outer_diameter_names = [
            f"${outer_diameter_prefix}_{index}" for index in range(1, stage_count + 1)
        ]
        fill_factor_names = [f"${fill_factor_prefix}_{index}" for index in range(1, stage_count + 1)]
        resistivity_names = [f"${resistivity_prefix}_{index}" for index in range(1, stage_count + 1)]
        material_selector_names = [
            f"${material_selector_prefix}_{index}" for index in range(1, stage_count + 1)
        ]
        inner_diameter_names = (
            [f"${inner_diameter_prefix}"] * stage_count
            if common_inner_diameter
            else [f"${inner_diameter_prefix}_{index}" for index in range(1, stage_count + 1)]
        )

        foundation: dict[str, Scalar] = {}
        position_on_expressions: dict[str, Scalar] = {}
        if PROJECTILE_INITIAL_Z_VARIABLE not in available:
            foundation[PROJECTILE_INITIAL_Z_VARIABLE] = PROJECTILE_INITIAL_Z_DEFAULT
        for index in range(1, stage_count + 1):
            center = center_names[index - 1]
            foundation[center] = f"{z_start_names[index - 1]}+{length_names[index - 1]}/2"
            if index == 1:
                position_on_expressions[position_on_names[0]] = first_stage_position_on
            else:
                factor = f"${position_factor_prefix}_{index}"
                if factor not in available:
                    foundation[factor] = default_position_factor
                if position_mode == "reluctance":
                    previous_center = center_names[index - 2]
                    position_on_expressions[position_on_names[index - 1]] = (
                        f"{previous_center}+{factor}*({center}-{previous_center})"
                    )
                else:
                    length = length_names[index - 1]
                    position_on_expressions[position_on_names[index - 1]] = (
                        f"{center}-{length}/2+{factor}*{length}"
                    )
            if duration_names[index - 1] not in available:
                foundation[duration_names[index - 1]] = default_position_duration
            if capacitance_names[index - 1] not in available:
                foundation[capacitance_names[index - 1]] = default_capacitance

        weight_expressions: dict[str, Scalar] = {}
        if material_weight_optimization:
            if capacitor_energy_density_j_per_g <= 0:
                raise ValueError("Capacitor energy density must be positive.")
            weight_sources = (
                outer_diameter_names
                + fill_factor_names
                + resistivity_names
                + inner_diameter_names
            )
            missing_weight_sources = [
                name for name in dict.fromkeys(weight_sources) if name not in available
            ]
            if missing_weight_sources:
                raise ValueError(
                    "Cannot calculate device weight because project variables are missing: "
                    + ", ".join(missing_weight_sources)
                )
            voltage_name = "$" + initial_voltage_variable.lstrip("$")
            if voltage_name not in available:
                foundation[voltage_name] = default_initial_voltage
            foundation[CAPACITOR_ENERGY_DENSITY_VARIABLE] = format_si_number(
                capacitor_energy_density_j_per_g * 1000
            )
            total_terms: list[str] = []
            for index in range(1, stage_count + 1):
                selector_name = material_selector_names[index - 1]
                density_name = f"$material_density_{index}"
                coil_weight_name = f"$coil_weight_{index}"
                capacitor_weight_name = f"$capacitor_weight_{index}"
                rho_name = resistivity_names[index - 1]
                if selector_name not in available:
                    rho_value = verify_aedt_supported_real_variables(self.app, [rho_name])[rho_name]
                    foundation[selector_name] = int(
                        abs(rho_value - COPPER_RESISTIVITY_OHM_M)
                        <= abs(rho_value - ALUMINUM_RESISTIVITY_OHM_M)
                    )
                weight_expressions[rho_name] = (
                    f"if({selector_name}==1,{COPPER_RESISTIVITY_OHM_M:.12g}Ohmm,"
                    f"{ALUMINUM_RESISTIVITY_OHM_M:.12g}Ohmm)"
                )
                weight_expressions[density_name] = (
                    f"if({selector_name}==1,{COPPER_DENSITY_KG_M3}kg_per_m3,"
                    f"{ALUMINUM_DENSITY_KG_M3}kg_per_m3)"
                )
                weight_expressions[coil_weight_name] = (
                    f"3.141592653589793/4*{fill_factor_names[index - 1]}*"
                    f"({outer_diameter_names[index - 1]}^2-{inner_diameter_names[index - 1]}^2)*"
                    f"{length_names[index - 1]}*{density_name}"
                )
                weight_expressions[capacitor_weight_name] = (
                    f"0.5*{capacitance_names[index - 1]}*{voltage_name}^2/"
                    f"{CAPACITOR_ENERGY_DENSITY_VARIABLE}"
                )
                total_terms.extend((coil_weight_name, capacitor_weight_name))
            weight_expressions[DEVICE_WEIGHT_VARIABLE] = "+".join(total_terms)

        self.set_global_variables(foundation, verify=True)
        self.set_global_variables(position_on_expressions, verify=True)
        if weight_expressions:
            self.set_global_variables(weight_expressions, verify=True)
        generated = {**foundation, **position_on_expressions, **weight_expressions}
        self.verify_supported_real_variables(generated)
        return generated

    def create_accelerator_screening_optimization(
        self,
        *,
        stage_count: int,
        z_start_prefix: str = "z_start",
        coil_length_prefix: str = "coil_Len",
        outer_diameter_prefix: str = "coil_outer_Diameter",
        turns_prefix: str = "Turn",
        gap_prefix: str = "gap",
        common_gap: bool = True,
        center_prefix: str = "coil_center",
        position_on_prefix: str = "POSon",
        position_factor_prefix: str = "u",
        position_mode: str = "reluctance",
        first_stage_position_on: Scalar = "-1000mm",
        position_duration_prefix: str = "POSdur",
        capacitance_prefix: str = "C",
        inner_diameter_prefix: str = "coil_inner_Diameter",
        common_inner_diameter: bool = True,
        fill_factor_prefix: str = "fill_factor",
        resistivity_prefix: str = "rho",
        material_selector_prefix: str = "is_copper",
        initial_voltage_variable: str = "V",
        default_position_factor: Scalar = "0.5",
        default_position_duration: Scalar = "10mm",
        default_capacitance: Scalar = "220uF",
        default_initial_voltage: Scalar = "390V",
        material_weight_optimization: bool = False,
        capacitor_energy_density_j_per_g: float = 0.85,
        prepare_variables: bool = True,
        name: str = "AcceleratorScreeningOptimization",
        calculation: str = "Moving1.Speed",
        solution: str | None = None,
        replace_existing: bool = True,
    ) -> tuple[Any, dict[str, tuple[float, float]]]:
        """Create an accelerator optimization setup with SI bounds and AEDT units.

        Required project variables are prepared before Optimetrics is created.
        Existing position-factor, duration, and capacitance values are
        preserved, while coil centers and POSon expressions are normalized to
        the selected parameterization. Center-to-center distances and current
        geometry values are evaluated, so every variation contains literal
        numbers rather than variable expressions. Coil length, outer diameter,
        turns, and the configured shared/independent gaps are varied from 50%
        to 150% of their current values. Stage 1 is fixed in both position
        modes and has no position-factor variable; its POS duration uses the
        first interval.
        """

        if stage_count < 2:
            raise ValueError("Accelerator optimization requires at least two coil stages.")
        if position_mode not in POSITION_MODES:
            raise ValueError(f"Unsupported POSon mode: {position_mode!r}")

        center_names = [f"${center_prefix}_{index}" for index in range(1, stage_count + 1)]
        length_names = [f"${coil_length_prefix}_{index}" for index in range(1, stage_count + 1)]
        outer_diameter_names = [
            f"${outer_diameter_prefix}_{index}" for index in range(1, stage_count + 1)
        ]
        turns_names = [f"${turns_prefix}_{index}" for index in range(1, stage_count + 1)]
        gap_names = (
            [f"${gap_prefix}"]
            if common_gap
            else [f"${gap_prefix}_{index}" for index in range(1, stage_count)]
        )
        factor_names = [
            f"${position_factor_prefix}_{index}" for index in range(2, stage_count + 1)
        ]
        duration_names = [f"${position_duration_prefix}_{index}" for index in range(1, stage_count + 1)]
        capacitance_names = [f"${capacitance_prefix}_{index}" for index in range(1, stage_count + 1)]
        resistivity_names = [f"${resistivity_prefix}_{index}" for index in range(1, stage_count + 1)]
        material_selector_names = [
            f"${material_selector_prefix}_{index}" for index in range(1, stage_count + 1)
        ]
        if prepare_variables:
            self.prepare_accelerator_optimization_variables(
                stage_count=stage_count,
                z_start_prefix=z_start_prefix,
                coil_length_prefix=coil_length_prefix,
                outer_diameter_prefix=outer_diameter_prefix,
                turns_prefix=turns_prefix,
                gap_prefix=gap_prefix,
                common_gap=common_gap,
                center_prefix=center_prefix,
                position_on_prefix=position_on_prefix,
                position_factor_prefix=position_factor_prefix,
                position_mode=position_mode,
                first_stage_position_on=first_stage_position_on,
                position_duration_prefix=position_duration_prefix,
                capacitance_prefix=capacitance_prefix,
                inner_diameter_prefix=inner_diameter_prefix,
                common_inner_diameter=common_inner_diameter,
                fill_factor_prefix=fill_factor_prefix,
                resistivity_prefix=resistivity_prefix,
                material_selector_prefix=material_selector_prefix,
                initial_voltage_variable=initial_voltage_variable,
                default_position_factor=default_position_factor,
                default_position_duration=default_position_duration,
                default_capacitance=default_capacitance,
                default_initial_voltage=default_initial_voltage,
                material_weight_optimization=material_weight_optimization,
                capacitor_energy_density_j_per_g=capacitor_energy_density_j_per_g,
            )

        required = (
            center_names
            + factor_names
            + duration_names
            + capacitance_names
            + length_names
            + outer_diameter_names
            + turns_names
            + gap_names
            + [PROJECTILE_INITIAL_Z_VARIABLE]
            + (
                resistivity_names + material_selector_names + [DEVICE_WEIGHT_VARIABLE]
                if material_weight_optimization
                else []
            )
        )
        available = self.app.variable_manager.variables
        missing = [variable for variable in required if variable not in available]
        if missing:
            raise ValueError("Missing project variables required by Optimetrics: " + ", ".join(missing))
        evaluated = self.verify_supported_real_variables(required)

        centers_m = [evaluated[variable] for variable in center_names]
        intervals_m = [
            float(format_si_number(right - left))
            for left, right in zip(centers_m, centers_m[1:])
        ]
        if any(interval <= 0.0 for interval in intervals_m):
            raise ValueError("Coil center variables must evaluate to strictly increasing positions.")
        if any(interval < 0.001 for interval in intervals_m):
            raise ValueError("Every coil center interval must be at least 1mm for the POSdur bounds.")

        bounds: dict[str, tuple[float, float]] = {}
        factor_bounds = (-0.2, 1.2) if position_mode == "reluctance" else (-0.3, 1.6)
        for variable in factor_names:
            bounds[variable] = factor_bounds
        for index, variable in enumerate(duration_names, start=1):
            upper = intervals_m[0] if index == 1 else intervals_m[index - 2]
            bounds[variable] = (0.001, upper)
        for variable in capacitance_names:
            bounds[variable] = (100e-6, 1200e-6)
        for variable in length_names + outer_diameter_names:
            current = evaluated[variable]
            if current <= 0.0:
                raise ValueError(f"Geometry optimization variable must be positive: {variable}")
            bounds[variable] = (
                float(format_si_number(current * 0.5)),
                float(format_si_number(current * 1.5)),
            )
        for variable in turns_names:
            current = evaluated[variable]
            if current <= 0.0:
                raise ValueError(f"Turns optimization variable must be positive: {variable}")
            bounds[variable] = (
                float(format_si_number(current * 0.5)),
                float(format_si_number(current * 1.5)),
            )
        for variable in gap_names:
            current = evaluated[variable]
            if current < 0.0:
                raise ValueError(f"Gap optimization variable must not be negative: {variable}")
            bounds[variable] = (
                float(format_si_number(current * 0.5)),
                float(format_si_number(current * 1.5)),
            )
        if material_weight_optimization:
            for variable in material_selector_names:
                bounds[variable] = (0.0, 1.0)
        bounds[PROJECTILE_INITIAL_Z_VARIABLE] = (0.0, 0.012)

        optimizations = self.app.optimizations
        existing = getattr(optimizations, "design_setups", {})
        if name in existing:
            if not replace_existing:
                raise ValueError(f"Optimetrics setup {name!r} already exists.")
            _require_true(optimizations.delete(name), f"delete Optimetrics setup {name!r}")

        active_solution = solution or getattr(self.app, "nominal_sweep", None)
        if not active_solution:
            raise ValueError("No nominal Maxwell analysis setup is available for Optimetrics.")
        setup = _require_result(
            optimizations.add(
                calculation=calculation,
                ranges={"Time": None},
                optimization_type="Optimization",
                condition="Maximize",
                goal_value=0,
                solution=active_solution,
                name=name,
            ),
            f"create Optimetrics setup {name!r}",
        )
        setup.auto_update = False
        if "Variables" not in setup.props:
            setup.props["Variables"] = {}
        if "StartingPoint" not in setup.props:
            setup.props["StartingPoint"] = {}
        for variable, (minimum, maximum) in bounds.items():
            is_material_selector = variable in material_selector_names and material_weight_optimization
            unit = str(getattr(available[variable], "units", "") or "").strip()
            minimum_value = format_aedt_value_from_si(minimum, unit)
            maximum_value = format_aedt_value_from_si(maximum, unit)
            minimum_number = minimum_value[: -len(unit)] if unit else minimum_value
            maximum_number = maximum_value[: -len(unit)] if unit else maximum_value
            minimum_step = 1.0 if is_material_selector else (maximum - minimum) / 100
            maximum_step = 1.0 if is_material_selector else (maximum - minimum) / 10
            _require_true(
                self.app.activate_variable_optimization(
                    variable,
                    minimum=minimum_value,
                    maximum=maximum_value,
                ),
                f"activate Optimetrics variable {variable!r}",
            )
            setup.props["Variables"][variable] = [
                "i:=",
                True,
                "int:=",
                is_material_selector,
                "Min:=",
                minimum_value,
                "Max:=",
                maximum_value,
                "MinStep:=",
                format_aedt_value_from_si(minimum_step, unit),
                "MaxStep:=",
                format_aedt_value_from_si(maximum_step, unit),
                "MinFocus:=",
                minimum_value,
                "MaxFocus:=",
                maximum_value,
                "UseManufacturableValues:=",
                "false",
                "Level:=",
                (
                    f"[{minimum:g}, {maximum:g}]"
                    if is_material_selector
                    else f"[{minimum_number}: {maximum_number}]" + (f" {unit}" if unit else "")
                ),
            ]
            starting_point = evaluated[variable]
            if is_material_selector:
                starting_point = int(starting_point >= 0.5)
            elif not minimum <= starting_point <= maximum:
                starting_point = (minimum + maximum) / 2
            setup.props["StartingPoint"][variable] = format_aedt_value_from_si(starting_point, unit)
        setup.auto_update = True
        _require_true(setup.update(), f"write all variables to Optimetrics setup {name!r}")
        if material_weight_optimization:
            _require_true(
                setup.add_goal(
                    calculation=DEVICE_WEIGHT_VARIABLE,
                    ranges={"Time": None},
                    solution=active_solution,
                    condition="Minimize",
                    goal_value=0,
                ),
                f"add device-weight goal to Optimetrics setup {name!r}",
            )
        return setup, bounds

    def analyze(
        self,
        setup: str | None = None,
        *,
        cores: int = 4,
        tasks: int = 1,
        gpus: int = 0,
        **kwargs: Any,
    ) -> Any:
        """Run the selected setup or all nominal setups."""

        if setup:
            return self.app.analyze_setup(
                setup, cores=cores, tasks=tasks, gpus=gpus, **kwargs
            )
        return self.app.analyze(cores=cores, tasks=tasks, gpus=gpus, **kwargs)

    def save(self, path: str | Path | None = None) -> None:
        """Save the active project, optionally under a new path."""

        if path is None:
            result = self.app.save_project()
        else:
            result = self.app.save_project(str(path))
        if result is False:
            raise Maxwell2DToolkitError("AEDT reported that saving the project failed.")


def build_from_spec(app: Any, spec: Mapping[str, Any]) -> BuildResult:
    """Build a parameterized Maxwell 2D model from a JSON-friendly mapping.

    Supported top-level keys are ``units``, ``model_depth``, ``variables``,
    ``geometry``, ``operations``, ``region``, ``windings``, and ``setups``.
    See ``examples/model_spec.json`` for a complete example.
    """

    tk = Maxwell2DToolkit(app)
    result = BuildResult()

    if "units" in spec:
        tk.set_model_units(str(spec["units"]))
    if "model_depth" in spec:
        tk.set_model_depth(spec["model_depth"])
    variables = spec.get("variables", {})
    if variables:
        if not isinstance(variables, Mapping):
            raise TypeError("`variables` must be a mapping.")
        tk.set_variables(variables)

    for item in _mapping_sequence(spec.get("geometry", []), "geometry"):
        kind = str(item.get("kind", "")).lower()
        name = _required_string(item, "name")
        common = {
            "name": name,
            "material": item.get("material"),
            "non_model": bool(item.get("non_model", False)),
        }
        if kind == "rectangle":
            obj = tk.rectangle(
                item["origin"],
                item["sizes"],
                is_covered=bool(item.get("is_covered", True)),
                **common,
            )
        elif kind == "circle":
            obj = tk.circle(
                item["origin"],
                item["radius"],
                num_sides=int(item.get("num_sides", 0)),
                is_covered=bool(item.get("is_covered", True)),
                **common,
            )
        elif kind == "ellipse":
            obj = tk.ellipse(
                item["origin"],
                item["major_radius"],
                item["ratio"],
                is_covered=bool(item.get("is_covered", True)),
                **common,
            )
        elif kind == "polyline":
            obj = tk.polyline(
                item["points"],
                close_surface=bool(item.get("close_surface", False)),
                cover_surface=bool(item.get("cover_surface", False)),
                **common,
            )
        elif kind == "ring":
            obj = tk.ring(
                item["center"],
                item["outer_radius"],
                item["inner_radius"],
                name=name,
                material=item.get("material"),
                keep_inner=bool(item.get("keep_inner", False)),
            )
        elif kind == "rectangular_frame":
            obj = tk.rectangular_frame(
                item["outer_origin"],
                item["outer_sizes"],
                item["inner_origin"],
                item["inner_sizes"],
                name=name,
                material=item.get("material"),
                keep_inner=bool(item.get("keep_inner", False)),
            )
        else:
            raise ValueError(f"Unsupported geometry kind {kind!r} for {name!r}.")
        result.objects[name] = obj

    for item in _mapping_sequence(spec.get("operations", []), "operations"):
        operation = str(item.get("operation", "")).lower()
        if operation == "unite":
            tk.unite(
                item["assignment"],
                purge=bool(item.get("purge", False)),
                keep_originals=bool(item.get("keep_originals", False)),
            )
        elif operation == "subtract":
            tk.subtract(
                item["blank"],
                item["tools"],
                keep_originals=bool(item.get("keep_originals", False)),
            )
        elif operation == "move":
            tk.move(item["assignment"], item["vector"])
        elif operation == "rotate":
            tk.rotate(
                item["assignment"],
                axis=str(item.get("axis", "Z")),
                angle=item.get("angle", 90),
                units=str(item.get("units", "deg")),
            )
        elif operation == "mirror":
            tk.mirror(
                item["assignment"],
                origin=item["origin"],
                vector=item["vector"],
                duplicate=bool(item.get("duplicate", False)),
                duplicate_assignment=bool(item.get("duplicate_assignment", True)),
            )
        elif operation == "assign_material":
            tk.assign_material(item["assignment"], str(item["material"]))
        else:
            raise ValueError(f"Unsupported operation {operation!r}.")

    region_spec = spec.get("region")
    if region_spec:
        if not isinstance(region_spec, Mapping):
            raise TypeError("`region` must be a mapping.")
        result.region = tk.create_region(
            pad_value=region_spec.get("pad_value", 100),
            pad_type=str(region_spec.get("pad_type", "Percentage Offset")),
            name=str(region_spec.get("name", "Region")),
            assign_balloon=bool(region_spec.get("assign_balloon", True)),
            balloon_name=str(region_spec.get("balloon_name", "Balloon")),
            is_voltage=bool(region_spec.get("is_voltage", False)),
        )

    for item in _mapping_sequence(spec.get("windings", []), "windings"):
        name = _required_string(item, "name")
        mode = str(item.get("mode", "explicit")).lower()
        common_winding = {
            "name": name,
            "winding_type": str(item.get("winding_type", "Current")),
            "is_solid": bool(item.get("is_solid", False)),
            "current": item.get("current", 0),
            "resistance": item.get("resistance", 0),
            "inductance": item.get("inductance", 0),
            "voltage": item.get("voltage", 0),
            "parallel_branches": int(item.get("parallel_branches", 1)),
            "phase": item.get("phase", 0),
        }
        if mode == "direct":
            winding_result = tk.set_geometry_as_winding(
                item["assignment"], **common_winding
            )
        elif mode == "paired":
            winding_result = tk.create_paired_winding(
                item["positive_assignment"],
                item["negative_assignment"],
                turns=int(item.get("turns", 1)),
                positive_coil_name=item.get("positive_coil_name"),
                negative_coil_name=item.get("negative_coil_name"),
                **common_winding,
            )
        elif mode == "explicit":
            coil_specs = []
            for coil in _mapping_sequence(item.get("coils", []), f"windings.{name}.coils"):
                coil_specs.append(
                    CoilSpec(
                        assignment=coil["assignment"],
                        turns=int(coil.get("turns", 1)),
                        polarity=str(coil.get("polarity", "Positive")),
                        name=coil.get("name"),
                    )
                )
            winding_result = tk.create_winding_from_coils(
                coil_specs, **common_winding
            )
        else:
            raise ValueError(f"Unsupported winding mode {mode!r} for {name!r}.")
        result.windings[name] = winding_result

    for item in _mapping_sequence(spec.get("setups", []), "setups"):
        name = _required_string(item, "name")
        setup = tk.create_setup(
            name=name,
            setup_type=item.get("setup_type"),
            properties=item.get("properties"),
        )
        result.setups[name] = setup

    return result


def _name_of(value: Any) -> str:
    if isinstance(value, str):
        if not value:
            raise ValueError("Empty selection/boundary name is not valid.")
        return value
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    raise TypeError(
        f"Expected an AEDT object/boundary with a `.name` attribute or a string, "
        f"got {type(value).__name__}."
    )


def _names_of(value: Selection | Sequence[Selection]) -> list[str]:
    if isinstance(value, str) or hasattr(value, "name"):
        return [_name_of(value)]
    if isinstance(value, Iterable):
        return [_name_of(item) for item in value]
    raise TypeError(f"Unsupported AEDT selection type: {type(value).__name__}.")


def _normalize_polarity(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "positive":
        return "Positive"
    if normalized == "negative":
        return "Negative"
    raise ValueError("`polarity` must be either 'Positive' or 'Negative'.")


def _require_result(result: Any, operation: str) -> Any:
    if result is False or result is None:
        raise Maxwell2DToolkitError(f"PyAEDT failed to {operation}.")
    return result


def _require_true(result: Any, operation: str) -> None:
    if result is False or result is None:
        raise Maxwell2DToolkitError(f"PyAEDT failed to {operation}.")


def _mapping_sequence(value: Any, label: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"`{label}` must be a sequence of mappings.")
    output: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise TypeError(f"`{label}[{index}]` must be a mapping.")
        output.append(item)
    return output


def _required_string(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Required non-empty string field {key!r} is missing.")
    return value
