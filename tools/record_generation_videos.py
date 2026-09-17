from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
from importlib.machinery import SourceFileLoader
import importlib.util
import logging
from pathlib import Path
import psutil
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "article_output" / "video_demo"
FFMPEG = Path(
    r"C:\Users\KCuO2\Documents\Codex\2026-08-31\https-github-com-ys-pan-yadof"
    r"\work\ffmpeg\ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe"
)
AEDT_VERSION = "2024.2"
VIDEO_FPS = 20
VIDEO_CRF = 18
MAX_CAPTURE_WIDTH = 1920
MAX_CAPTURE_HEIGHT = 1080
GUI_LEAD_SECONDS = 1.5
VIDEO_TAIL_SECONDS = 2.0
MOUSE_MOVE_SECONDS = 0.35
WINDOW_WAIT_SECONDS = 90.0
WORKFLOW_TIMEOUT_SECONDS = 600
LICENSE_VENDOR_PROCESS = "ansyslmd.exe"
MODEL_FRAME_NAME = "__RecordingFrame__"


sys.path.insert(0, str(ROOT))


def _load_builder() -> Any:
    loader = SourceFileLoader("recording_builder", str(ROOT / "maxwell_accelerator_builder.pyw"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


builder = _load_builder()

import tkinter as tk
from tkinter import ttk
import maxwell_circuit_toolkit.core as circuit_core
import maxwell_circuit_toolkit.gui as circuit_gui_module
from maxwell_circuit_toolkit.gui import CircuitBuilderGUI


OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
PROJECT_PATH = OUTPUT_DIR / f"LAG_GUI_Recorded_Build_{RUN_ID}.aedt"
MODEL_VIDEO = OUTPUT_DIR / f"LAG_GUI_Maxwell2D_Build_{RUN_ID}.mp4"
CIRCUIT_VIDEO = OUTPUT_DIR / f"LAG_GUI_Circuit_Build_{RUN_ID}.mp4"
LOG_PATH = ROOT / "logs" / f"record_generation_videos_{RUN_ID}.log"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="ascii", errors="backslashreplace"),
        logging.StreamHandler(),
    ],
)
LOGGER = logging.getLogger("record_generation_videos")


USER32 = ctypes.windll.user32
ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
SW_MAXIMIZE = 3
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_ALLCHILDREN = 0x0080
RDW_UPDATENOW = 0x0100
CREATE_NO_WINDOW = 0x08000000


ROOT_HWND = 0
AEDT_HWND = 0
AEDT_PROCESS_ID = 0
MODEL_CONFIG: Any = None
CIRCUIT_TOPOLOGY = "single_boost"
CURSOR_HIGHLIGHTER: Any = None


def _require_license_vendor() -> None:
    names = {
        str(process.info["name"] or "").casefold()
        for process in psutil.process_iter(["name"])
    }
    if LICENSE_VENDOR_PROCESS.casefold() not in names:
        raise RuntimeError(
            "The Ansys license vendor daemon is unavailable. Restore a valid "
            "Ansys license before recording."
        )


