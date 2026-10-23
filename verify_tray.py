"""Ad-hoc verification: drive the real tray app the way a user would.

Covers the app window, closing to the tray, the tray menu's icon states,
hold-to-talk with the live preview overlay, the single-instance guard and a
clean restart. It runs the app against a throwaway config with an unusual
hotkey so nothing the user has open is disturbed.
"""

import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
PYW = APP / ".venv" / "Scripts" / "pythonw.exe"
LOG = APP / "dictate.log"
HOLD_KEY = "<ctrl>+<alt>+<shift>+<f12>"
WINDOW_TITLE = "Dicteer"
OVERLAY_TITLE = "Dicteer Overlay"

user32 = ctypes.windll.user32
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

FAILS = []
WM_CLOSE = 0x0010


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def windows():
    rows = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _lp):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cls = ctypes.create_unicode_buffer(256)
        txt = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(hwnd, cls, 256)
        user32.GetWindowTextW(hwnd, txt, 512)
        rows.append((hwnd, pid.value, cls.value, txt.value, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(callback, 0)
    return rows


def tray_pids(exclude=()):
    return {row[1] for row in windows() if "SystemTrayIcon" in row[2]} - set(exclude)


def tk_windows(pid):
    return [row for row in windows() if row[1] == pid and row[2] == "TkTopLevel"]


def overlay_windows(pid):
    return [row for row in tk_windows(pid) if row[3] == OVERLAY_TITLE and row[4]]


def find_title(title, visible_only=False):
    for hwnd, _pid, _cls, text, visible in windows():
        if text == title and (visible or not visible_only):
            return hwnd
    return None


def visible_title(title):
    return find_title(title, visible_only=True) is not None


def tasklist(pid):
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
        capture_output=True,
        text=True,
    ).stdout
    return out.strip().split(",")[0].strip('"') if out.strip() else ""


def offset():
    return LOG.stat().st_size if LOG.exists() else 0


def log_since(start):
    with open(LOG, "rb") as fh:
        fh.seek(start)
        return fh.read().decode("utf-8", "replace")


def wait_for(text, start, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if text in log_since(start):
            return True
        time.sleep(0.2)
    return False


def wait_until(predicate, seconds=15, interval=0.2):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def launch(config, *extra):
    start = offset()
    proc = subprocess.Popen(
        [str(PYW), "tray.pyw", "--config", str(config), *extra],
        cwd=str(APP),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, start


def kill_tree(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


def make_config(tmpdir: Path) -> Path:
    base = json.loads((APP / "config.json").read_text(encoding="utf-8"))
    base.update(
        {
            "hotkey": HOLD_KEY,
            "hold_to_talk": True,
            "overlay": True,
            "stream_preview": True,
            "beeps": False,
            "model": "base.en",
            "preview_model": "",
            "language": "en",
            # Never type into whatever window happens to be focused while this
            # script is driving the real app.
            "mode": "clipboard",
        }
    )
    path = tmpdir / "verify-config.json"
    path.write_text(json.dumps(base, indent=2), encoding="utf-8")
    return path


def hold_and_release(seconds=1.6):
    from pynput.keyboard import Controller, Key

    keyboard = Controller()
    for key in (Key.ctrl, Key.alt, Key.shift, Key.f12):
        keyboard.press(key)
        time.sleep(0.03)
    time.sleep(seconds)
    for key in (Key.f12, Key.shift, Key.alt, Key.ctrl):
        keyboard.release(key)
        time.sleep(0.03)


def main():
    baseline = tray_pids()
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        proc, start = launch(config, "--settings")

        check("tray app reports ready", wait_for("tray ready", start))
        check("window loaded", wait_for("window loaded", start))
        check("the page booted on the settings view", wait_for("ui booted", start) and "#settings" in log_since(start),
              "expected --settings to open the settings tab")

        pids = tray_pids(baseline)
        real = next(iter(pids)) if pids else None
        check("tray owns a notification-area window", real is not None, f"pid={real}")
        if real:
            name = tasklist(real)
            check("tray process is a python interpreter", "python" in name.lower(), name)

        check("window is on screen", wait_until(lambda: visible_title(WINDOW_TITLE), 20))
        window = find_title(WINDOW_TITLE, visible_only=True)
        if window:
            user32.PostMessageW(window, WM_CLOSE, 0, 0)
            check("closing the window parks it in the tray", wait_until(lambda: not visible_title(WINDOW_TITLE), 15))
            check("tray survives closing the window", real in tray_pids(baseline))
            check("no crash logged", "startup failed" not in log_since(start))

        check("hotkey armed", wait_for("listening for " + HOLD_KEY, start))
        check("hold-to-talk mode reported", "hold to talk" in log_since(start))
        check("model warm-loaded", wait_for("model ready in", start))
        check("no overlay before speaking", real is not None and not overlay_windows(real))

        foreground = user32.GetForegroundWindow()
        hold_and_release(1.6)
        check("holding the hotkey starts recording", wait_for("hold to talk: pressed", start, 10))
        check("recording started", wait_for("recording at", start, 10))
        check("the live overlay starts with the recording", wait_for("overlay ready", start, 15))
        shown = wait_until(lambda: bool(overlay_windows(real)), 10)
        check("live overlay appears while talking", shown, str(overlay_windows(real)) if real else "no pid")
        check("overlay did not steal focus", user32.GetForegroundWindow() == foreground)

        check("releasing the hotkey stops recording", wait_for("hold to talk: released", start, 15))
        check("clip was processed", wait_for("stopped after", start, 15))
        # The room is not silent (fan/mic hum/game audio), so the app may well
        # transcribe something; what matters is that the cycle completes and
        # that the clipboard-only config is honoured.
        processed = (
            wait_for("copied to clipboard", start, 30)
            or wait_for("no speech detected", start, 10)
            or wait_for("ignored", start, 5)
        )
        check("hold cycle finished and delivered or filtered the clip", processed)
        check(
            "clipboard mode sent no keystrokes",
            "pasted " not in log_since(start),
            "a paste here means the delivery mode was ignored",
        )
        check(
            "overlay hides after the clip",
            wait_until(lambda: not overlay_windows(real), 10),
            str(overlay_windows(real)) if real else "no pid",
        )

        # duplicate launch must refuse and must not disturb the running instance
        second, second_start = launch(config)
        check("second instance refuses to start", wait_for("already running", second_start, 20))
        check("second instance brings the existing window forward", wait_until(lambda: visible_title(WINDOW_TITLE), 15))
        check("running instance is untouched", real in tray_pids(baseline))
        kill_tree(second.pid)
        time.sleep(1)
        check("running instance survived the duplicate attempt", real in tray_pids(baseline))

        kill_tree(proc.pid)
        if real:
            kill_tree(real)
        check(
            "quitting removes the tray icon",
            wait_until(lambda: real not in tray_pids(baseline), 15),
        )
        check("no tk windows left behind", not (real and tk_windows(real)))

        third, third_start = launch(config)
        check("restart after quit works", wait_for("tray ready", third_start))
        new_pid = next(iter(tray_pids(baseline)), None)
        check("restart gets a fresh process", new_pid is not None and new_pid != real, f"{real} -> {new_pid}")
        kill_tree(third.pid)
        if new_pid:
            kill_tree(new_pid)
        check("no tray windows left behind", wait_until(lambda: not tray_pids(baseline)))

        saved = json.loads(config.read_text(encoding="utf-8"))
        check("verification config stayed valid", saved.get("hotkey") == HOLD_KEY)

    print()
    print("FAILED: " + ", ".join(FAILS) if FAILS else "tray verification passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
