import ctypes
from ctypes import wintypes
from pathlib import Path
import time

from PIL import ImageGrab
import psutil


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "article_output" / "video_demo" / "qa_live_aedt.png"
USER32 = ctypes.windll.user32
ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def find_window() -> int:
    process_ids = {
        process.info["pid"]
        for process in psutil.process_iter(["pid", "name"])
        if str(process.info["name"] or "").casefold() == "ansysedt.exe"
    }
    windows = []

    @ENUM_WINDOWS_PROC
    def collect(hwnd: int, _lparam: int) -> bool:
        pid = wintypes.DWORD()
        USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in process_ids and USER32.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            USER32.GetWindowRect(hwnd, ctypes.byref(rect))
            windows.append((max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top), hwnd))
        return True

    USER32.EnumWindows(collect, 0)
    return max(windows)[1]


hwnd = find_window()
USER32.ShowWindow(hwnd, 3)
USER32.SetForegroundWindow(hwnd)
time.sleep(1)
USER32.keybd_event(0x1B, 0, 0, 0)
USER32.keybd_event(0x1B, 0, 2, 0)
time.sleep(0.5)
for x, y, delay in [(566, 727, 1)]:
    USER32.SetCursorPos(x, y)
    USER32.mouse_event(0x0002, 0, 0, 0, 0)
    USER32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(delay)
ImageGrab.grab(all_screens=True).save(OUTPUT)
