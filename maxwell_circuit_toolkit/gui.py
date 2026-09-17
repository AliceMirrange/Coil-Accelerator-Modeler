from __future__ import annotations

import queue
import os
import threading
import traceback
from configparser import ConfigParser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parents[1]

try:
    from maxwell_circuit_toolkit import (
        AcceleratorCircuitOptions,
        PROJECTILE_MOVE_VECTOR,
        PROJECTILE_MOVE_REMINDER,
        TOPOLOGY_SINGLE_BOOST,
        available_topologies,
        build_accelerator_external_circuit,
        mirror_external_circuit_project_variables,
        topology_display_name,
    )
    TOOLKIT_IMPORT_ERROR = None
except Exception as exc:  # shown in GUI instead of crashing a .pyw silently
    AcceleratorCircuitOptions = None  # type: ignore[assignment]
    build_accelerator_external_circuit = None  # type: ignore[assignment]
    mirror_external_circuit_project_variables = None  # type: ignore[assignment]
    PROJECTILE_MOVE_REMINDER = (
        "\u5728\u7ed8\u5236\u5b8c\u52a8\u5b50\u540e\uff0c\u6cbfZ+\u65b9\u5411\u6267\u884cMove\u64cd\u4f5c\uff0c"
        "Moving Vector\u4e3a(0,0,$proj_initZ)"
    )
    PROJECTILE_MOVE_VECTOR = "0,0,$proj_initZ"
    TOPOLOGY_SINGLE_BOOST = "single_boost"
    available_topologies = None  # type: ignore[assignment]
    topology_display_name = lambda key: str(key)  # type: ignore[assignment]
    TOOLKIT_IMPORT_ERROR = exc

@dataclass(frozen=True, slots=True)
class GUIConfig:
    project_path: Path
    maxwell_design: str
    circuit_design: str
    topology: str
    aedt_version: str | None
    non_graphical: bool
    delete_existing: bool
    switch_model_name: str
    diode_model_name: str
    diode_model_parameters: dict[str, str]
    ron: str
    roff: str
    von: str
    voff: str
    v1: str
    v2: str
    tr: str
    tf: str
    period: str
    vpwl_fall_delta: str
    vpwl_end_position: str
    pulse_shunt_resistance: str
    pos_on_prefix: str
    pos_dur_prefix: str
    coil_resistance_prefix: str
    capacitance_prefix: str
    esr_prefix: str
    initial_voltage_variable: str
    last_resistance_variable: str
    fallback_pos_on: str
    fallback_pos_dur: str
    first_stage_position_on: str
    fallback_capacitance: str
    fallback_initial_voltage: str
    fallback_esr: str
    fallback_last_resistance: str


def _release_aedt(app: Any, *, non_graphical: bool) -> None:
    """Release PyAEDT control; close only a headless session."""
    if app is None:
        return
    release = getattr(app, "release_desktop", None)
    if release is not None:
        try:
            release(close_projects=bool(non_graphical), close_desktop=bool(non_graphical))
            return
        except TypeError:
            try:
                release(bool(non_graphical), bool(non_graphical))
                return
            except Exception:
                pass
    desktop = getattr(app, "desktop_class", None)
    if desktop is not None:
        release = getattr(desktop, "release_desktop", None)
        if release is not None:
            try:
                release(close_projects=bool(non_graphical), close_desktop=bool(non_graphical))
                return
            except Exception:
                pass
    if non_graphical:
        close = getattr(getattr(app, "desktop_class", None), "close_desktop", None)
        if close is not None:
            close()


