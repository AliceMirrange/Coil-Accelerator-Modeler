from pathlib import Path

import re

import pytest

from maxwell_circuit_toolkit.core import (
    AcceleratorCircuitOptions,
    TOPOLOGY_FILM_CAPACITOR_SCR,
    TOPOLOGY_ODD_EVEN_BOOST,
    TOPOLOGY_SINGLE_BOOST,
    available_topologies,
    build_accelerator_external_circuit,
    ensure_position_variables,
    mirror_external_circuit_project_variables,
    get_independent_project_variable_names,
    natural_winding_key,
    project_variables_referenced_by_netlist,
    set_global_variables_low_level,
    wire_component_pins,
)


class FakeProject:
    def __init__(self, variables=None):
        self.variables = dict(variables or {})
        self.change_calls = []
        self.get_calls = 0

    def GetVariables(self):
        self.get_calls += 1
        return list(self.variables)

    def ChangeProperty(self, args):
        self.change_calls.append(args)
        tab = args[1]
        for section in tab[2:]:
            if section[0] == "NAME:NewProps":
                for p in section[1:]:
                    self.variables[p[0][5:]] = p[-1]
            elif section[0] == "NAME:ChangedProps":
                for p in section[1:]:
                    self.variables[p[0][5:]] = p[-1]


class FakePin:
    def __init__(self, component, name):
        self._component = component
        self.name = name

    @property
    def location(self):
        # Allow tests to emulate a stale PyAEDT CircuitPins.location after
        # a native editor Rotate/Flip.  The editor's GetComponentPinLocation
        # still returns the authoritative post-transform coordinate.
        override = getattr(self._component, "public_pin_offsets_override", None)
        if override and self.name in override:
            dx, dy = override[self.name]
            return [self._component.location[0] + dx, self._component.location[1] + dy]
        x, y = self._component.pin_xy(self.name)
        return [x, y]


class FakeComponent:
    _next_id = 1

    def __init__(self, name, info=None, pin_offsets=None, parameters=None):
        self.name = name
        self.parameters = dict(parameters or {})
        if info is not None:
            self.parameters["Info"] = info
        self.pin_offsets = dict(pin_offsets or {})
        self._location = [0.0, 0.0]
        sid = FakeComponent._next_id
        FakeComponent._next_id += 1
        self.composed_name = f"CompInst@{name};{sid};{sid}"

    @property
    def location(self):
        return self._location

    @location.setter
    def location(self, value):
        self._location = [float(value[0]), float(value[1])]

    @property
    def pins(self):
        return [FakePin(self, name) for name in self.pin_offsets]

    def pin_xy(self, pin):
        dx, dy = self.pin_offsets[pin]
        return self._location[0] + dx, self._location[1] + dy


class FakeEditor:
    def __init__(self):
        self.components = {}
        self.rotate_calls = []
        self.flip_h_calls = []
        self.wires = []
        self.net_by_endpoint = {}
        self._next_net = 1

    def register(self, comp):
        self.components[comp.composed_name] = comp

    def GetComponentPins(self, comp_id):
        # Real AEDT GPort objects are schematic ports rather than ordinary
        # components and return no native pins. PyAEDT synthesizes one pin in
        # CircuitComponent.pins for Port@ objects.
        if "GPort@" in comp_id:
            return []
        return list(self.components[comp_id].pin_offsets)

    def GetComponentPinLocation(self, comp_id, pin, x_or_y):
        # Native Schematic Editor API returns meters.  The fake component
        # locations are expressed in the builder's schematic unit (mil).
        x, y = self.components[comp_id].pin_xy(pin)
        value_m = (x if x_or_y else y) * 0.0000254
        return value_m

    def _selected_components(self, selections):
        ids = selections[selections.index("Selections:=") + 1]
        return [self.components[i] for i in ids]

    def Rotate(self, selections, params):
        self.rotate_calls.append((selections, params))
        degrees = params[params.index("Degrees:=") + 1]
        assert degrees == 90
        for comp in self._selected_components(selections):
            comp.pin_offsets = {name: (-dy, dx) for name, (dx, dy) in comp.pin_offsets.items()}

    def FlipHorizontal(self, selections, params):
        self.flip_h_calls.append((selections, params))
        for comp in self._selected_components(selections):
            comp.pin_offsets = {name: (dx, -dy) for name, (dx, dy) in comp.pin_offsets.items()}

    def endpoint_at(self, point):
        px, py = float(point[0]), float(point[1])
        found = []
        for comp_id, comp in self.components.items():
            for pin in comp.pin_offsets:
                x, y = comp.pin_xy(pin)
                if abs(x - px) < 1e-9 and abs(y - py) < 1e-9:
                    found.append((comp_id, str(pin)))
        return found

    def join_endpoints(self, endpoints):
        endpoints = list(dict.fromkeys(endpoints))
        existing = [self.net_by_endpoint[e] for e in endpoints if e in self.net_by_endpoint]
        if existing:
            net = existing[0]
            for other in set(existing[1:]):
                if other != net:
                    for ep, nid in list(self.net_by_endpoint.items()):
                        if nid == other:
                            self.net_by_endpoint[ep] = net
        else:
            net = f"net_{self._next_net}"
            self._next_net += 1
        for ep in endpoints:
            self.net_by_endpoint[ep] = net
        return net

    def GetComponentPinInfo(self, comp_id, pin):
        net = self.net_by_endpoint.get((comp_id, str(pin)), "")
        return [f"WireId={net}"]


