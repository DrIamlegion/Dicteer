#!/usr/bin/env python3
"""The application window.

pywebview insists on the main thread, while the tray icon needs its own message
loop, so the process is arranged as: main thread runs this window, a worker
thread runs the tray, and the dictation engine lives in its own threads.
Everything the page can do goes through :class:`Api`; everything the page must
react to arrives as ``window.dictate.push({type, value})``.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import math
import threading
import time
from pathlib import Path

import autostart
import cloud as cloud_mod
import history as history_mod
import settings as settings_mod

log = logging.getLogger("dictate")

UI_DIR = Path(__file__).resolve().parent / "ui"
ICON = Path(__file__).resolve().parent / "assets" / "icon.ico"
TITLE = "Dicteer"
APP_VERSION = "2.0.12"

DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 760
MIN_WIDTH = 900
MIN_HEIGHT = 620

_user32 = ctypes.windll.user32
_user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_user32.MoveWindow.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.BOOL]
_user32.FindWindowW.restype = wt.HWND
_user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]

SPI_GETWORKAREA = 0x0030


# --------------------------------------------------------------------------
# placement
# --------------------------------------------------------------------------
def work_area() -> tuple[int, int, int, int]:
    """The usable desktop rectangle, i.e. without the taskbar."""
    rect = wt.RECT()
    try:
        if _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            if rect.right - rect.left > 200 and rect.bottom - rect.top > 200:
                return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, 1920, 1080


def _int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


class Placement:
    """Remembers where the window was, and decides where it opens.

    Deliberately stored next to config.json rather than in the app folder, so a
    throwaway config (used by the tests) also gets throwaway geometry.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self._read()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        except Exception as exc:
            log.debug("could not store window placement: %s", exc)

    def resolve(self) -> dict:
        left, top, right, bottom = work_area()
        avail_w, avail_h = right - left, bottom - top
        width = max(MIN_WIDTH, min(_int(self.data.get("width"), DEFAULT_WIDTH), avail_w))
        height = max(MIN_HEIGHT, min(_int(self.data.get("height"), DEFAULT_HEIGHT), avail_h))
        x, y = self.data.get("x"), self.data.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            x = left + (avail_w - width) // 2
            y = top + max(0, (avail_h - height) // 2 - 20)
        # Never open mostly off-screen, e.g. after unplugging a monitor.
        x = max(left - 40, min(x, right - 160))
        y = max(top - 10, min(y, bottom - 120))
        return {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "maximized": bool(self.data.get("maximized")),
        }

    def store(self, window=None) -> None:
        """Save the real rectangle of the live window (Windows titlebar included)."""
        hwnd = _user32.FindWindowW(None, TITLE)
        if not hwnd:
            return
        rect = wt.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return
        current = dict(self.data)
        minimized = bool(_user32.IsIconic(hwnd))
        maximized = bool(_user32.IsZoomed(hwnd))
        current["maximized"] = maximized
        if not minimized and not maximized:
            current.update(
                {
                    "x": rect.left,
                    "y": rect.top,
                    "width": max(MIN_WIDTH, rect.right - rect.left),
                    "height": max(MIN_HEIGHT, rect.bottom - rect.top),
                }
            )
        self._write(current)
        self.data = current
        if maximized:
            log.debug("window placement stored (maximized)")
        else:
            log.debug("window placement stored %sx%s at %s,%s", current.get("width"), current.get("height"), current.get("x"), current.get("y"))


# --------------------------------------------------------------------------
# bridge
# --------------------------------------------------------------------------
class Api:
    """Methods callable from JavaScript, always returning plain data."""

    def __init__(self, shell):
        self.shell = shell
        # pywebview discovers exposed methods by walking every attribute of the
        # js_api object. Without this marker it crawls the whole engine, the
        # window's native handles and their .NET object graph, which is slow and
        # drowns the log in recursion errors (and would expose far too much).
        setattr(shell, "_serializable", False)

    def boot(self) -> dict:
        log.info("ui requested boot data")
        return self.shell.boot_payload()

    def dismiss_storage_warnings(self) -> dict:
        self.shell.dismiss_storage_warnings()
        return {"ok": True}

    def log_js(self, message: str) -> dict:
        log.warning("window javascript: %s", str(message)[:500])
        return {"ok": True}

    def toggle_record(self) -> dict:
        self.shell.dictator.toggle()
        return {"state": self.shell.dictator.state}

    def set_listening(self, enabled: bool) -> dict:
        return self.shell.set_listening(bool(enabled))

    def validate_hotkey(self, hotkey: str) -> dict:
        try:
            import dictate as app

            app.validate_hotkey(str(hotkey or "").strip())
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "hotkey": str(hotkey).strip()}

    def test_microphone(self, seconds: float = 2.0, device=None) -> dict:
        """Record a short clip and report how loud the selected input is.

        ``device`` lets the settings page test the microphone that is currently
        selected in the form, before it has been saved.
        """
        import dictate as app

        if self.shell.dictator.state != "idle":
            return {"ok": False, "error": "Stop eerst de opname voordat je de microfoon test."}
        try:
            seconds = max(1.0, min(5.0, float(seconds or 2.0)))
        except (TypeError, ValueError):
            seconds = 2.0
        if isinstance(device, str) and device.strip().lower() in (
            "default",
            "standaard",
            "systeemstandaard",
        ):
            # The form asked for Windows' current default, not the saved one.
            device = None
        elif device in ("", "null", None):
            device = self.shell.cfg.get("input_device")

        recorder = None
        try:
            recorder = app.Recorder(app.resolve_device(device, fallback=False))
            recorder.start()
            levels: list[float] = []
            deadline = time.time() + seconds
            while time.time() < deadline:
                time.sleep(0.1)
                levels.append(recorder.level())
            recorder.stop()
        except Exception as exc:
            log.warning("microphone test failed: %s", exc)
            if recorder is not None:
                try:
                    recorder.stop()
                except Exception:
                    pass
            return {"ok": False, "error": f"Microfoon niet bereikbaar: {exc}"}

        peak = max(levels) if levels else 0.0
        dbfs = round(20.0 * math.log10(peak), 1) if peak > 0 else -90.0
        if peak >= 0.05:
            verdict = "goed"
        elif peak >= 0.008:
            verdict = "zacht"
        else:
            verdict = "stil"
        log.info("microphone test: peak %.4f (%.1f dBFS), verdict %s", peak, dbfs, verdict)
        device = recorder.device
        try:
            import sounddevice as sd

            device = sd.query_devices(device if device is not None else sd.default.device[0], "input")["name"]
        except Exception:
            pass
        return {
            "ok": True,
            "peak": round(peak, 4),
            "dbfs": dbfs,
            "verdict": verdict,
            "device": str(device),
            "seconds": round(seconds, 1),
        }

    def save_settings(self, updates: dict) -> dict:
        try:
            new_cfg = settings_mod.apply_updates(self.shell.cfg, updates or {})
            backend = str(new_cfg.get("online_backend") or "local")
            if backend != "local" and not self.shell.credentials.has(backend):
                name = cloud_mod.PROVIDERS[backend]["name"]
                return {
                    "ok": False,
                    "error": f"Sla eerst je gratis {name}-sleutel op voordat je online gaat.",
                }
            settings_mod.save(self.shell.config_path, new_cfg)
            self.shell.apply_settings(new_cfg)
        except settings_mod.SettingsError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # a broken write must not kill the window
            log.error("saving settings failed: %s", exc)
            return {"ok": False, "error": f"Opslaan mislukt: {exc}"}
        log.info("settings saved from the window")
        return {"ok": True, "settings": dict(new_cfg), "cloud": self.shell.cloud_status()}

    def save_cloud_key(self, key: str, provider: str | None = None) -> dict:
        return self.shell.save_cloud_key(key, provider)

    def clear_cloud_key(self, provider: str | None = None) -> dict:
        return self.shell.clear_cloud_key(provider)

    def cloud_status(self) -> dict:
        return self.shell.cloud_status()

    def set_autostart(self, enabled: bool) -> dict:
        try:
            if enabled:
                autostart.enable()
            else:
                autostart.disable()
            return {"ok": True, "enabled": autostart.is_enabled()}
        except Exception as exc:
            log.error("autostart change failed: %s", exc)
            return {"ok": False, "error": str(exc), "enabled": autostart.is_enabled()}

    def copy(self, text: str) -> dict:
        try:
            import pyperclip

            pyperclip.copy(text or "")
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_history(self, item_id: str) -> dict:
        self.shell.history.delete(item_id)
        return self.shell.history_payload()

    def correct_history(self, item_id: str, text: str) -> dict:
        return self.shell.correct_history(item_id, text)

    def forget_learning(self, kind: str, value: str) -> dict:
        return self.shell.forget_learning(kind, value)

    def add_vocabulary(self, word: str) -> dict:
        return self.shell.add_vocabulary(word)

    def clear_learning(self) -> dict:
        return self.shell.clear_learning()

    def clear_history(self) -> dict:
        self.shell.history.clear()
        return self.shell.history_payload()

    def list_devices(self) -> dict:
        return {"devices": self.shell.devices()}

    def open_log(self) -> dict:
        self.shell.open_path(self.shell.log_path)
        return {"ok": True}

    def open_crash_log(self) -> dict:
        return self.shell.open_crash_log()

    def open_folder(self) -> dict:
        self.shell.open_path(self.shell.app_dir)
        return {"ok": True}

    def check_updates(self) -> dict:
        """Ask GitHub for the newest release; only the Over view calls this."""
        import updates

        return updates.check(APP_VERSION)

    def open_release(self, url: str) -> dict:
        """Open a release page in the browser, and only ours."""
        import updates

        if not updates.is_release_url(url):
            return {"ok": False, "error": "Onbekende downloadlink."}
        self.shell.open_path(url)
        return {"ok": True}

    def hide_window(self) -> dict:
        self.shell.ui.hide()
        return {"ok": True}

    def quit_app(self) -> dict:
        threading.Thread(target=self.shell.quit, daemon=True).start()
        return {"ok": True}