def build_circuit(config: GUIConfig, progress) -> dict[str, Any]:
    """Open Maxwell, create/assign the external circuit, save, and release."""
    if build_accelerator_external_circuit is None or AcceleratorCircuitOptions is None:
        raise RuntimeError(f"Cannot import maxwell_circuit_toolkit: {TOOLKIT_IMPORT_ERROR}")
    if not config.project_path.exists():
        raise FileNotFoundError(f"AEDT 工程不存在：{config.project_path}")

    app = None
    primary_error: BaseException | None = None
    try:
        progress("正在导入 PyAEDT…")
        from ansys.aedt.core import Maxwell2d

        progress("正在连接/启动 AEDT，并打开 Maxwell2D Design…")
        # Graphical mode intentionally uses new_desktop=False so this builder can
        # attach to the AEDT instance left open by the geometry builder.  In
        # headless mode an isolated process is safer and is closed on completion.
        app = Maxwell2d(
            project=str(config.project_path),
            design=config.maxwell_design,
            version=config.aedt_version,
            non_graphical=config.non_graphical,
            new_desktop=bool(config.non_graphical),
            close_on_exit=False,
        )

        options = AcceleratorCircuitOptions(
            circuit_design=config.circuit_design,
            topology=config.topology,
            switch_model_name=config.switch_model_name,
            diode_model_name=config.diode_model_name,
            diode_model_parameters=config.diode_model_parameters,
            ron=config.ron,
            roff=config.roff,
            von=config.von,
            voff=config.voff,
            v1=config.v1,
            v2=config.v2,
            tr=config.tr,
            tf=config.tf,
            period=config.period,
            vpwl_fall_delta=config.vpwl_fall_delta,
            vpwl_end_position=config.vpwl_end_position,
            pulse_shunt_resistance=config.pulse_shunt_resistance,
            pos_on_prefix=config.pos_on_prefix,
            pos_dur_prefix=config.pos_dur_prefix,
            coil_resistance_prefix=config.coil_resistance_prefix,
            capacitance_prefix=config.capacitance_prefix,
            esr_prefix=config.esr_prefix,
            initial_voltage_variable=config.initial_voltage_variable,
            last_resistance_variable=config.last_resistance_variable,
            fallback_pos_on=config.fallback_pos_on,
            fallback_pos_dur=config.fallback_pos_dur,
            first_stage_position_on=config.first_stage_position_on,
            fallback_capacitance=config.fallback_capacitance,
            fallback_initial_voltage=config.fallback_initial_voltage,
            fallback_esr=config.fallback_esr,
            fallback_last_resistance=config.fallback_last_resistance,
            delete_existing_circuit=config.delete_existing,
        )

        netlist = config.project_path.with_name(
            f"{config.project_path.stem}_{config.circuit_design}.sph"
        )
        result = build_accelerator_external_circuit(
            app,
            options=options,
            netlist_path=netlist,
            progress=progress,
        )
        progress("正在保存 AEDT 工程…")
        try:
            app.save_project(str(config.project_path))
        except TypeError:
            app.save_project()

        return {
            "winding_names": [getattr(x, "name", str(x)) for x in result.windings],
            "topology": result.topology,
            "switch_count": len(result.switches),
            "pulse_count": len(result.pulses),
            "vpwl_count": len(result.vpwl_sources),
            "vpulse_count": len(result.vpulse_sources),
            "diode_count": len(result.diodes),
            "resistor_count": len(result.resistors),
            "pulse_shunt_resistor_count": len(result.pulse_shunt_resistors),
            "storage_capacitor_count": len(result.storage_capacitors),
            "storage_esr_resistor_count": len(result.storage_esr_resistors),
            "last_resistor_count": len(result.return_resistors),
            "ground_count": len(result.grounds),
            "created_variables": dict(result.created_variables),
            "netlist_path": str(result.netlist_path or netlist),
        }
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if app is not None:
            try:
                progress("正在释放 PyAEDT/AEDT 会话…")
                _release_aedt(app, non_graphical=config.non_graphical)
            except Exception:
                if primary_error is None:
                    raise