def _capture_rect() -> tuple[int, int, int, int]:
    screen_width = USER32.GetSystemMetrics(0)
    screen_height = USER32.GetSystemMetrics(1)
    width = min(MAX_CAPTURE_WIDTH, screen_width) & ~1
    height = min(MAX_CAPTURE_HEIGHT, screen_height, width * 9 // 16) & ~1
    width = min(width, height * 16 // 9) & ~1
    return ((screen_width - width) // 2) & ~1, ((screen_height - height) // 2) & ~1, width, height


def _window_area(hwnd: int) -> int:
    rect = wintypes.RECT()
    USER32.GetWindowRect(hwnd, ctypes.byref(rect))
    return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)


def _find_process_window(process_id: int) -> int:
    deadline = time.monotonic() + WINDOW_WAIT_SECONDS
    while time.monotonic() < deadline:
        candidates: list[int] = []

        @ENUM_WINDOWS_PROC
        def collect(hwnd: int, _lparam: int) -> bool:
            pid = wintypes.DWORD()
            USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == process_id and USER32.IsWindowVisible(hwnd):
                candidates.append(hwnd)
            return True

        USER32.EnumWindows(collect, 0)
        if candidates:
            return max(candidates, key=_window_area)
        time.sleep(0.25)
    raise TimeoutError(f"No visible window found for process {process_id}.")


def _activate_window(hwnd: int) -> None:
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    USER32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
    USER32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
    USER32.SetForegroundWindow(hwnd)


def _maximize_window(hwnd: int, *, activate: bool) -> None:
    USER32.ShowWindow(hwnd, SW_MAXIMIZE)
    if activate:
        _activate_window(hwnd)


def _refresh_aedt_window() -> None:
    USER32.RedrawWindow(
        AEDT_HWND,
        None,
        0,
        RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW,
    )


class CursorHighlighter:
    SIZE = 54
    TRANSPARENT_COLOR = "#010203"

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(bg=self.TRANSPARENT_COLOR)
        self.window.wm_attributes("-transparentcolor", self.TRANSPARENT_COLOR)
        self.canvas = tk.Canvas(
            self.window,
            width=self.SIZE,
            height=self.SIZE,
            bg=self.TRANSPARENT_COLOR,
            highlightthickness=0,
        )
        self.canvas.pack()
        self.shadow = self.canvas.create_oval(4, 4, self.SIZE - 4, self.SIZE - 4, outline="#202020", width=7)
        self.ring = self.canvas.create_oval(4, 4, self.SIZE - 4, self.SIZE - 4, outline="#ffd400", width=4)
        self.window.update_idletasks()
        hwnd = int(USER32.GetAncestor(self.window.winfo_id(), GA_ROOT) or self.window.winfo_id())
        style = USER32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        USER32.SetWindowLongW(
            hwnd,
            GWL_EXSTYLE,
            style | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE,
        )
        self._follow()

    def _follow(self) -> None:
        point = wintypes.POINT()
        USER32.GetCursorPos(ctypes.byref(point))
        self.move_to(point.x, point.y)
        self.root.after(16, self._follow)

    def move_to(self, x: int, y: int) -> None:
        half = self.SIZE // 2
        self.window.geometry(f"{self.SIZE}x{self.SIZE}+{x - half}+{y - half}")
        self.window.update_idletasks()

    def pulse(self) -> None:
        self.canvas.itemconfigure(self.ring, outline="#ff3b30", width=7)
        self.root.after(320, lambda: self.canvas.itemconfigure(self.ring, outline="#ffd400", width=4))


class ScreenRecorder:
    def __init__(self, output_path: Path) -> None:
        x, y, width, height = _capture_rect()
        self.output_path = output_path
        self._stderr = LOG_PATH.open("a", encoding="ascii", errors="backslashreplace")
        self._process = subprocess.Popen(
            [
                str(FFMPEG),
                "-y",
                "-f",
                "gdigrab",
                "-framerate",
                str(VIDEO_FPS),
                "-draw_mouse",
                "1",
                "-offset_x",
                str(x),
                "-offset_y",
                str(y),
                "-video_size",
                f"{width}x{height}",
                "-i",
                "desktop",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                str(VIDEO_CRF),
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr,
            creationflags=CREATE_NO_WINDOW,
        )
        time.sleep(0.5)
        if self._process.poll() is not None:
            raise RuntimeError(f"FFmpeg exited before recording {output_path}.")
        LOGGER.info("Recording %s at %sx%s.", output_path, width, height)

    def stop(self) -> None:
        if self._process.poll() is None:
            if self._process.stdin is None:
                raise RuntimeError("FFmpeg stdin is unavailable.")
            self._process.stdin.write(b"q\n")
            self._process.stdin.flush()
            self._process.wait(timeout=30)
        self._stderr.close()
        if self._process.returncode:
            raise subprocess.CalledProcessError(self._process.returncode, self._process.args)
        LOGGER.info("Saved video %s.", self.output_path)


def _frame_model_view(app: Any, config: Any) -> None:
    total_length = sum(coil.length_mm + coil.gap_after_mm for coil in config.coils)
    outer_radius = max(coil.outer_diameter_mm for coil in config.coils) / 2.0
    z_margin = max(10.0, total_length * 0.06)
    r_margin = max(5.0, outer_radius * 0.20)
    frame = app.modeler.create_rectangle(
        origin=["0mm", "0mm", f"{-z_margin}mm"],
        sizes=[f"{total_length + 2.0 * z_margin}mm", f"{outer_radius + r_margin}mm"],
        name=MODEL_FRAME_NAME,
        material="vacuum",
        non_model=True,
    )
    app.modeler.fit_all()
    frame.color = (255, 255, 255)
    frame.transparency = 1.0
    LOGGER.info(
        "Maxwell view framed once for %.3f mm axial length and %.3f mm radius.",
        total_length,
        outer_radius,
    )


def _frame_circuit_view(circuit: Any, topology: str) -> None:
    circuit.modeler.schematic_units = "mil"
    windings = circuit_core.get_winding_components(circuit)
    stage_indices = [
        circuit_core.winding_stage_index(getattr(winding, "name", "")) or index
        for index, winding in enumerate(windings, start=1)
    ]
    final_locations: list[list[float]] = []
    for row_number, stage_index in enumerate(stage_indices, start=1):
        if topology == circuit_core.TOPOLOGY_FILM_CAPACITOR_SCR:
            final_locations.append([1500 + (row_number - 1) * 3200, 3000])
            continue
        if topology == circuit_core.TOPOLOGY_ODD_EVEN_BOOST:
            chain_position = (stage_index - 1) // 2
            row_offset = 3600 if stage_index % 2 else 0
        else:
            chain_position = row_number - 1
            row_offset = 0
        final_locations.append([750 + chain_position * 2400, 2400 + row_offset])

    for winding, location in zip(windings, final_locations):
        winding.location = location
    if len(windings) > 1:
        windings[0].location = [300, 800]
        windings[-1].location = [
            max(location[0] for location in final_locations) + 2200,
            7200 if topology == circuit_core.TOPOLOGY_ODD_EVEN_BOOST else (
                4800 if topology == circuit_core.TOPOLOGY_FILM_CAPACITOR_SCR else 3700
            ),
        ]
    circuit.modeler.zoom_to_fit()
    for winding, location in zip(windings, final_locations):
        winding.location = location
    LOGGER.info("Circuit view framed once for %s stages.", len(windings))


def _click_widget(widget: tk.Widget) -> None:
    _activate_window(ROOT_HWND)
    widget.update_idletasks()
    target_x = widget.winfo_rootx() + widget.winfo_width() // 2
    target_y = widget.winfo_rooty() + widget.winfo_height() // 2
    point = wintypes.POINT()
    USER32.GetCursorPos(ctypes.byref(point))
    steps = max(1, int(MOUSE_MOVE_SECONDS * 60))
    for step in range(1, steps + 1):
        fraction = step / steps
        USER32.SetCursorPos(
            round(point.x + (target_x - point.x) * fraction),
            round(point.y + (target_y - point.y) * fraction),
        )
        if CURSOR_HIGHLIGHTER is not None:
            CURSOR_HIGHLIGHTER.move_to(
                round(point.x + (target_x - point.x) * fraction),
                round(point.y + (target_y - point.y) * fraction),
            )
        time.sleep(MOUSE_MOVE_SECONDS / steps)
    if CURSOR_HIGHLIGHTER is not None:
        CURSOR_HIGHLIGHTER.pulse()
    USER32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.10)
    USER32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    LOGGER.info("Clicked generator button at %s,%s.", target_x, target_y)


def main() -> None:
    global ROOT_HWND, AEDT_HWND, AEDT_PROCESS_ID, MODEL_CONFIG, CIRCUIT_TOPOLOGY, CURSOR_HIGHLIGHTER

    if not FFMPEG.is_file():
        raise FileNotFoundError(FFMPEG)
    _require_license_vendor()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    settings = builder.load_builder_settings()
    import ansys.aedt.core as aedt_core_module

    original_launch = builder.launch_maxwell2d
    original_maxwell2d = aedt_core_module.Maxwell2d
    original_create_circuit = circuit_core.create_external_circuit_design
    redraw_names = (
        "create_voltage_switch_model",
        "create_diode_model",
        "create_resistor",
        "create_storage_capacitor",
        "create_diode",
        "create_ground",
        "create_voltage_controlled_switch",
        "create_position_vpulse",
        "create_position_vpwl",
    )
    original_circuit_calls = {name: getattr(circuit_core, name) for name in redraw_names}
    original_wire = circuit_core._create_wire_points

    def redraw_after(original: Any) -> Any:
        def wrapped(circuit: Any, *args: Any, **kwargs: Any) -> Any:
            result = original(circuit, *args, **kwargs)
            _refresh_aedt_window()
            return result

        return wrapped

    def recording_wire(circuit: Any, *args: Any, **kwargs: Any) -> Any:
        result = original_wire(circuit, *args, **kwargs)
        _refresh_aedt_window()
        return result

    def launch_recording_aedt(*args: Any, **kwargs: Any) -> Any:
        global AEDT_HWND, AEDT_PROCESS_ID
        app = original_launch(*args, **kwargs)
        AEDT_PROCESS_ID = int(app.desktop_class.aedt_process_id)
        AEDT_HWND = _find_process_window(AEDT_PROCESS_ID)
        _maximize_window(AEDT_HWND, activate=False)
        _activate_window(ROOT_HWND)
        _frame_model_view(app, MODEL_CONFIG)
        _activate_window(AEDT_HWND)
        return app

    def create_recording_circuit(*args: Any, **kwargs: Any) -> Any:
        maxwell_app = args[0] if args else kwargs["maxwell_app"]
        _activate_window(ROOT_HWND)
        if MODEL_FRAME_NAME in maxwell_app.modeler.object_names:
            maxwell_app.modeler.delete(MODEL_FRAME_NAME)
            maxwell_app.save_project(str(PROJECT_PATH))
            LOGGER.info("Removed the temporary non-model view frame.")
        circuit = original_create_circuit(*args, **kwargs)
        _activate_window(ROOT_HWND)
        _frame_circuit_view(circuit, CIRCUIT_TOPOLOGY)
        _maximize_window(AEDT_HWND, activate=True)
        return circuit

    def bound_maxwell2d(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("aedt_process_id", AEDT_PROCESS_ID)
        return original_maxwell2d(*args, **kwargs)

    builder.launch_maxwell2d = launch_recording_aedt
    circuit_core.create_external_circuit_design = create_recording_circuit
    for name, original in original_circuit_calls.items():
        setattr(circuit_core, name, redraw_after(original))
    circuit_core._create_wire_points = recording_wire

    root = tk.Tk()
    root.title("Maxwell \u76f4\u7ebf\u7535\u78c1\u52a0\u901f\u5668\u5efa\u6a21\u4e0e\u7535\u8def\u751f\u6210\u5668")
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    model_page = ttk.Frame(notebook)
    circuit_page = ttk.Frame(notebook)
    notebook.add(model_page, text="Maxwell2D \u5efa\u6a21\u5668")
    notebook.add(circuit_page, text="External Circuit \u751f\u6210\u5668")
    model_gui = builder.AcceleratorBuilderGUI(model_page, settings=settings, manage_window=False)
    circuit_gui = CircuitBuilderGUI(
        circuit_page,
        settings=settings,
        config_path=builder.CONFIG_PATH,
        log_dir=builder.LOG_DIR,
        diode_parameters=builder.diode_model_parameters(settings),
        project_path=model_gui.project_path,
        maxwell_design=model_gui.design_name,
        aedt_version=model_gui.aedt_version,
        non_graphical=model_gui.non_graphical,
        manage_window=False,
    )
    model_gui.project_path.set(str(PROJECT_PATH))
    model_gui.aedt_version.set(AEDT_VERSION)
    model_gui.non_graphical.set(False)
    CIRCUIT_TOPOLOGY = circuit_gui.topology_by_label[circuit_gui.topology_display.get()]
    MODEL_CONFIG = model_gui._collect_config()

    root.update_idletasks()
    ROOT_HWND = int(USER32.GetAncestor(root.winfo_id(), GA_ROOT) or root.winfo_id())
    _maximize_window(ROOT_HWND, activate=True)
    root.update_idletasks()
    CURSOR_HIGHLIGHTER = CursorHighlighter(root)

    failures: list[BaseException] = []
    recorder: ScreenRecorder | None = ScreenRecorder(MODEL_VIDEO)

    def fail(error: BaseException) -> None:
        nonlocal recorder
        if failures:
            return
        failures.append(error)
        if recorder is not None:
            recorder.stop()
            recorder = None
        root.after(0, root.destroy)

    def finish() -> None:
        nonlocal recorder
        if recorder is not None:
            recorder.stop()
            recorder = None
        LOGGER.info("PROJECT=%s", PROJECT_PATH)
        LOGGER.info("MODEL_VIDEO=%s", MODEL_VIDEO)
        LOGGER.info("CIRCUIT_VIDEO=%s", CIRCUIT_VIDEO)
        root.destroy()

    def circuit_info(_title: str, _message: str, **_kwargs: Any) -> None:
        circuit_gui.status.set("External Circuit recording complete.")
        root.after(round(VIDEO_TAIL_SECONDS * 1000), finish)

    def circuit_error(title: str, message: str, **_kwargs: Any) -> None:
        fail(RuntimeError(f"{title}: {message}"))

    def start_circuit_phase() -> None:
        nonlocal recorder
        if recorder is not None:
            recorder.stop()
        notebook.select(circuit_page)
        root.update_idletasks()
        _maximize_window(ROOT_HWND, activate=True)
        aedt_core_module.Maxwell2d = bound_maxwell2d
        circuit_gui_module.messagebox.showinfo = circuit_info
        circuit_gui_module.messagebox.showerror = circuit_error
        recorder = ScreenRecorder(CIRCUIT_VIDEO)
        root.after(round(GUI_LEAD_SECONDS * 1000), lambda: _click_widget(circuit_gui.build_button))

    def model_success(config: Any, _resistances: list[float]) -> None:
        model_gui.status.set(f"Complete: {config.project_path}")
        LOGGER.info("Maxwell model generation completed from the GUI button.")
        root.after(round(VIDEO_TAIL_SECONDS * 1000), start_circuit_phase)

    def model_error(_config: Any, traceback_text: str) -> None:
        meaningful = [line.strip() for line in traceback_text.splitlines() if line.strip()]
        fail(RuntimeError(meaningful[-1] if meaningful else "Maxwell model generation failed."))

    model_gui._show_build_success = model_success
    model_gui._show_build_error = model_error

    root.after(round(GUI_LEAD_SECONDS * 1000), lambda: _click_widget(model_gui.build_button))
    root.after(
        WORKFLOW_TIMEOUT_SECONDS * 1000,
        lambda: fail(TimeoutError(f"Recording exceeded {WORKFLOW_TIMEOUT_SECONDS} seconds.")),
    )
    LOGGER.info("Starting GUI-driven graphical AEDT recording run %s.", RUN_ID)
    try:
        root.mainloop()
    finally:
        builder.launch_maxwell2d = original_launch
        aedt_core_module.Maxwell2d = original_maxwell2d
        circuit_core.create_external_circuit_design = original_create_circuit
        for name, original in original_circuit_calls.items():
            setattr(circuit_core, name, original)
        circuit_core._create_wire_points = original_wire
        if recorder is not None:
            recorder.stop()
    if failures:
        raise failures[0]


if __name__ == "__main__":
    main()