class WebUI:
    """Owns the window and forwards engine events into the page."""

    def __init__(self, shell, hidden: bool = False, initial_view: str | None = None):
        self.shell = shell
        self.hidden = hidden
        self.initial_view = initial_view
        self.window = None
        self.visible = False
        self.loaded = False
        self.placed = False
        self._allow_close = False
        self._last_level = 0.0
        self._pending: list[dict] = []
        self._pending_lock = threading.Lock()
        self.placement = Placement(Path(shell.config_path).with_name("ui-state.json"))
        self._geometry = self.placement.resolve()
        # The tests reuse the real page against a throwaway config; keeping the
        # geometry with that config keeps the user's own placement untouched.
        self._maximize_pending = self._geometry["maximized"] and not hidden

    # -- lifecycle --------------------------------------------------------
    def create(self) -> None:
        import webview

        geometry = self._geometry
        # The starting view belongs in the URL: calling show('settings') before
        # webview.start()/the page load would be dropped, and the page would open
        # on Dicteren instead.
        url = str(UI_DIR / "index.html")
        if self.initial_view:
            url += f"#{self.initial_view}"
        self.window = webview.create_window(
            TITLE,
            url,
            js_api=Api(self.shell),
            width=geometry["width"],
            height=geometry["height"],
            x=geometry["x"],
            y=geometry["y"],
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            background_color="#0b1120",
            text_select=True,
            hidden=self.hidden,
        )
        self.window.events.loaded += self._on_loaded
        self.window.events.shown += self._on_shown
        self.window.events.restored += self._on_restored
        self.window.events.maximized += self._on_maximized
        # pywebview has no 'hidden' event; hide() updates the shell directly and
        # minimising is the other way the window can leave the screen.
        self.window.events.minimized += self._on_hidden
        self.window.events.closing += self._on_closing

    def run(self) -> None:
        import webview

        webview.start(
            debug=bool(self.shell.debug),
            private_mode=True,
            icon=str(ICON) if ICON.exists() else None,
        )

    # -- events -----------------------------------------------------------
    def _on_loaded(self) -> None:
        self.loaded = True
        log.info("window loaded")
        # Push the initial state instead of making the page call into Python on
        # startup: an early bridge call races pywebview's own initialisation.
        self.emit({"type": "boot", "value": self.shell.boot_payload()})
        with self._pending_lock:
            pending, self._pending = self._pending, []
        for event in pending:
            self.emit(event)

    def _on_shown(self) -> None:
        self.visible = True
        self.shell.on_window_visibility(True)
        if self._maximize_pending:
            self._maximize_pending = False
            try:
                self.window.maximize()
            except Exception as exc:
                log.debug("could not maximise: %s", exc)

    def _on_restored(self) -> None:
        self.visible = True
        self._geometry["maximized"] = False
        self.shell.on_window_visibility(True)

    def _on_maximized(self) -> None:
        self.visible = True
        self._geometry["maximized"] = True
        self.shell.on_window_visibility(True)

    def _on_hidden(self) -> None:
        self.visible = False
        self.shell.on_window_visibility(False)

    def _on_closing(self) -> bool:
        if self._allow_close:
            return True
        # Keep the placement before hiding: the rectangle is still valid here.
        self.placement.store()
        # Closing the window parks the app in the tray; dictation keeps working.
        self.hide()
        self.shell.hint_tray()
        return False

    # -- actions ----------------------------------------------------------
    def show(self, view: str | None = None) -> None:
        if self.window is None:
            return
        try:
            self.window.show()
            try:
                self.window.restore()
            except Exception:
                pass
            if view:
                self.window.evaluate_js(f"location.hash = '#{view}'")
        except Exception as exc:
            log.debug("could not show window: %s", exc)
        self.visible = True
        self.shell.on_window_visibility(True)

    def hide(self) -> None:
        if self.window is not None:
            try:
                self.window.hide()
            except Exception as exc:
                log.debug("could not hide window: %s", exc)
        self.visible = False
        self.shell.on_window_visibility(False)

    def destroy(self) -> None:
        self.placement.store()
        self._allow_close = True
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception as exc:
                log.debug("could not destroy window: %s", exc)

    # -- python -> js -----------------------------------------------------
    def emit(self, event: dict) -> None:
        if not self.loaded or self.window is None:
            with self._pending_lock:
                if len(self._pending) > 40:
                    self._pending = self._pending[-40:]
                self._pending.append(event)
            return
        try:
            payload = json.dumps(event, ensure_ascii=False)
            self.window.evaluate_js(f"window.dictate && window.dictate.push({payload})")
        except Exception as exc:
            log.debug("could not push %s to the window: %s", event.get("type"), exc)

    def emit_level(self, rms: float) -> None:
        now = time.time()
        if now - self._last_level < 0.08:
            return
        self._last_level = now
        self.emit({"type": "level", "value": min(1.0, max(0.0, float(rms) * 5.0))})

    def emit_state(self, state: str) -> None:
        self.emit({"type": "state", "value": state, "listening": self.shell.dictator.listening})

    def emit_listening(self, listening: bool) -> None:
        self.emit({"type": "listening", "value": bool(listening)})

    def emit_partial(self, text: str) -> None:
        self.emit({"type": "partial", "value": text})

    def emit_history(self, items: list[dict]) -> None:
        self.emit({"type": "history", "value": items})

    def emit_settings(self, cfg: dict) -> None:
        self.emit({"type": "settings", "value": cfg})

    def emit_toast(self, text: str, kind: str = "ok") -> None:
        self.emit({"type": "toast", "value": {"text": text, "kind": kind}})

    def emit_storage_warnings(self, warnings: list[str]) -> None:
        self.emit({"type": "storage_warnings", "value": list(warnings)})

    def emit_delivered(self, item: dict, items: list[dict], stats: dict) -> None:
        payload = dict(item)
        payload["history"] = items
        payload["stats"] = stats
        self.emit({"type": "delivered", "value": payload})