def sync_external_circuit_parameters(config: GUIConfig, progress) -> dict[str, Any]:
    """Mirror External Circuit project-parameter names into their Value fields."""
    if mirror_external_circuit_project_variables is None:
        raise RuntimeError(f"Cannot import maxwell_circuit_toolkit: {TOOLKIT_IMPORT_ERROR}")
    if not config.project_path.exists():
        raise FileNotFoundError(f"AEDT 工程不存在：{config.project_path}")

    netlist = config.project_path.with_name(
        f"{config.project_path.stem}_{config.circuit_design}.sph"
    )
    if not netlist.is_file():
        raise FileNotFoundError(
            "未找到与当前 Circuit Design 对应的已导出 .sph："
            f"{netlist}\n请先用“生成并回写 External Circuit”生成一次电路。"
        )

    app = None
    primary_error: BaseException | None = None
    try:
        progress("正在导入 PyAEDT…")
        from ansys.aedt.core import Maxwell2d

        progress("正在连接/启动 AEDT，并打开 Maxwell2D Design…")
        app = Maxwell2d(
            project=str(config.project_path),
            design=config.maxwell_design,
            version=config.aedt_version,
            non_graphical=config.non_graphical,
            new_desktop=bool(config.non_graphical),
            close_on_exit=False,
        )
        progress("正在筛选 Maxwell Circuit 实际使用的独立项目全局变量…")
        mapping = mirror_external_circuit_project_variables(
            app,
            circuit_design=config.circuit_design,
            netlist_path=netlist,
        )
        progress("正在保存 AEDT 工程…")
        try:
            app.save_project(str(config.project_path))
        except TypeError:
            app.save_project()
        return {
            "parameter_count": len(mapping),
            "parameters": list(mapping.keys()),
            "netlist_path": str(netlist),
        }
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if app is not None:
            try:
                progress("正在释放 PyAEDT/AEDT 会话…")
                _release_aedt(app, non_graphical=config.non_graphical)
            except Exception:
                if primary_error is None:
                    raise