class FakeDesign:
    def __init__(self, editor):
        self.editor = editor

    def SetActiveEditor(self, name):
        assert name == "SchematicEditor"
        return self.editor


class FakeSchematic:
    def __init__(self, windings, editor):
        self.components = {}
        self.created = []
        self.grounds = []
        self.editor = editor
        for w in windings:
            self._store("Dedicated Elements", "Winding", w, [0, 0])

    def _apply_angle(self, c, angle):
        steps = int(round(float(angle) / 90.0)) % 4
        for _ in range(steps):
            c.pin_offsets = {name: (-dy, dx) for name, (dx, dy) in c.pin_offsets.items()}

    def _store(self, lib, kind, c, location=None, angle=0):
        self._apply_angle(c, angle)
        c.location = list(location or [0, 0])
        self.created.append((lib, kind, c))
        self.components[len(self.components) + 1] = c
        self.editor.register(c)
        return c

    def create_component(self, name=None, component_library=None, component_name=None, location=None, angle=0):
        if component_name == "SW_VModel":
            c = FakeComponent(name, parameters={"Ron": "", "Roff": "", "Von": "", "Voff": ""})
        elif component_name == "SW_V4":
            # SW_V4 uses the fixed native identities established by the
            # user's four-distinct-net experiment.
            c = FakeComponent(
                name,
                # User-validated transform is three +90deg CCW rotations, no flip.
                # Electrical identities remain fixed regardless of screen position:
                # n1=main_1, n2=main_2, n3=control+, n4=control-.
                pin_offsets={"n1": (-1, 0), "n2": (1, 0), "n3": (-0.5, -1), "n4": (0.5, -1)},
                parameters={"MOD": ""},
            )
        elif component_name == "VPulse":
            # Real AEDT instance observed by the user: +90deg showed positive
            # on the left. The builder therefore uses -90deg, yielding the
            # validated reference orientation: negative left, positive right.
            c = FakeComponent(
                name,
                pin_offsets={"n1": (0, 1), "n2": (0, -1)},
                parameters={
                    "Type": "TIME", "V1": "", "V2": "", "Td": "", "Tr": "",
                    "Tf": "", "Pw": "", "Period": "",
                },
            )
        elif component_name == "VPWL":
            c = FakeComponent(
                name,
                pin_offsets={"n1": (0, 1), "n2": (0, -1)},
                parameters={
                    "Type": "TIME",
                    **{key: "" for index in range(1, 21) for key in (f"T{index}", f"V{index}")},
                },
            )
        elif component_name == "Res":
            c = FakeComponent(name, pin_offsets={"n1": (-1, 0), "n2": (1, 0)}, parameters={"R": ""})
        elif component_name == "Cap":
            c = FakeComponent(name, pin_offsets={"n1": (-1, 0), "n2": (1, 0)}, parameters={"C": "10pF", "IC": "0V"})
        elif component_name == "DIODE_Model":
            c = FakeComponent(
                name,
                parameters={
                    "IS": "default", "RS": "default", "N": "default", "EG": "default",
                    "XTI": "default", "BV": "default", "IBV": "default", "TNOM": "default",
                },
            )
        elif component_name == "DIODE":
            c = FakeComponent(name, pin_offsets={"n1": (-1, 0), "n2": (1, 0)}, parameters={"MOD": "required"})
        else:
            raise AssertionError(component_name)
        return self._store(component_library, component_name, c, location, angle)

    def create_resistor(self, name=None, value=50, location=None, angle=0, use_instance_id_netlist=False):
        c = FakeComponent(name, pin_offsets={"n1": (-1, 0), "n2": (1, 0)}, parameters={"R": str(value)})
        return self._store("Passive Elements", "Res", c, location, angle)

    def create_diode(self, name=None, location=None, angle=0, use_instance_id_netlist=False):
        c = FakeComponent(name, pin_offsets={"n1": (-1, 0), "n2": (1, 0)}, parameters={"MOD": "required"})
        return self._store("Passive Elements", "DIODE", c, location, angle)

    def create_gnd(self, location=None, angle=0, page=1):
        # Mirror real PyAEDT/AEDT behavior: the editor ID is a GPort and
        # GetComponentPins returns [], while CircuitComponent.pins exposes one
        # synthetic pin. The synthetic connection point is above the symbol.
        c = FakeComponent(f"GND_{len(self.grounds) + 1}", pin_offsets={})
        sid = c.composed_name.split(";")[-1]
        c.composed_name = f"GPort@0;{sid}"
        c.pin_offsets = {c.composed_name: (0, 1)}
        self.grounds.append(c)
        return self._store("Draw", "GND", c, location, angle)

    def create_wire(self, points, name="", page=1):
        pts = [[float(p[0]), float(p[1])] for p in points]
        self.editor.wires.append(pts)

        def on_segment(px, py, a, b, tol=1e-9):
            ax, ay = a
            bx, by = b
            cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
            if abs(cross) > tol:
                return False
            return (min(ax, bx) - tol <= px <= max(ax, bx) + tol and
                    min(ay, by) - tol <= py <= max(ay, by) + tol)

        touched = []
        for comp_id, comp in self.editor.components.items():
            for pin in comp.pin_offsets:
                px, py = comp.pin_xy(pin)
                if any(on_segment(px, py, pts[j], pts[j + 1]) for j in range(len(pts) - 1)):
                    touched.append((comp_id, str(pin)))
        touched = list(dict.fromkeys(touched))
        if len(touched) < 2:
            return False
        self.editor.join_endpoints(touched)
        return object()


