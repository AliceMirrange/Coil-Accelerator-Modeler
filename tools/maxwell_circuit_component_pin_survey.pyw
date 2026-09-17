# -*- coding: utf-8 -*-
"""Maxwell Circuit component/pin survey utility.

Standalone diagnostic tool for AEDT + PyAEDT.  It places every documented
component in the built-in ``Maxwell Circuit Elements`` library once and dumps
native Schematic Editor / Definition Manager return values to a TXT file.

The tool is intentionally diagnostic: it does not infer pin semantics, rename
pins, connect nets, or compensate for unexpected return values.  Failures are
written to the report and scanning continues with the next query/component.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
import queue
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# Documented built-in Maxwell Circuit Elements library (44 components total).
# Ansys Maxwell Help groups the library into these four categories.
LIBRARY_COMPONENTS = {
    "Dedicated Elements": [
        "AC_Model",
        "BarC",
        "BarC_Model",
        "ECE3_Model",
        "ECE6_Model",
        "ECEDCM_Model",
        "ECEF_Model",
        "ECEIM_Model",
        "ECEL_Model",
        "ECER_Model",
        "ECESRM_Model",
        "ECET_Model",
        "ECEW_Model",
        "Winding",
    ],
    "Passive Elements": [
        "Cap",
        "DIODE",
        "DIODE_Model",
        "Ind",
        "IndM",
        "Res",
        "SW_I",
        "SW_I4",
        "SW_IModel",
        "SW_V",
        "SW_V4",
        "SW_VModel",
        "Transformer",
    ],
    "Probes": [
        "Ammeter",
        "Voltmeter",
        "VoltmeterG",
    ],
    "Sources": [
        "IDC",
        "IExp",
        "IPulse",
        "IPWL",
        "IPWM",
        "ISffm",
        "ISin",
        "VDC",
        "VExp",
        "VPulse",
        "VPWL",
        "VPWM",
        "VSffm",
        "VSin",
    ],
}

TOTAL_COMPONENTS = sum(len(v) for v in LIBRARY_COMPONENTS.values())
PROPERTY_TABS = (
    "PassedParameterTab",
    "ComponentTab",
    "Component",
    "BaseElementTab",
    "Quantities",
    "Signals",
)


class TextReport:
    def __init__(self, path: Path, notify=None):
        self.path = path
        self.notify = notify
        self._fh = path.open("w", encoding="utf-8", newline="\n")

    def close(self):
        try:
            self._fh.flush()
        finally:
            self._fh.close()

    def line(self, text="", *, ui=False):
        s = str(text)
        self._fh.write(s + "\n")
        self._fh.flush()
        if ui and self.notify:
            self.notify(s)

    def block(self, title, value):
        self.line(f"--- {title} ---")
        self.line(_safe_repr(value))


def _safe_repr(value):
    try:
        return repr(value)
    except Exception as exc:
        return f"<repr failed: {type(exc).__name__}: {exc}>"


def _call(report: TextReport, label: str, func, *args):
    """Call one AEDT API method and record either its raw return or the error."""
    try:
        value = func(*args)
    except Exception as exc:
        report.line(f"{label}: ERROR {type(exc).__name__}: {exc}")
        return None, False
    report.line(f"{label}: {_safe_repr(value)}")
    return value, True


def _script_obj(obj, *names):
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    raise RuntimeError(f"Cannot obtain AEDT script object from attributes {names!r}")


def _dump_properties(report: TextReport, editor, comp_id: str):
    report.line("[PROPERTY TABS]")
    for tab in PROPERTY_TABS:
        try:
            props = list(editor.GetProperties(tab, comp_id) or [])
        except Exception as exc:
            report.line(f"  {tab}: ERROR {type(exc).__name__}: {exc}")
            continue
        report.line(f"  {tab}.GetProperties = {_safe_repr(props)}")
        for prop in props:
            try:
                value = editor.GetPropertyValue(tab, comp_id, str(prop))
                report.line(f"    {prop!r} = {_safe_repr(value)}")
            except Exception as exc:
                report.line(f"    {prop!r} = ERROR {type(exc).__name__}: {exc}")
            if tab in ("Quantities", "Signals"):
                try:
                    direction = editor.GetPropertyAttribute(tab, comp_id, str(prop), "Direction")
                    report.line(f"      Direction = {_safe_repr(direction)}")
                except Exception as exc:
                    report.line(f"      Direction = ERROR {type(exc).__name__}: {exc}")


def _dump_pin_state(report: TextReport, editor, comp_id: str, nominal_angle: int):
    report.line(f"[PIN STATE @ nominal rotation {nominal_angle} deg]")
    info, _ = _call(report, "GetComponentInfo", editor.GetComponentInfo, comp_id)
    pins_raw, ok = _call(report, "GetComponentPins", editor.GetComponentPins, comp_id)
    if not ok:
        return
    try:
        pins = list(pins_raw or [])
    except Exception:
        report.line(f"Could not convert GetComponentPins return to list: {_safe_repr(pins_raw)}")
        return
    report.line(f"pin_count: {len(pins)}")
    for index, pin in enumerate(pins):
        pin = str(pin)
        report.line(f"  pin[{index}] name={pin!r}")
        _call(report, f"    GetComponentPinInfo({pin!r})", editor.GetComponentPinInfo, comp_id, pin)
        _call(
            report,
            f"    GetComponentPinLocation({pin!r}, True/X)",
            editor.GetComponentPinLocation,
            comp_id,
            pin,
            True,
        )
        _call(
            report,
            f"    GetComponentPinLocation({pin!r}, False/Y)",
            editor.GetComponentPinLocation,
            comp_id,
            pin,
            False,
        )


def _rotate_90(report: TextReport, editor, comp_id: str):
    try:
        result = editor.Rotate(
            ["NAME:Selections", "Selections:=", [comp_id]],
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
        report.line(f"Rotate(+90 deg): ERROR {type(exc).__name__}: {exc}")
        return False
    report.line(f"Rotate(+90 deg): {_safe_repr(result)}")
    return True


def _manager_names(report: TextReport, manager, label: str):
    if manager is None:
        report.line(f"{label}.GetNames: manager unavailable")
        return []
    try:
        names = list(manager.GetNames() or [])
    except Exception as exc:
        report.line(f"{label}.GetNames: ERROR {type(exc).__name__}: {exc}")
        return []
    report.line(f"{label}.GetNames: {_safe_repr(names)}")
    return [str(x) for x in names]


def _dump_manager_data(report: TextReport, manager, label: str, candidates):
    if manager is None:
        report.line(f"{label}: manager unavailable")
        return
    seen = set()
    for candidate in candidates:
        if candidate is None:
            continue
        candidate = str(candidate)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            data = manager.GetData(candidate)
        except Exception as exc:
            report.line(f"{label}.GetData({candidate!r}): ERROR {type(exc).__name__}: {exc}")
        else:
            report.line(f"{label}.GetData({candidate!r}):")
            report.line(_safe_repr(data))


def run_survey(output_dir: Path, design_name: str, gui_log):
    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    txt_path = output_dir / f"MaxwellCircuit_ComponentPinSurvey_{timestamp}.txt"
    aedt_path = output_dir / f"MaxwellCircuit_ComponentPinSurvey_{timestamp}.aedt"

    report = TextReport(txt_path, gui_log)
    app = None
    try:
        report.line("Maxwell Circuit Component / Pin Survey", ui=True)
        report.line(f"Generated: {_dt.datetime.now().isoformat(timespec='seconds')}")
        report.line(f"Documented library component count: {TOTAL_COMPONENTS}")
        report.line(f"Requested design name: {design_name}")
        report.line(f"Target AEDT project: {aedt_path}")
        report.line("")
        report.line("IMPORTANT: This utility records raw AEDT returns. It intentionally performs no pin-semantic inference.")
        report.line("")

        from ansys.aedt.core import MaxwellCircuit
        import ansys.aedt.core as aedt_core

        report.line(f"ansys.aedt.core module: {_safe_repr(aedt_core)}")
        report.line(f"PyAEDT package version: {_safe_repr(getattr(aedt_core, '__version__', 'unknown'))}")

        # A dedicated Desktop/project avoids touching the accelerator project.
        app = MaxwellCircuit(
            project=None,
            design=design_name,
            new_desktop=True,
            non_graphical=False,
            close_on_exit=False,
        )
        report.line(f"MaxwellCircuit object created: {_safe_repr(app)}", ui=True)
        report.line(f"project_name: {_safe_repr(getattr(app, 'project_name', None))}")
        report.line(f"design_name: {_safe_repr(getattr(app, 'design_name', None))}")

        try:
            if getattr(app, "design_name", None) != design_name:
                result = app.rename_design(design_name, save=False)
                report.line(f"rename_design({design_name!r}) -> {_safe_repr(result)}")
        except Exception as exc:
            report.line(f"rename_design: ERROR {type(exc).__name__}: {exc}")

        try:
            result = app.save_project(str(aedt_path))
            report.line(f"save_project(initial) -> {_safe_repr(result)}")
        except Exception as exc:
            report.line(f"save_project(initial): ERROR {type(exc).__name__}: {exc}")

        odesign = _script_obj(app, "odesign", "_odesign")
        oproject = _script_obj(app, "oproject", "_oproject")
        editor = odesign.SetActiveEditor("SchematicEditor")
        report.line(f"SchematicEditor: {_safe_repr(editor)}")

        try:
            def_manager = oproject.GetDefinitionManager()
            comp_manager = def_manager.GetManager("Component")
        except Exception as exc:
            report.line(f"ComponentManager acquisition: ERROR {type(exc).__name__}: {exc}")
            def_manager = None
            comp_manager = None

        try:
            symbol_manager = def_manager.GetManager("Symbol") if def_manager is not None else None
        except Exception as exc:
            report.line(f"SymbolManager acquisition: ERROR {type(exc).__name__}: {exc}")
            symbol_manager = None

        report.line("")
        report.line("=== INITIAL DEFINITION MANAGER STATE ===")
        initial_component_defs = _manager_names(report, comp_manager, "ComponentManager")
        initial_symbol_defs = _manager_names(report, symbol_manager, "SymbolManager")

        # Native CreateComponent takes schematic coordinates in SI units.  Use a
        # generous grid so that all 44 unconnected symbols are visually separate.
        dx = 0.03048   # 1200 mil
        dy = 0.02540   # 1000 mil
        columns = 6
        serial = 0
        success_count = 0
        failure_count = 0

        for category, names in LIBRARY_COMPONENTS.items():
            report.line("")
            report.line("#" * 88)
            report.line(f"CATEGORY: {category}", ui=True)
            report.line("#" * 88)

            for component_name in names:
                serial += 1
                full_path = f"Maxwell Circuit Elements\\{category}:{component_name}"
                col = (serial - 1) % columns
                row = (serial - 1) // columns
                x = col * dx
                y = -row * dy

                report.line("")
                report.line("=" * 88)
                report.line(f"[{serial:02d}/{TOTAL_COMPONENTS:02d}] {full_path}", ui=True)
                report.line(f"placement: X={x!r}, Y={y!r}, Angle=0, Flip=False")

                before_comp_defs = set(_manager_names(report, comp_manager, "ComponentManager.before"))
                before_sym_defs = set(_manager_names(report, symbol_manager, "SymbolManager.before"))

                try:
                    comp_id = editor.CreateComponent(
                        [
                            "NAME:ComponentProps",
                            "Name:=",
                            full_path,
                            "Id:=",
                            str(serial),
                        ],
                        [
                            "NAME:Attributes",
                            "Page:=",
                            1,
                            "X:=",
                            x,
                            "Y:=",
                            y,
                            "Angle:=",
                            0,
                            "Flip:=",
                            False,
                        ],
                    )
                except Exception as exc:
                    failure_count += 1
                    report.line(f"CreateComponent: ERROR {type(exc).__name__}: {exc}", ui=True)
                    report.line(traceback.format_exc())
                    continue

                success_count += 1
                comp_id = str(comp_id)
                report.line(f"CreateComponent return: {comp_id!r}")

                comp_info, _ = _call(report, "GetComponentInfo(initial)", editor.GetComponentInfo, comp_id)
                _dump_properties(report, editor, comp_id)

                after_comp_defs = set(_manager_names(report, comp_manager, "ComponentManager.after"))
                after_sym_defs = set(_manager_names(report, symbol_manager, "SymbolManager.after"))
                new_comp_defs = sorted(after_comp_defs - before_comp_defs)
                new_sym_defs = sorted(after_sym_defs - before_sym_defs)
                report.line(f"new Component definitions after placement: {_safe_repr(new_comp_defs)}")
                report.line(f"new Symbol definitions after placement: {_safe_repr(new_sym_defs)}")

                component_info_name = None
                if comp_info:
                    try:
                        for item in list(comp_info):
                            text = str(item)
                            if text.startswith("ComponentName="):
                                component_info_name = text.split("=", 1)[1]
                                break
                    except Exception:
                        pass

                report.line("[COMPONENT MANAGER DATA]")
                comp_candidates = list(new_comp_defs) + [component_info_name, component_name, full_path]
                _dump_manager_data(report, comp_manager, "ComponentManager", comp_candidates)

                report.line("[SYMBOL MANAGER DATA]")
                symbol_candidates = list(new_sym_defs)
                # Also try component name only as a diagnostic; errors are useful evidence.
                symbol_candidates.append(component_name)
                _dump_manager_data(report, symbol_manager, "SymbolManager", symbol_candidates)

                # Read the exact same instance at four orientations.  Pin names are
                # never interpreted; the report captures whether AEDT preserves them.
                for orientation_index, nominal_angle in enumerate((0, 90, 180, 270)):
                    _dump_pin_state(report, editor, comp_id, nominal_angle)
                    if orientation_index < 3:
                        if not _rotate_90(report, editor, comp_id):
                            report.line("Remaining orientation checks skipped because Rotate failed.")
                            break

                report.line("END COMPONENT")

        report.line("")
        report.line("=" * 88)
        report.line("FINAL SUMMARY", ui=True)
        report.line(f"documented components attempted: {TOTAL_COMPONENTS}")
        report.line(f"CreateComponent succeeded: {success_count}")
        report.line(f"CreateComponent failed: {failure_count}")
        report.line("")
        report.line("=== FINAL COMPONENT MANAGER STATE ===")
        _manager_names(report, comp_manager, "ComponentManager.final")
        report.line("=== FINAL SYMBOL MANAGER STATE ===")
        _manager_names(report, symbol_manager, "SymbolManager.final")

        try:
            result = app.save_project(str(aedt_path))
            report.line(f"save_project(final) -> {_safe_repr(result)}")
        except Exception as exc:
            report.line(f"save_project(final): ERROR {type(exc).__name__}: {exc}")

        report.line(f"TXT report: {txt_path}")
        report.line(f"AEDT project: {aedt_path}")
        report.line("AEDT is intentionally left open for manual inspection.", ui=True)
        return txt_path, aedt_path, success_count, failure_count

    except Exception:
        report.line("")
        report.line("FATAL ERROR", ui=True)
        report.line(traceback.format_exc())
        raise
    finally:
        # Do not close AEDT. Release only the Python-side control handle if possible.
        if app is not None:
            try:
                app.release_desktop(close_projects=False, close_desktop=False)
                report.line("release_desktop(close_projects=False, close_desktop=False) completed.")
            except Exception as exc:
                report.line(f"release_desktop: ERROR {type(exc).__name__}: {exc}")
        report.close()


class SurveyGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Maxwell Circuit 全元件 Pin 返回值扫描")
        self.root.geometry("760x520")
        self.events = queue.Queue()

        default_dir = r"E:/Ansys/Maxwell/LinearAcceleratorGenerator"

        frm = ttk.Frame(root, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(4, weight=1)

        ttk.Label(frm, text=f"扫描对象：Maxwell Circuit Elements 官方 4 类，共 {TOTAL_COMPONENTS} 个元件").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )

        ttk.Label(frm, text="输出目录：").grid(row=1, column=0, sticky="w")
        self.output_var = tk.StringVar(value=default_dir)
        ttk.Entry(frm, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="浏览...", command=self._browse).grid(row=1, column=2)

        ttk.Label(frm, text="Circuit Design 名称：").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.design_var = tk.StringVar(value="MaxwellCircuit_ComponentPinSurvey")
        ttk.Entry(frm, textvariable=self.design_var).grid(row=2, column=1, sticky="ew", padx=6, pady=(6, 0))

        self.start_btn = ttk.Button(frm, text="开始扫描", command=self._start)
        self.start_btn.grid(row=3, column=0, columnspan=3, pady=12)

        self.log = tk.Text(frm, height=20, wrap="word")
        self.log.grid(row=4, column=0, columnspan=3, sticky="nsew")
        scroll = ttk.Scrollbar(frm, orient="vertical", command=self.log.yview)
        scroll.grid(row=4, column=3, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        note = (
            "说明：脚本会启动独立 AEDT Desktop，创建一个 Maxwell Circuit 项目；"
            "每个元件记录 native pin 名、pin 信息、坐标、四个旋转方向、属性页、"
            "ComponentManager/SymbolManager 数据。任何单项失败只写入 TXT，不中断整个扫描。"
        )
        ttk.Label(frm, text=note, wraplength=710).grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.root.after(100, self._pump)

    def _browse(self):
        path = filedialog.askdirectory(initialdir=self.output_var.get() or None)
        if path:
            self.output_var.set(path)

    def _append(self, text):
        self.log.insert("end", str(text) + "\n")
        self.log.see("end")

    def _start(self):
        out = self.output_var.get().strip()
        design = self.design_var.get().strip()
        if not out:
            messagebox.showerror("参数错误", "请指定输出目录。")
            return
        if not design:
            messagebox.showerror("参数错误", "请指定 Circuit Design 名称。")
            return

        self.start_btn.configure(state="disabled")
        self.log.delete("1.0", "end")
        self._append("开始扫描。这个过程会放置 44 个元件，并多次调用 AEDT Scripting API。")

        def worker():
            try:
                result = run_survey(Path(out), design, lambda s: self.events.put(("log", s)))
            except Exception as exc:
                self.events.put(("error", f"{type(exc).__name__}: {exc}"))
            else:
                self.events.put(("done", result))

        threading.Thread(target=worker, daemon=True).start()

    def _pump(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "error":
                    self.start_btn.configure(state="normal")
                    messagebox.showerror("扫描失败", payload)
                elif kind == "done":
                    self.start_btn.configure(state="normal")
                    txt_path, aedt_path, ok, failed = payload
                    self._append(f"完成：成功放置 {ok}，失败 {failed}")
                    messagebox.showinfo(
                        "扫描完成",
                        f"成功放置：{ok}\n失败：{failed}\n\nTXT：\n{txt_path}\n\nAEDT：\n{aedt_path}\n\n请把 TXT 文件发给我。",
                    )
        except queue.Empty:
            pass
        self.root.after(100, self._pump)


def main():
    root = tk.Tk()
    SurveyGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