class CircuitBuilderGUI:
    def __init__(
        self,
        root: tk.Misc,
        *,
        settings: ConfigParser | None = None,
        config_path: str | Path = SCRIPT_DIR / "default.ini",
        log_dir: str | Path = SCRIPT_DIR / "logs",
        diode_parameters: dict[str, str] | None = None,
        project_path: tk.StringVar | None = None,
        maxwell_design: tk.StringVar | None = None,
        aedt_version: tk.StringVar | None = None,
        non_graphical: tk.BooleanVar | None = None,
        manage_window: bool = True,
    ) -> None:
        self.root = root
        if settings is None or diode_parameters is None:
            raise ValueError("CircuitBuilderGUI requires settings and diode_parameters from the main program")
        self.settings = settings
        self.config_path = Path(config_path)
        self.log_dir = Path(log_dir)
        defaults = self.settings["gui_defaults"]
        if manage_window:
            self.root.title("Maxwell 直线电磁加速器 External Circuit 建模器")
            self.root.minsize(940, 650)

        previous_path = defaults["last_project_path"].strip()
        filename = defaults["project_filename"].format(timestamp=datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.project_path = project_path or tk.StringVar(value=previous_path or str(SCRIPT_DIR / filename))
        self.maxwell_design = maxwell_design or tk.StringVar(value=defaults["maxwell_design"])
        self.circuit_design = tk.StringVar(value=defaults["circuit_design"])
        self.aedt_version = aedt_version or tk.StringVar(value=defaults["aedt_version"])
        self.non_graphical = non_graphical or tk.BooleanVar(value=defaults.getboolean("non_graphical"))
        self.delete_existing = tk.BooleanVar(value=defaults.getboolean("delete_existing_circuit"))

        topology_pairs = available_topologies() if TOOLKIT_IMPORT_ERROR is None else [("single_boost", "单路Boost")]
        self.topology_pairs = topology_pairs
        self.topology_by_label = {label: key for key, label in topology_pairs}
        topology_key = self.settings.get("circuit", "topology").strip()
        default_label = next((label for key, label in topology_pairs if key == topology_key), None)
        if default_label is None:
            raise ValueError(f"default.ini 中不支持的电路拓扑：{topology_key}")
        self.topology_display = tk.StringVar(value=default_label)

        switch = self.settings["voltage_controlled_switch"]
        pulse = self.settings["vpulse"]
        pwl = self.settings["vpwl"]
        initial = self.settings["initial_values"]
        prefixes = self.settings["variable_prefixes"]
        self.switch_model_name = tk.StringVar(value=switch["model_name"])
        self.diode_model_name = tk.StringVar(value=self.settings.get("diode", "model_name"))
        self.diode_parameters = dict(diode_parameters)
        self.ron = tk.StringVar(value=switch["Ron"])
        self.roff = tk.StringVar(value=switch["Roff"])
        self.von = tk.StringVar(value=switch["Von"])
        self.voff = tk.StringVar(value=switch["Voff"])

        self.v1 = tk.StringVar(value=pulse["V1"])
        self.v2 = tk.StringVar(value=pulse["V2"])
        self.tr = tk.StringVar(value=pulse["Tr"])
        self.tf = tk.StringVar(value=pulse["Tf"])
        self.period = tk.StringVar(value=pulse["Period"])
        self.vpwl_fall_delta = pwl["fall_delta"].strip()
        self.vpwl_end_position = pwl["end_position"].strip()
        self.pulse_shunt_resistance = tk.StringVar(value=pulse["shunt_resistance"])
        self.fallback_pos_on = tk.StringVar(value=initial["pos_on"])
        self.fallback_pos_dur = tk.StringVar(value=initial["pos_dur"])
        self.first_stage_position_on = initial["first_stage_pos_on"].strip()
        self.fallback_capacitance = tk.StringVar(value=initial["capacitance"])
        self.fallback_initial_voltage = tk.StringVar(value=initial["initial_voltage"])
        self.fallback_esr = tk.StringVar(value=initial["esr"])
        self.fallback_last_resistance = initial["last_resistance"]
        self.pos_on_prefix = prefixes["pos_on"]
        self.pos_dur_prefix = prefixes["pos_dur"]
        self.coil_resistance_prefix = prefixes["r_dc"]
        self.capacitance_prefix = prefixes["capacitance"]
        self.esr_prefix = prefixes["esr"]
        self.initial_voltage_variable = prefixes["initial_voltage"]
        self.last_resistance_variable = prefixes["last_resistance"]

        self.status = tk.StringVar(value="就绪。请选择包含 External Winding 的 Maxwell 工程。")
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        general = ttk.Frame(self.root, padding=12)
        general.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Label(general, text="AEDT 工程文件").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(general, textvariable=self.project_path, width=76).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(general, text="浏览…", command=self._browse_project).grid(row=0, column=2)

        ttk.Label(general, text="Maxwell2D Design").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(general, textvariable=self.maxwell_design, width=32).grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(general, text="新建 Maxwell Circuit Design").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(general, textvariable=self.circuit_design, width=32).grid(row=2, column=1, sticky="w", padx=6)

        ttk.Label(general, text="电路拓扑").grid(row=3, column=0, sticky="w", pady=5)
        self.topology_combo = ttk.Combobox(
            general,
            textvariable=self.topology_display,
            values=[label for _key, label in self.topology_pairs],
            state="readonly",
            width=30,
        )
        self.topology_combo.grid(row=3, column=1, sticky="w", padx=6, pady=5)

        ttk.Label(general, text="AEDT 版本").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Entry(general, textvariable=self.aedt_version, width=16).grid(row=4, column=1, sticky="w", padx=6)

        ttk.Checkbutton(
            general,
            text="非图形化模式",
            variable=self.non_graphical,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 3))
        ttk.Checkbutton(
            general,
            text="同名 Maxwell Circuit Design 已存在时删除并重建",
            variable=self.delete_existing,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=3)

        for row, (label, variable) in enumerate(
            (
                ("共享二极管模型名称", self.diode_model_name),
                (f"缺失 ${self.capacitance_prefix}_n 时初值", self.fallback_capacitance),
                (f"缺失 ${self.initial_voltage_variable} 时初值", self.fallback_initial_voltage),
                (f"缺失 ${self.esr_prefix}_n 时初值", self.fallback_esr),
            ),
            start=7,
        ):
            ttk.Label(general, text=label).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(general, textvariable=variable, width=30).grid(row=row, column=1, sticky="w", padx=6)

        self.move_reminder_label = tk.Label(
            general,
            text=(
                "\u91cd\u8981\u63d0\u793a\uff1a"
                + PROJECTILE_MOVE_REMINDER
                + "\uff1b\u70b9\u51fb\u672c\u884c\u590d\u5236 Moving Vector"
            ),
            fg="#a00000",
            anchor="w",
            justify="left",
            cursor="hand2",
        )
        self.move_reminder_label.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(12, 3))
        self.move_reminder_label.bind("<Button-1>", self._copy_projectile_move_vector)
        ttk.Button(general, text="打开 default.ini", command=lambda: os.startfile(self.config_path)).grid(
            row=12, column=2, sticky="e", pady=(6, 3)
        )
        general.columnconfigure(1, weight=1)

        bottom = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=180)
        self.progress.pack(side="left", padx=(0, 10))
        ttk.Label(bottom, textvariable=self.status).pack(side="left", fill="x", expand=True)
        self.build_button = ttk.Button(bottom, text="生成并回写 External Circuit", command=self._start_build)
        self.build_button.pack(side="right")
        self.sync_button = ttk.Button(
            bottom,
            text="同步 Circuit 全局变量 → Parameter Values",
            command=self._start_sync_parameters,
        )
        self.sync_button.pack(side="right", padx=(0, 8))

    def _copy_projectile_move_vector(self, _event: Any = None) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(PROJECTILE_MOVE_VECTOR)
        self.root.update_idletasks()
        self.status.set(f"\u5df2\u590d\u5236 Moving Vector\uff1a{PROJECTILE_MOVE_VECTOR}")

    def _browse_project(self) -> None:
        initial = Path(self.project_path.get())
        path = filedialog.askopenfilename(
            title="选择 Maxwell AEDT 工程",
            initialdir=str(initial.parent) if initial.parent.exists() else str(SCRIPT_DIR),
            filetypes=[("Ansys Electronics Desktop project", "*.aedt"), ("All files", "*.*")],
        )
        if path:
            self.project_path.set(path)

    def _collect_config(self) -> GUIConfig:
        path = Path(self.project_path.get().strip())
        if not str(path):
            raise ValueError("请选择 AEDT 工程文件。")
        if path.suffix.lower() != ".aedt":
            raise ValueError("工程文件必须是 .aedt。")
        maxwell_design = self.maxwell_design.get().strip()
        circuit_design = self.circuit_design.get().strip()
        if not maxwell_design or not circuit_design:
            raise ValueError("Maxwell Design 与 Circuit Design 名称不能为空。")
        version = self.aedt_version.get().strip() or None
        topology_label = self.topology_display.get().strip()
        topology = self.topology_by_label.get(topology_label)
        if topology is None:
            raise ValueError(f"未知电路拓扑：{topology_label}")
        for label, var in (
            ("共享开关模型名称", self.switch_model_name),
            ("共享二极管模型名称", self.diode_model_name),
            ("Ron", self.ron), ("Roff", self.roff), ("Von", self.von), ("Voff", self.voff),
            ("V1", self.v1), ("V2", self.v2), ("Tr", self.tr), ("Tf", self.tf),
            ("Period", self.period), ("VPULSE 并联电阻", self.pulse_shunt_resistance),
            (f"{self.pos_on_prefix} 回退值", self.fallback_pos_on),
            (f"{self.pos_dur_prefix} 回退值", self.fallback_pos_dur),
            ("$C_n 初值", self.fallback_capacitance),
            (f"${self.initial_voltage_variable} 初值", self.fallback_initial_voltage),
            ("ESR 初值", self.fallback_esr),
        ):
            if not var.get().strip():
                raise ValueError(f"{label} 不能为空。")
        for label, value in (
            ("VPWL fall_delta", self.vpwl_fall_delta),
            ("VPWL end_position", self.vpwl_end_position),
            ("first_stage_pos_on", self.first_stage_position_on),
        ):
            if not value:
                raise ValueError(f"{label} must not be empty.")
        return GUIConfig(
            project_path=path,
            maxwell_design=maxwell_design,
            circuit_design=circuit_design,
            topology=topology,
            aedt_version=version,
            non_graphical=bool(self.non_graphical.get()),
            delete_existing=bool(self.delete_existing.get()),
            switch_model_name=self.switch_model_name.get().strip(),
            diode_model_name=self.diode_model_name.get().strip(),
            diode_model_parameters=dict(self.diode_parameters),
            ron=self.ron.get().strip(),
            roff=self.roff.get().strip(),
            von=self.von.get().strip(),
            voff=self.voff.get().strip(),
            v1=self.v1.get().strip(),
            v2=self.v2.get().strip(),
            tr=self.tr.get().strip(),
            tf=self.tf.get().strip(),
            period=self.period.get().strip(),
            vpwl_fall_delta=self.vpwl_fall_delta,
            vpwl_end_position=self.vpwl_end_position,
            pulse_shunt_resistance=self.pulse_shunt_resistance.get().strip(),
            pos_on_prefix=self.pos_on_prefix,
            pos_dur_prefix=self.pos_dur_prefix,
            coil_resistance_prefix=self.coil_resistance_prefix,
            capacitance_prefix=self.capacitance_prefix,
            esr_prefix=self.esr_prefix,
            initial_voltage_variable=self.initial_voltage_variable,
            last_resistance_variable=self.last_resistance_variable,
            fallback_pos_on=self.fallback_pos_on.get().strip(),
            fallback_pos_dur=self.fallback_pos_dur.get().strip(),
            first_stage_position_on=self.first_stage_position_on,
            fallback_capacitance=self.fallback_capacitance.get().strip(),
            fallback_initial_voltage=self.fallback_initial_voltage.get().strip(),
            fallback_esr=self.fallback_esr.get().strip(),
            fallback_last_resistance=self.fallback_last_resistance,
        )

    def _start_build(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        try:
            config = self._collect_config()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.root)
            return
        if TOOLKIT_IMPORT_ERROR is not None:
            messagebox.showerror(
                "函数库加载失败",
                f"无法加载 maxwell_circuit_toolkit。\n\n{TOOLKIT_IMPORT_ERROR}",
                parent=self.root,
            )
            return

        self.build_button.configure(state="disabled")
        self.sync_button.configure(state="disabled")
        self.progress.start(12)
        self.status.set("正在启动电路建模任务…")
        self.worker = threading.Thread(target=self._worker, args=(config,), daemon=True)
        self.worker.start()
        self.root.after(100, self._poll_events)

    def _start_sync_parameters(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        try:
            config = self._collect_config()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.root)
            return
        if TOOLKIT_IMPORT_ERROR is not None:
            messagebox.showerror(
                "函数库加载失败",
                f"无法加载 maxwell_circuit_toolkit。\n\n{TOOLKIT_IMPORT_ERROR}",
                parent=self.root,
            )
            return

        self.build_button.configure(state="disabled")
        self.sync_button.configure(state="disabled")
        self.progress.start(12)
        self.status.set("正在同步 External Circuit Parameter Values…")
        self.worker = threading.Thread(target=self._sync_worker, args=(config,), daemon=True)
        self.worker.start()
        self.root.after(100, self._poll_events)

    def _sync_worker(self, config: GUIConfig) -> None:
        try:
            result = sync_external_circuit_parameters(
                config, lambda msg: self.events.put(("progress", msg))
            )
        except Exception as exc:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_dir / "maxwell_circuit_parameter_sync_error.log"
            try:
                log_path.write_text(traceback.format_exc(), encoding="utf-8")
            except Exception:
                pass
            self.events.put(("error", (exc, log_path, "参数同步")))
        else:
            self.events.put(("sync_done", result))

    def _worker(self, config: GUIConfig) -> None:
        try:
            result = build_circuit(config, lambda msg: self.events.put(("progress", msg)))
        except Exception as exc:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_dir / "maxwell_circuit_builder_error.log"
            try:
                log_path.write_text(traceback.format_exc(), encoding="utf-8")
            except Exception:
                pass
            self.events.put(("error", (exc, log_path, "建模")))
        else:
            self.events.put(("done", result))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self.status.set(str(payload))
                elif kind == "error":
                    exc, log_path, operation = payload
                    self.progress.stop()
                    self.build_button.configure(state="normal")
                    self.sync_button.configure(state="normal")
                    self.status.set(f"External Circuit {operation}失败。")
                    messagebox.showerror(
                        f"{operation}失败",
                        f"{exc}\n\n完整 traceback：\n{log_path}",
                        parent=self.root,
                    )
                    return
                elif kind == "sync_done":
                    self.progress.stop()
                    self.build_button.configure(state="normal")
                    self.sync_button.configure(state="normal")
                    names = list(payload.get("parameters", []))
                    preview = ", ".join(names)
                    if len(preview) > 800:
                        preview = preview[:800] + "…"
                    self.status.set(
                        f"完成：已提交 {int(payload['parameter_count'])} 个 Parameter Values 映射。"
                    )
                    messagebox.showinfo(
                        "Parameter Values 同步完成",
                        f"已将 {payload['parameter_count']} 个 External Circuit 参数映射为同名项目全局变量。\n"
                        f"Netlist：{payload['netlist_path']}\n\n"
                        f"映射：{preview}",
                        parent=self.root,
                    )
                    return
                elif kind == "done":
                    self.progress.stop()
                    self.build_button.configure(state="normal")
                    self.sync_button.configure(state="normal")
                    count = int(payload["switch_count"])
                    vpwl_count = int(payload.get("vpwl_count", 0))
                    vpulse_count = int(payload.get("vpulse_count", 0))
                    diode_count = int(payload.get("diode_count", 0))
                    resistor_count = int(payload.get("resistor_count", 0))
                    shunt_count = int(payload.get("pulse_shunt_resistor_count", 0))
                    storage_cap_count = int(payload.get("storage_capacitor_count", 0))
                    storage_esr_count = int(payload.get("storage_esr_resistor_count", 0))
                    last_resistor_count = int(payload.get("last_resistor_count", 0))
                    created = payload["created_variables"]
                    topology_label = topology_display_name(str(payload.get("topology", "single_boost")))
                    self.status.set(f"完成：{topology_label}，{count} 级 External Circuit 已回写 Maxwell。")
                    created_text = ", ".join(created.keys()) if created else "无（所需项目变量均已存在）"
                    if len(created_text) > 500:
                        created_text = created_text[:500] + "…"
                    messagebox.showinfo(
                        "External Circuit 建模完成",
                        f"拓扑：{topology_label}\n"
                        f"Winding 数量：{count}\n"
                        f"SW_V4：{count}\n"
                        f"VPWL：{vpwl_count}\n"
                        f"VPULSE：{vpulse_count}\n"
                        f"DIODE：{diode_count}\n"
                        f"线圈串联电阻：{resistor_count}（R=${self.coil_resistance_prefix}_n）\n"
                        f"位置电压源并联电阻：{shunt_count}（{self.pulse_shunt_resistance.get()}）\n"
                        f"储能电容：{storage_cap_count}（C=${self.capacitance_prefix}_n，IC=${self.initial_voltage_variable}）\n"
                        f"储能电容ESR：{storage_esr_count}（R=${self.esr_prefix}_n）\n"
                        f"末级二极管返回电阻：{last_resistor_count}（R=${self.last_resistance_variable}）\n"
                        f"共享开关模型：{self.switch_model_name.get()}\n"
                        f"共享二极管模型：{self.diode_model_name.get() if diode_count else '未使用'}\n"
                        f"Netlist：{payload['netlist_path']}\n\n"
                        f"本次新建项目变量：{created_text}\n\n"
                        f"动子位置提醒：\n{PROJECTILE_MOVE_REMINDER}",
                        parent=self.root,
                    )
                    return
        except queue.Empty:
            pass

        if self.worker is not None and self.worker.is_alive():
            self.root.after(100, self._poll_events)
        else:
            # A worker should always post done/error.  This fallback prevents a
            # stuck disabled button if a very unusual interpreter-level failure occurs.
            self.progress.stop()
            self.build_button.configure(state="normal")
            self.sync_button.configure(state="normal")


def main() -> None:
    raise RuntimeError("Run maxwell_accelerator_builder.pyw instead")


if __name__ == "__main__":
    main()