class FakeModeler:
    def __init__(self, windings, editor):
        self.schematic = FakeSchematic(windings, editor)
        self.schematic_units = None


class FakeCircuit:
    def __init__(self, windings):
        self.editor = FakeEditor()
        self._odesign = FakeDesign(self.editor)
        self.modeler = FakeModeler(windings, self.editor)
        self.exports = []

    def export_netlist_from_schematic(self, output_file):
        self.exports.append(output_file)
        Path(output_file).write_text("* fake sph\n", encoding="utf-8")
        return True


class FakeVariableManager:
    def __init__(self, project):
        self.project = project

    @property
    def variables(self):
        result = {}
        for name, expression in self.project.variables.items():
            match = re.fullmatch(
                r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z_]+)?\s*",
                str(expression),
            )
            value = float(match.group(1)) if match else 0.0
            units = match.group(2) if match and match.group(2) else ""
            result[name] = type("FakeAedtVariable", (), {"units": units, "numeric_value": value})()
        return result

    @property
    def independent_project_variable_names(self):
        # Geometry-derived coil resistances are deliberately dependent in the
        # real accelerator project; all other fake project variables here are
        # treated as independent constants.
        return [
            name for name in self.project.variables
            if not str(name).startswith("$coil_R_")
        ]


class FakeMaxwell:
    def __init__(self, winding_names=("Winding_3", "Winding_1", "Winding_2")):
        variables = {
            "$z_start_1": "0",
            "$z_start_2": "0.02",
            "$z_start_3": "0.04",
            "$coil_Len_1": "0.01",
            "$coil_Len_2": "0.011",
            "$coil_Len_3": "0.012",
        }
        for name in winding_names:
            import re as _re
            m = _re.search(r"(\d+)$", name)
            if m:
                variables[f"$coil_R_{int(m.group(1))}"] = "0.1"
        self._oproject = FakeProject(variables)
        self.variable_manager = FakeVariableManager(self._oproject)
        self.design_list = ["LinearAccelerator2D"]
        self.project_file = "/tmp/test.aedt"
        self.deleted = []
        self.edits = []
        self.saved = 0
        self.circuit = None
        self.winding_names = tuple(winding_names)

    def create_external_circuit(self, circuit_design):
        windings = [
            FakeComponent(name, info="Winding", pin_offsets={"n1": (-1, 0), "n2": (1, 0)})
            for name in self.winding_names
        ]
        self.circuit = FakeCircuit(windings)
        return self.circuit

    def delete_design(self, name):
        self.deleted.append(name)

    def edit_external_circuit(
        self, netlist_file_path, schematic_design_name=None, parameters=None
    ):
        self.edits.append((netlist_file_path, schematic_design_name, parameters))
        return True

    def save_project(self, *args):
        self.saved += 1
        return True


def _wire_exists(editor, a, apin, b, bpin):
    na = editor.net_by_endpoint.get((a.composed_name, apin))
    nb = editor.net_by_endpoint.get((b.composed_name, bpin))
    return bool(na and nb and na == nb)


def test_natural_winding_sort():
    names = ["Winding_10", "Winding_2", "Winding_1"]
    assert sorted(names, key=natural_winding_key) == ["Winding_1", "Winding_2", "Winding_10"]


def test_wire_uses_native_editor_pin_location_not_stale_public_pin_location():
    editor = FakeEditor()
    a = FakeComponent("A", pin_offsets={"P": (1.0, 0.0)})
    b = FakeComponent("B", pin_offsets={"P": (-1.0, 0.0)})
    a.location = [0.0, 0.0]
    b.location = [5.0, 0.0]
    # Simulate stale CircuitPins.location values that would point both wires
    # at the wrong places after a transformed symbol.
    a.public_pin_offsets_override = {"P": (100.0, 100.0)}
    b.public_pin_offsets_override = {"P": (-100.0, -100.0)}
    editor.register(a)
    editor.register(b)

    class _S:
        def __init__(self, ed):
            self.editor = ed
        def create_wire(self, points, name="", page=1):
            pts = [[float(p[0]), float(p[1])] for p in points]
            self.editor.wires.append(pts)
            return object()

    class _M:
        def __init__(self, ed):
            self.schematic = _S(ed)
            self.schematic_units = "mil"

    class _C:
        def __init__(self, ed):
            self._odesign = FakeDesign(ed)
            self.modeler = _M(ed)

    c = _C(editor)
    wire_component_pins(c, a, "P", b, "P", route="straight")
    assert editor.wires[-1] == [[1.0, 0.0], [4.0, 0.0]]


def test_topology_registry_contains_all_supported_circuits():
    pairs = dict(available_topologies())
    assert pairs == {
        TOPOLOGY_SINGLE_BOOST: "单路Boost",
        TOPOLOGY_ODD_EVEN_BOOST: "奇偶Boost",
        TOPOLOGY_FILM_CAPACITOR_SCR: "薄膜电容+模拟SCR",
    }


def test_realistic_gport_has_synthetic_pin_even_when_native_pin_list_is_empty():
    editor = FakeEditor()
    schematic = FakeSchematic([], editor)
    gnd = schematic.create_gnd(location=[10, 20])
    assert editor.GetComponentPins(gnd.composed_name) == []
    assert len(gnd.pins) == 1
    assert gnd.pins[0].name == gnd.composed_name
    assert gnd.pins[0].location == [10.0, 21.0]


def test_batch_project_variables_one_changeproperty():
    app = type("App", (), {})()
    app._oproject = FakeProject({"$A": "1"})
    set_global_variables_low_level(app, {"$A": "2", "$B": "3"})
    assert len(app._oproject.change_calls) == 1
    assert app._oproject.variables["$A"] == "2"
    assert app._oproject.variables["$B"] == "3"


def test_ensure_position_variables_preserves_existing_and_uses_independent_defaults():
    app = type("App", (), {})()
    app._oproject = FakeProject({
        "$POSon_1": "5mm",
        "$z_start_1": "1mm",
        "$z_start_2": "2mm",
        "$coil_Len_1": "3mm",
        "$coil_Len_2": "4mm",
    })
    made = ensure_position_variables(app, 2)
    assert "$POSon_1" not in made
    assert made["$POSdur_1"] == "10mm"
    assert made["$POSon_2"] == "0mm"
    assert made["$POSdur_2"] == "10mm"
    assert app._oproject.variables["$POSon_1"] == "5mm"


def test_single_boost_uses_native_named_pins_and_exact_manual_switch_transform(tmp_path: Path):
    app = FakeMaxwell()
    result = build_accelerator_external_circuit(
        app,
        options=AcceleratorCircuitOptions(topology=TOPOLOGY_SINGLE_BOOST),
        netlist_path=tmp_path / "boost.sph",
    )
    editor = result.circuit.editor
    assert [x.name for x in result.windings] == ["Winding_1", "Winding_2", "Winding_3"]
    assert len(result.switches) == 3
    assert len(result.pulses) == 3
    assert len(result.vpwl_sources) == 1
    assert len(result.vpulse_sources) == 2
    assert result.vpwl_sources[0] is result.pulses[0]
    assert [
        (library, kind)
        for library, kind, component in result.circuit.modeler.schematic.created
        if component is result.vpwl_sources[0]
    ] == [("Sources", "VPWL")]
    assert len(result.diodes) == 3
    assert len(result.resistors) == 3
    assert len(result.pulse_shunt_resistors) == 3
    assert len(result.storage_capacitors) == 3
    assert len(result.storage_esr_resistors) == 3
    assert len(result.return_resistors) == 1
    assert result.last_resistor is not None
    assert len(result.grounds) == 12  # pulse-, control-, main, and storage-ESR ground per stage
    assert result.switch_model.parameters["DeviceName"] == "SW_Model_Common"
    assert result.switch_model.parameters["Von"] == "0.9*$V"
    assert result.switch_model.parameters["Voff"] == "0.1*$V"
    assert result.diode_model.name == "DIODE_Model_Common"
    assert result.diode_model.parameters["DeviceName"] == "DIODE_Model_Common"

    # Every SW_V4 gets three +90deg rotations; each 10k shunt, storage
    # capacitor, and storage ESR gets one. VPULSE remains at 0deg.
    assert len(editor.rotate_calls) == 18
    assert len(editor.flip_h_calls) == 0
    for _, params in editor.rotate_calls:
        assert "Degrees:=" in params and params[params.index("Degrees:=") + 1] == 90

    # Three rotations change only the visual orientation. Native pin identities
    # stay fixed: n1=main_1, n2=main_2, n3=control+, n4=control-.
    for switch in result.switches:
        assert switch.pin_xy("n1")[1] > switch.pin_xy("n2")[1]
        assert switch.pin_xy("n3")[1] > switch.pin_xy("n4")[1]
        assert switch.pin_xy("n3")[0] < switch.pin_xy("n1")[0]
        assert switch.pin_xy("n4")[0] < switch.pin_xy("n2")[0]

    for i, (winding, resistor, switch, pulse, diode) in enumerate(
        zip(result.windings, result.resistors, result.switches, result.pulses, result.diodes), start=1
    ):
        assert switch.parameters["MOD"] == "SW_Model_Common"
        assert diode.parameters["MOD"] == "DIODE_Model_Common"
        assert pulse.parameters["Type"] == "POS"
        if i == 1:
            assert pulse.name == "VPWL_1"
            assert [
                (pulse.parameters[f"T{point}"], pulse.parameters[f"V{point}"])
                for point in range(1, 5)
            ] == [
                ("$POSon_1", "1.25*$V"),
                ("$POSdur_1", "1.25*$V"),
                ("$POSdur_1+0.01mm", "0V"),
                ("1000000000mm", "0V"),
            ]
        else:
            assert pulse.name == f"VPULSE_{i}"
            assert pulse.parameters["V1"] == "0V"
            assert pulse.parameters["V2"] == "1.25*$V"
            assert pulse.parameters["Tr"] == "0"
            assert pulse.parameters["Tf"] == "0"
            assert pulse.parameters["Td"] == f"$POSon_{i}-$proj_initZ"
            assert pulse.parameters["Pw"] == f"$POSdur_{i}"

        assert resistor.parameters["R"] == f"$coil_R_{i}"
        shunt = result.pulse_shunt_resistors[i - 1]
        assert shunt.parameters["R"] == "10000ohm"
        assert _wire_exists(editor, pulse, "n2", shunt, "n1")
        assert _wire_exists(editor, pulse, "n1", shunt, "n2")

        storage_cap = result.storage_capacitors[i - 1]
        storage_esr = result.storage_esr_resistors[i - 1]
        assert storage_cap.parameters["C"] == f"$C_{i}"
        assert storage_cap.parameters["IC"] == "$V"
        assert storage_esr.parameters["R"] == f"$ESR_{i}"
        assert app._oproject.variables[f"$C_{i}"] == "220uF"
        assert app._oproject.variables[f"$ESR_{i}"] == "400mOhm"
        assert app._oproject.variables["$V"] == "390V"
        # Both required storage elements are rotated once: n2 upper, n1 lower.
        assert storage_cap.pin_xy("n2")[1] > storage_cap.pin_xy("n1")[1]
        assert storage_esr.pin_xy("n2")[1] > storage_esr.pin_xy("n1")[1]
        assert _wire_exists(editor, winding, "n1", storage_cap, "n2")
        assert _wire_exists(editor, storage_cap, "n1", storage_esr, "n2")

        # Exact fixed-pin topology: Winding -> Rcoil -> switching node; source
        # negative is grounded and source positive goes ONLY to SW n3/control+.
        assert _wire_exists(editor, winding, "n2", resistor, "n1")
        assert _wire_exists(editor, resistor, "n2", diode, "n1")
        assert _wire_exists(editor, resistor, "n2", switch, "n1")
        stage_grounds = result.grounds[(i - 1) * 4:i * 4]
        pulse_gnd, ctrl_gnd, main_gnd, storage_gnd = stage_grounds
        assert _wire_exists(editor, switch, "n2", main_gnd, main_gnd.pins[0].name)
        assert _wire_exists(editor, switch, "n4", ctrl_gnd, ctrl_gnd.pins[0].name)
        assert _wire_exists(editor, pulse, "n2", pulse_gnd, pulse_gnd.pins[0].name)
        assert _wire_exists(editor, pulse, "n1", switch, "n3")
        assert _wire_exists(editor, storage_esr, "n1", storage_gnd, storage_gnd.pins[0].name)
        assert pulse.pin_xy("n1")[1] > pulse.pin_xy("n2")[1]
        assert abs(pulse.pin_xy("n1")[0] - pulse.pin_xy("n2")[0]) < 1e-9
        # The 10 kOhm shunt is rotated exactly once and is vertical beside VPULSE.
        assert shunt.pin_xy("n2")[1] > shunt.pin_xy("n1")[1]
        assert abs(shunt.pin_xy("n1")[0] - shunt.pin_xy("n2")[0]) < 1e-9
        minus_net = editor.net_by_endpoint[(pulse.composed_name, "n2")]
        plus_net = editor.net_by_endpoint[(pulse.composed_name, "n1")]
        assert minus_net != plus_net

        # Reference layout: rail above switch; R and diode on rail; pulse left of switch.
        assert winding.location[1] > switch.location[1]
        assert resistor.location[1] == winding.location[1]
        assert diode.location[1] == winding.location[1]
        assert winding.location[0] < resistor.location[0] < diode.location[0]
        assert pulse.location[0] < switch.location[0]

    # DIODE_n cathode -> Winding_(n+1) left. The final cathode returns through
    # the dedicated $Rlast resistor to the final Winding's left terminal.
    assert _wire_exists(editor, result.diodes[0], "n2", result.windings[1], "n1")
    assert _wire_exists(editor, result.diodes[1], "n2", result.windings[2], "n1")
    assert result.last_resistor.name == "R_last"
    assert result.last_resistor.parameters["R"] == "$Rlast"
    assert app._oproject.variables["$Rlast"] == "1ohm"
    assert _wire_exists(editor, result.diodes[2], "n2", result.last_resistor, "n1")
    assert _wire_exists(editor, result.last_resistor, "n2", result.windings[2], "n1")
    assert (
        editor.net_by_endpoint[(result.last_resistor.composed_name, "n1")]
        != editor.net_by_endpoint[(result.last_resistor.composed_name, "n2")]
    )
    assert app.edits[-1][1] == "AcceleratorExternalCircuit"
    assert result.netlist_path.suffix == ".sph"
    assert app._oproject.variables["$POSon_1"] == "-1000mm"
    assert app._oproject.variables["$proj_initZ"] == "0mm"


def test_thin_film_capacitor_scr_builds_isolated_latched_stages(tmp_path: Path):
    app = FakeMaxwell()
    for name in list(app._oproject.variables):
        if name.startswith("$coil_R_"):
            del app._oproject.variables[name]
    result = build_accelerator_external_circuit(
        app,
        options=AcceleratorCircuitOptions(topology=TOPOLOGY_FILM_CAPACITOR_SCR),
        netlist_path=tmp_path / "film_scr.sph",
    )
    editor = result.circuit.editor

    assert result.topology == TOPOLOGY_FILM_CAPACITOR_SCR
    assert len(result.switches) == len(result.pulses) == 3
    assert result.vpwl_sources == []
    assert result.vpulse_sources == result.pulses
    assert len(result.diodes) == len(result.storage_capacitors) == 3
    assert result.resistors == []
    assert result.pulse_shunt_resistors == []
    assert result.storage_esr_resistors == []
    assert result.return_resistors == []
    assert result.last_resistor is None
    assert len(result.grounds) == 6
    assert app._oproject.variables["$proj_initZ"] == "0mm"
    assert "$ESR_1" not in app._oproject.variables
    assert "$Rlast" not in app._oproject.variables

    stage_net_sets = []
    for i, (winding, capacitor, diode, switch, pulse) in enumerate(
        zip(
            result.windings,
            result.storage_capacitors,
            result.diodes,
            result.switches,
            result.pulses,
        ),
        start=1,
    ):
        assert capacitor.name == f"C_film_{i}"
        assert capacitor.parameters["C"] == f"$C_{i}"
        assert capacitor.parameters["IC"] == "$V"
        assert diode.parameters["MOD"] == "DIODE_Model_Common"
        assert switch.parameters["MOD"] == "SW_Model_Common"
        assert pulse.name == f"VPULSE_{i}"
        assert pulse.parameters["Type"] == "POS"
        assert pulse.parameters["Td"] == (
            "$POSon_1"
            if i == 1
            else f"$POSon_{i}-$proj_initZ"
        )
        assert pulse.parameters["Pw"] == "1000000000mm"

        assert capacitor.pin_xy("n2")[1] > capacitor.pin_xy("n1")[1]
        assert diode.pin_xy("n1")[1] > diode.pin_xy("n2")[1]
        assert switch.pin_xy("n1")[1] > switch.pin_xy("n2")[1]
        assert _wire_exists(editor, capacitor, "n2", winding, "n1")
        assert _wire_exists(editor, winding, "n2", diode, "n1")
        assert _wire_exists(editor, diode, "n2", switch, "n1")
        assert _wire_exists(editor, switch, "n2", capacitor, "n1")
        assert _wire_exists(editor, pulse, "n1", switch, "n3")
        assert _wire_exists(editor, pulse, "n2", switch, "n4")
        for component, pin_a, pin_b in (
            (capacitor, "n1", "n2"),
            (diode, "n1", "n2"),
            (switch, "n1", "n2"),
            (pulse, "n1", "n2"),
        ):
            assert (
                editor.net_by_endpoint[(component.composed_name, pin_a)]
                != editor.net_by_endpoint[(component.composed_name, pin_b)]
            )

        main_ground, control_ground = result.grounds[(i - 1) * 2:i * 2]
        assert _wire_exists(
            editor, capacitor, "n1", main_ground, main_ground.pins[0].name
        )
        assert _wire_exists(
            editor, pulse, "n2", control_ground, control_ground.pins[0].name
        )
        endpoints = [
            (component.composed_name, pin)
            for component in (
                winding, capacitor, diode, switch, pulse, main_ground, control_ground
            )
            for pin in component.pin_offsets
        ]
        stage_net_sets.append(
            {editor.net_by_endpoint[endpoint] for endpoint in endpoints}
        )

    for i, nets in enumerate(stage_net_sets):
        for other in stage_net_sets[i + 1:]:
            assert nets.isdisjoint(other)


def test_existing_projectile_initial_z_variable_is_preserved(tmp_path: Path):
    app = FakeMaxwell()
    app._oproject.variables["$proj_initZ"] = "0.025"
    result = build_accelerator_external_circuit(
        app,
        options=AcceleratorCircuitOptions(topology=TOPOLOGY_FILM_CAPACITOR_SCR),
        netlist_path=tmp_path / "existing_projectile_offset.sph",
    )
    assert app._oproject.variables["$proj_initZ"] == "0.025"
    assert "$proj_initZ" not in result.created_variables
    assert result.pulses[1].parameters["Td"] == "$POSon_2-$proj_initZ"


def test_odd_even_boost_splits_cascades_and_keeps_last_resistor_loop(tmp_path: Path):
    app = FakeMaxwell(
        ("Winding_6", "Winding_2", "Winding_5", "Winding_1", "Winding_4", "Winding_3")
    )
    result = build_accelerator_external_circuit(
        app,
        options=AcceleratorCircuitOptions(topology=TOPOLOGY_ODD_EVEN_BOOST),
        netlist_path=tmp_path / "odd_even.sph",
    )
    editor = result.circuit.editor

    assert result.topology == TOPOLOGY_ODD_EVEN_BOOST
    assert [item.name for item in result.windings] == [
        "Winding_1", "Winding_2", "Winding_3",
        "Winding_4", "Winding_5", "Winding_6",
    ]

    # Odd chain: 1 -> 3 -> 5. Even chain: 2 -> 4 -> 6. The target is the
    # fixed positive capacitor pin n2, which already shares Winding_i.n1.
    for source_i, target_i in ((1, 3), (2, 4), (3, 5), (4, 6)):
        diode = result.diodes[source_i - 1]
        capacitor = result.storage_capacitors[target_i - 1]
        winding = result.windings[target_i - 1]
        assert _wire_exists(editor, diode, "n2", capacitor, "n2")
        assert _wire_exists(editor, diode, "n2", winding, "n1")

    # Odd and even stages are independent schematic rows, and same-parity
    # stages advance horizontally within their own row.
    odd_y = {result.windings[i - 1].location[1] for i in (1, 3, 5)}
    even_y = {result.windings[i - 1].location[1] for i in (2, 4, 6)}
    assert len(odd_y) == len(even_y) == 1
    assert next(iter(odd_y)) > next(iter(even_y))
    assert result.windings[0].location[0] == result.windings[1].location[0]
    assert result.windings[2].location[0] > result.windings[0].location[0]

    # Both parity-terminal stages close through separate resistor instances,
    # both of which reference the shared $Rlast value. Each returns to the
    # positive pin of its own terminal-stage storage capacitor.
    assert [item.name for item in result.return_resistors] == [
        "R_last_odd", "R_last_even"
    ]
    odd_return, even_return = result.return_resistors
    assert odd_return.parameters["R"] == "$Rlast"
    assert even_return.parameters["R"] == "$Rlast"
    assert _wire_exists(editor, result.diodes[4], "n2", odd_return, "n1")
    assert _wire_exists(editor, odd_return, "n2", result.storage_capacitors[4], "n2")
    assert _wire_exists(editor, result.diodes[5], "n2", even_return, "n1")
    assert _wire_exists(editor, even_return, "n2", result.storage_capacitors[5], "n2")
    for resistor in result.return_resistors:
        assert (
            editor.net_by_endpoint[(resistor.composed_name, "n1")]
            != editor.net_by_endpoint[(resistor.composed_name, "n2")]
        )

    # Adjacent opposite-parity stages must not be cascaded together.
    assert not _wire_exists(editor, result.diodes[0], "n2", result.windings[1], "n1")
    assert not _wire_exists(editor, result.diodes[1], "n2", result.windings[2], "n1")


def test_odd_even_boost_requires_at_least_one_stage_in_each_parity_chain(tmp_path: Path):
    app = FakeMaxwell(("Winding_1",))
    with pytest.raises(Exception, match="至少需要 2"):
        build_accelerator_external_circuit(
            app,
            options=AcceleratorCircuitOptions(topology=TOPOLOGY_ODD_EVEN_BOOST),
            netlist_path=tmp_path / "too_short_odd_even.sph",
        )


def test_two_stage_odd_even_boost_returns_each_diode_to_its_own_capacitor(tmp_path: Path):
    app = FakeMaxwell(("Winding_2", "Winding_1"))
    result = build_accelerator_external_circuit(
        app,
        options=AcceleratorCircuitOptions(topology=TOPOLOGY_ODD_EVEN_BOOST),
        netlist_path=tmp_path / "two_stage_odd_even.sph",
    )
    editor = result.circuit.editor
    assert [item.name for item in result.return_resistors] == [
        "R_last_odd", "R_last_even"
    ]
    for index, resistor in enumerate(result.return_resistors):
        assert _wire_exists(editor, result.diodes[index], "n2", resistor, "n1")
        assert _wire_exists(
            editor, resistor, "n2", result.storage_capacitors[index], "n2"
        )


def test_storage_capacitor_positive_side_is_on_winding_input_and_previous_diode_node(tmp_path: Path):
    app = FakeMaxwell()
    result = build_accelerator_external_circuit(
        app,
        options=AcceleratorCircuitOptions(topology=TOPOLOGY_SINGLE_BOOST),
        netlist_path=tmp_path / "storage.sph",
    )
    editor = result.circuit.editor
    # Stage 1 has no DIODE_0: its capacitor attaches directly to Winding_1.n1.
    assert _wire_exists(editor, result.windings[0], "n1", result.storage_capacitors[0], "n2")
    # For later stages, the same Winding_n.n1 node is also DIODE_(n-1).n2.
    for i in (1, 2):
        a = editor.net_by_endpoint[(result.diodes[i - 1].composed_name, "n2")]
        b = editor.net_by_endpoint[(result.windings[i].composed_name, "n1")]
        c = editor.net_by_endpoint[(result.storage_capacitors[i].composed_name, "n2")]
        assert a == b == c


def test_single_boost_rejects_non_contiguous_winding_numbers(tmp_path: Path):
    app = FakeMaxwell(("Winding_1", "Winding_3"))
    with pytest.raises(Exception, match="编号连续"):
        build_accelerator_external_circuit(
            app,
            options=AcceleratorCircuitOptions(topology=TOPOLOGY_SINGLE_BOOST),
            netlist_path=tmp_path / "bad.sph",
        )


def test_single_boost_applies_configurable_diode_and_vpulse_shunt_parameters(tmp_path: Path):
    app = FakeMaxwell(winding_names=("Winding_1",))
    result = build_accelerator_external_circuit(
        app,
        options=AcceleratorCircuitOptions(
            topology=TOPOLOGY_SINGLE_BOOST,
            diode_model_parameters={"IS": "2pA", "RS": "3ohm"},
            pulse_shunt_resistance="47kohm",
        ),
        netlist_path=tmp_path / "configured.sph",
    )
    assert result.diode_model.parameters["IS"] == "2pA"
    assert result.diode_model.parameters["RS"] == "3ohm"
    assert result.pulse_shunt_resistors[0].parameters["R"] == "47kohm"


def test_existing_esr_project_variable_is_preserved(tmp_path: Path):
    app = FakeMaxwell()
    app._oproject.variables["$ESR_2"] = "0.007"
    result = build_accelerator_external_circuit(
        app,
        options=AcceleratorCircuitOptions(topology=TOPOLOGY_SINGLE_BOOST),
        netlist_path=tmp_path / "esr.sph",
    )
    assert app._oproject.variables["$ESR_2"] == "0.007"
    assert "$ESR_2" not in result.created_variables
    assert result.storage_esr_resistors[1].parameters["R"] == "$ESR_2"


def test_existing_capacitance_and_shared_initial_voltage_variables_are_preserved(tmp_path: Path):
    app = FakeMaxwell()
    app._oproject.variables["$C_2"] = "0.0022"
    app._oproject.variables["$V"] = "350"
    result = build_accelerator_external_circuit(
        app,
        options=AcceleratorCircuitOptions(topology=TOPOLOGY_SINGLE_BOOST),
        netlist_path=tmp_path / "capvars.sph",
    )
    assert app._oproject.variables["$C_2"] == "0.0022"
    assert app._oproject.variables["$V"] == "350"
    assert "$C_2" not in result.created_variables
    assert "$V" not in result.created_variables
    assert result.storage_capacitors[1].parameters["C"] == "$C_2"
    for cap in result.storage_capacitors:
        assert cap.parameters["IC"] == "$V"


def test_existing_last_resistance_variable_is_preserved(tmp_path: Path):
    app = FakeMaxwell()
    app._oproject.variables["$Rlast"] = "47"
    result = build_accelerator_external_circuit(
        app,
        options=AcceleratorCircuitOptions(topology=TOPOLOGY_SINGLE_BOOST),
        netlist_path=tmp_path / "rlast.sph",
    )
    assert app._oproject.variables["$Rlast"] == "47"
    assert "$Rlast" not in result.created_variables
    assert result.last_resistor.parameters["R"] == "$Rlast"


def test_v090_fixed_native_pin_mapping_constants():
    from maxwell_circuit_toolkit.core import (
        SW_V4_MAIN_1_PIN, SW_V4_MAIN_2_PIN,
        SW_V4_CONTROL_PLUS_PIN, SW_V4_CONTROL_MINUS_PIN,
        VPULSE_POSITIVE_PIN, VPULSE_NEGATIVE_PIN,
        VPWL_POSITIVE_PIN, VPWL_NEGATIVE_PIN,
        WINDING_LEFT_PIN, WINDING_RIGHT_PIN,
        RESISTOR_LEFT_PIN, RESISTOR_RIGHT_PIN,
        DIODE_ANODE_PIN, DIODE_CATHODE_PIN,
        CAPACITOR_STAGE_NODE_PIN, CAPACITOR_ESR_PIN,
        ESR_CAPACITOR_PIN, ESR_GROUND_PIN,
    )
    assert SW_V4_MAIN_1_PIN == "n1"
    assert SW_V4_MAIN_2_PIN == "n2"
    assert SW_V4_CONTROL_PLUS_PIN == "n3"
    assert SW_V4_CONTROL_MINUS_PIN == "n4"
    assert VPULSE_POSITIVE_PIN == "n1"
    assert VPULSE_NEGATIVE_PIN == "n2"
    assert VPWL_POSITIVE_PIN == "n1"
    assert VPWL_NEGATIVE_PIN == "n2"
    assert WINDING_LEFT_PIN == "n1"
    assert WINDING_RIGHT_PIN == "n2"
    assert RESISTOR_LEFT_PIN == "n1"
    assert RESISTOR_RIGHT_PIN == "n2"
    assert DIODE_ANODE_PIN == "n1"
    assert DIODE_CATHODE_PIN == "n2"
    assert CAPACITOR_STAGE_NODE_PIN == "n2"
    assert CAPACITOR_ESR_PIN == "n1"
    assert ESR_CAPACITOR_PIN == "n2"
    assert ESR_GROUND_PIN == "n1"


def test_v090_removed_pin_semantic_inference_and_netlist_safety_helpers():
    import maxwell_circuit_toolkit.core as core
    for name in (
        "_switch_semantic_pins",
        "_vpulse_polarity_pins",
        "_left_right_pin_names",
        "_component_pin_names_by_x",
        "_require_same_net",
        "get_component_pin_wire_id",
        "validate_single_boost_netlist",
    ):
        assert not hasattr(core, name)


def test_project_variables_referenced_by_netlist_uses_exact_dollar_tokens(tmp_path: Path):
    sph = tmp_path / "vars.sph"
    sph.write_text(
        "R1 a b '$coil_R_2'\n"
        "C1 c d '$C_10'\n"
        "V1 e f PULSE(0 1 '$POSon_2-$proj_initZ' 0 0 '$POSdur_2' 1e9)\n"
        "C2 g h '$C_2' IC='$Vinit'\n"
        "R2 h 0 '$ESR_2'\n"
        "R3 x y '$C_2'\n",
        encoding="utf-8",
    )
    names = project_variables_referenced_by_netlist(sph)
    assert set(names) == {
        "$coil_R_2", "$C_10", "$POSon_2", "$POSdur_2",
        "$C_2", "$Vinit", "$ESR_2", "$proj_initZ",
    }
    assert len(names) == 8


def test_get_independent_project_variable_names_excludes_dependent_coil_r():
    app = FakeMaxwell(winding_names=("Winding_1",))
    app._oproject.variables.update({
        "$C_1": "1000uF",
        "$ESR_1": "1mOhm",
        "$Vinit": "400V",
    })
    names = set(get_independent_project_variable_names(app))
    assert "$C_1" in names
    assert "$ESR_1" in names
    assert "$Vinit" in names
    assert "$coil_R_1" not in names


def test_mirror_external_circuit_project_variables_only_submits_independent_circuit_globals(tmp_path: Path):
    sph = tmp_path / "mirror.sph"
    sph.write_text(
        "C1 a b '$C_1' IC='$Vinit'\n"
        "R1 b 0 '$ESR_1'\n"
        "V1 p 0 PULSE(0 1 '$POSon_2-$proj_initZ' 0 0 1e9mm 1e9mm)\n"
        "Rcoil x y '$coil_R_1'\n",
        encoding="utf-8",
    )
    app = FakeMaxwell(winding_names=("Winding_1",))
    app._oproject.variables.update({
        "$C_1": "1000uF",
        "$ESR_1": "1mOhm",
        "$Vinit": "400V",
        "$POSon_2": "25mm",
        "$proj_initZ": "5mm",
    })
    mapping = mirror_external_circuit_project_variables(
        app, circuit_design="AcceleratorExternalCircuit", netlist_path=sph
    )
    assert mapping == {
        "$C_1": "$C_1",
        "$ESR_1": "$ESR_1",
        "$POSon_2": "$POSon_2",
        "$Vinit": "$Vinit",
        "$proj_initZ": "$proj_initZ",
    }
    assert "$coil_R_1" not in mapping
    path, design, parameters = app.edits[-1]
    assert path == ""
    assert design == "AcceleratorExternalCircuit"
    assert parameters == mapping
