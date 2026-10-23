#!/usr/bin/env pythonw
"""Dicteer - application shell.

Main thread: the window (pywebview). Worker thread: the tray icon. Engine
threads: hotkey listener, preview pass, transcription. The window and the tray
share one :class:`dictate.Dictator`, so whichever control you use, the state is
the same.

    pythonw tray.pyw              start with the window
    pythonw tray.pyw --hidden     start in the tray (used at login)
    pythonw tray.pyw --settings   open on the settings tab
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

import autostart
import cloud as cloud_mod
import crashlog
import desktop
import dictate as app
import history as history_mod
import learning as learning_mod
import settings as settings_mod

log = logging.getLogger("dictate")

SIZE = 64
COLORS = {
    "off": "#94a3b8",        # hotkey disabled
    "idle": "#3b82f6",       # armed, waiting
    "recording": "#ef4444",  # capturing audio
    "transcribing": "#f59e0b",
}


def mic_image(color: str):
    """A small microphone glyph in the given colour, drawn from scratch."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((23, 7, 41, 37), radius=9, fill=color)
    draw.arc((14, 17, 50, 49), start=0, end=180, fill=color, width=4)
    draw.line((32, 48, 32, 56), fill=color, width=4)
    draw.line((22, 56, 42, 56), fill=color, width=4)
    return image


class Shell:
    def __init__(
        self,
        cfg: dict,
        config_path: Path,
        debug: bool = False,
        crash_notice: dict | None = None,
    ):
        self.cfg = cfg
        self.config_path = config_path
        self.debug = debug
        # Where config, history, models and logs live: the checkout folder, or
        # %APPDATA%\Dicteer in the installed build.
        self.data_dir = app.DATA_DIR
        self.log_path = app.LOG_PATH
        self.crash_log_path = Path(config_path).with_name("crash.log")
        self.crash_notice = dict(crash_notice or {})
        self.ui: desktop.WebUI | None = None
        # Storage warnings persist in the window until the user dismisses
        # them. Read failures arrive before the window exists; write failures
        # can be appended while it is running.
        self._storage_warnings: list[str] = []

        self.dictator = app.Dictator(cfg, argparse.Namespace(no_paste=False))
        # History and learning live with the config that produced them. With the
        # default config that is the app folder; the verification runs point at
        # a throwaway config and therefore cannot touch the user's dictations.
        data_dir = Path(config_path).resolve().parent
        self.history = history_mod.HistoryStore(
            data_dir / "history.json", on_error=self._storage_error
        )
        # The optional online key lives with the config too, encrypted by
        # Windows DPAPI. A throwaway config therefore also isolates its key.
        self.credentials = cloud_mod.CredentialStore(data_dir / "credentials.json")
        self.dictator.cloud = cloud_mod.CloudTranscriber(self.credentials)
        self.dictator.on_notice = self.on_notice
        # Corrections are learned from edits in the window; the hooks make the
        # engine use them without the engine knowing anything about the UI.
        self.learning = learning_mod.LearningStore(
            data_dir / "learning.json", on_error=self._storage_error
        )
        self.dictator.postprocess = self.learning.apply_text
        self.dictator.extra_prompt = self.learning.prompt_extra
        for message in (self.history.read_error, self.learning.read_error):
            if message:
                self._storage_error(message)

        self.icon = None
        self.overlay = None
        self.window_visible = False
        self._images: dict[str, object] = {}
        self._overlay_generation = 0
        self._last_partial = ""
        self._tray_hint_shown = False
        self._quitting = False

        self.dictator.on_state = self.on_state
        self.dictator.on_partial = self.on_partial
        self.dictator.on_level = self.on_level
        self.dictator.on_delivered = self.on_delivered

    # -- helpers ----------------------------------------------------------
    def hotkey_label(self) -> str:
        return str(self.cfg.get("hotkey", "<f9>")).replace("<", "").replace(">", "").upper()

    def devices(self) -> list[dict]:
        """The Windows microphones, with a stable name-based id."""
        return app.enumerate_input_devices()

    def boot_payload(self) -> dict:
        """Everything the window needs to render itself."""
        return {
            "state": self.dictator.state,
            "listening": self.dictator.listening,
            "settings": dict(self.cfg),
            "history": self.history.items(),
            "stats": self.stats(),
            "learning": self.learning.payload(),
            "storage_warnings": list(self._storage_warnings),
            "devices": self.devices(),
            "cloud": self.cloud_status(),
            "autostart": autostart.is_enabled(),
            "hotkey_label": self.hotkey_label(),
            "version": desktop.APP_VERSION,
            "models": settings_mod.MODELS,
            "model_dir": str(app.MODEL_DIR),
            "config_path": str(self.config_path),
            "crash_log_path": str(self.crash_log_path),
            "crash": dict(
                self.crash_notice
                or {"pending": False, "path": str(self.crash_log_path)}
            ),
        }

    def stats(self) -> dict:
        return history_mod.stats(self.history.items())

    def cloud_status(self) -> dict:
        """What the window may know about the online key: never its value."""
        backend = str(self.cfg.get("online_backend") or "local").strip().lower()
        return {
            "backend": backend if backend in settings_mod.ONLINE_BACKENDS else "local",
            "provider": cloud_mod.PROVIDER,
            "has_key": self.credentials.has(cloud_mod.PROVIDER),
            "model": cloud_mod.GROQ_MODEL,
            "free_limits": cloud_mod.FREE_LIMITS,
            "keys_url": cloud_mod.GROQ_KEYS_URL,
        }

    def save_cloud_key(self, key: str) -> dict:
        try:
            self.credentials.set(key)
        except Exception as exc:
            log.warning("could not store the Groq key: %s", exc)
            return {"ok": False, "error": str(exc), "cloud": self.cloud_status()}
        self.dictator.reset_cloud_warning()
        log.info("Groq API key stored (encrypted)")
        return {"ok": True, "cloud": self.cloud_status()}

    def clear_cloud_key(self) -> dict:
        removed = self.credentials.clear(cloud_mod.PROVIDER)
        self.dictator.reset_cloud_warning()
        return {"ok": True, "removed": removed, "cloud": self.cloud_status()}

    def on_notice(self, message: str) -> None:
        """A cloud attempt failed and the local model took over."""
        log.warning("%s", message)
        if self.ui is not None:
            self.ui.emit_toast(message, "error")

    def _storage_error(self, message: str) -> None:
        """History or learning read/write failed; keep it visible until dismissed."""
        log.error("%s", message)
        if message in self._storage_warnings:
            return
        self._storage_warnings.append(message)
        if self.ui is not None:
            self.ui.emit_storage_warnings(list(self._storage_warnings))
            self.ui.emit_toast(message, "error")

    def dismiss_storage_warnings(self) -> None:
        """The window's storage banner was dismissed by the user."""
        self._storage_warnings = []

    def history_payload(self) -> dict:
        """History plus its counters, for the window's list actions."""
        return {"ok": True, "history": self.history.items(), "stats": self.stats()}

    # -- correction learning ----------------------------------------------
    def correct_history(self, item_id: str, text: str) -> dict:
        """Apply a user's edit and learn from the difference."""
        result = self.history.update(str(item_id or ""), text)
        if result is None:
            return {"ok": False, "error": "Dit dictaat bestaat niet meer."}
        learned = {"corrections": 0, "words": 0}
        if result["changed"] and self.cfg.get("learn_from_corrections", True):
            try:
                learned = self.learning.learn_from_diff(result["old"], result["item"]["text"])
            except Exception as exc:  # a bad edit must not lose the corrected text
                log.error("could not learn from correction: %s", exc)
                log.debug("traceback", exc_info=True)
        if result["changed"]:
            log.info(
                "history correction saved (%d word(s), %d vocabulary learned)",
                learned.get("corrections", 0),
                learned.get("words", 0),
            )
        return {
            "ok": True,
            "item": result["item"],
            "history": self.history.items(),
            "stats": self.stats(),
            "learning": self.learning.payload(),
            "learned": learned,
        }

    def forget_learning(self, kind: str, value: str) -> dict:
        kind = str(kind or "").strip().lower()
        if kind == "correction":
            removed = self.learning.forget_correction(value)
        elif kind in ("word", "vocabulary"):
            removed = self.learning.forget_word(value)
        else:
            return {"ok": False, "error": "Onbekend soort leeritem."}
        return {"ok": True, "removed": removed, "learning": self.learning.payload()}

    def clear_learning(self) -> dict:
        self.learning.clear()
        log.info("learned corrections cleared")
        return {"ok": True, "learning": self.learning.payload()}

    def open_path(self, path) -> None:
        try:
            os.startfile(str(path))  # noqa: S606 - opening our own files
        except Exception as exc:
            log.error("could not open %s: %s", path, exc)

    def open_crash_log(self) -> dict:
        """Open the crash log, or report that no crash has been recorded yet."""
        if self.crash_log_path.exists():
            self.open_path(self.crash_log_path)
            return {"ok": True, "path": str(self.crash_log_path)}
        return {
            "ok": False,
            "error": "Er is nog geen crashlog: er zijn geen onverwachte fouten opgeslagen.",
            "path": str(self.crash_log_path),
        }

    # -- engine callbacks -------------------------------------------------
    def on_state(self, state: str) -> None:
        self._refresh_icon()
        if self.ui is not None:
            self.ui.emit_state(state)
            self.ui.emit_listening(self.dictator.listening)

        # Do not spin up the Tk overlay just because the app started: its
        # main loop competes with model loading for the interpreter lock. It is
        # created on the first recording instead.
        if self.overlay is None and state == "idle":
            return
        overlay = self._ensure_overlay()
        if overlay is None:
            return
        self._overlay_generation += 1
        generation = self._overlay_generation
        if state == "recording":
            self._last_partial = ""
            overlay.set_text("")
            overlay.show("recording")
        elif state == "transcribing":
            overlay.set_state("transcribing")
        else:
            timer = threading.Timer(1.2, self._hide_overlay, args=(generation,))
            timer.daemon = True
            timer.start()

    def on_partial(self, text: str) -> None:
        self._last_partial = text
        if self.ui is not None:
            self.ui.emit_partial(text)
        overlay = self.overlay
        if overlay is not None:
            overlay.set_text(text)

    def on_level(self, rms: float) -> None:
        if self.ui is not None:
            self.ui.emit_level(rms)
        overlay = self.overlay
        if overlay is not None and self.dictator.state == "recording":
            overlay.set_level(rms)

    def on_delivered(self, text: str, meta: dict) -> None:
        item = self.history.add(
            text,
            duration=meta.get("duration", 0.0),
            model=meta.get("model", ""),
            language=meta.get("language", ""),
            mode=meta.get("mode", ""),
        )
        if self.ui is not None:
            self.ui.emit_delivered(item, self.history.items(), self.stats())

    def sync_listening(self) -> None:
        """Tray and window both show the state of the hotkey."""
        self._refresh_icon()
        if self.ui is not None:
            self.ui.emit_listening(self.dictator.listening)

    def set_listening(self, enabled: bool) -> dict:
        try:
            if enabled:
                app.validate_hotkey(str(self.cfg.get("hotkey", "<f9>")))
                self.dictator.start_listening()
            else:
                self.dictator.stop_listening()
        except Exception as exc:
            log.error("could not change hotkey state: %s", exc)
            self.sync_listening()
            return {"ok": False, "error": str(exc), "listening": self.dictator.listening}
        self.sync_listening()
        return {"ok": True, "listening": self.dictator.listening}

    def _hide_overlay(self, generation: int) -> None:
        overlay = self.overlay
        if overlay is None:
            return
        if generation == self._overlay_generation and self.dictator.state == "idle":
            overlay.hide()

    # -- window / overlay interplay ---------------------------------------
    def hint_tray(self) -> None:
        """Tell a first-time closer that dictation did not stop."""
        if self._tray_hint_shown or self.icon is None:
            return
        self._tray_hint_shown = True
        try:
            self.icon.notify(
                "Dicteer blijft op de achtergrond luisteren.\n"
                "Klik op het microfoonpictogram om dit venster terug te halen.",
                "Dicteer",
            )
            log.info("tray hint shown")
        except Exception as exc:
            log.debug("tray notification unavailable: %s", exc)

    def on_window_visibility(self, visible: bool) -> None:
        was_visible = self.window_visible
        self.window_visible = visible
        if visible and self.overlay is not None:
            self.overlay.hide()
        elif was_visible and not visible and self.dictator.state == "recording":
            overlay = self._ensure_overlay()
            if overlay is not None:
                overlay.set_text(self._last_partial)
                overlay.show("recording")

    def _ensure_overlay(self):
        """The overlay is for when the window is out of the way."""
        if not self.cfg.get("overlay", True) or self.window_visible:
            return None
        if self.overlay is None:
            try:
                from overlay import Overlay

                self.overlay = Overlay()
            except Exception as exc:
                log.error("live overlay unavailable: %s", exc)
                return None
        return self.overlay

    def apply_settings(self, cfg: dict) -> None:
        self.cfg = cfg
        crashlog.set_enabled(cfg.get("crash_logging", True))
        self.dictator.apply_config(cfg)
        if self.ui is not None:
            self.ui.emit_settings(cfg)
        self.sync_listening()
        if not cfg.get("overlay", True) and self.overlay is not None:
            self.overlay.hide()

    # -- tray -------------------------------------------------------------
    def image(self, key: str):
        if key not in self._images:
            self._images[key] = mic_image(COLORS.get(key, COLORS["off"]))
        return self._images[key]

    def _current_key(self) -> str:
        if not self.dictator.listening:
            return "off"
        return self.dictator.state if self.dictator.state in COLORS else "idle"

    def _tooltip(self) -> str:
        if not self.dictator.listening:
            return "Dicteer - sneltoets uit"
        hotkey = self.hotkey_label()
        return {
            "idle": f"Dicteer - houd {hotkey} ingedrukt om te praten",
            "recording": "Dicteer - opnemen (laat los om te plakken)",
            "transcribing": "Dicteer - omzetten...",
        }.get(self.dictator.state, "Dicteer")

    def _refresh_icon(self) -> None:
        icon = self.icon
        if icon is None:
            return
        try:
            icon.icon = self.image(self._current_key())
            icon.title = self._tooltip()
            icon.update_menu()
        except Exception as exc:
            log.debug("tray refresh failed: %s", exc)

    def status_text(self, _item=None) -> str:
        if not self.dictator.listening:
            return "Gepauzeerd - sneltoets uit"
        verb = "houd" if self.cfg.get("hold_to_talk", False) else "druk op"
        return {
            "idle": f"Klaar - {verb} {self.hotkey_label()} om te praten",
            "recording": "Opnemen... laat los om te plakken",
            "transcribing": "Bezig met omzetten...",
        }.get(self.dictator.state, "Klaar")

    def record_text(self, _item=None) -> str:
        if self.dictator.state == "recording":
            return "Stop en plak"
        if self.dictator.state == "transcribing":
            return "Bezig..."
        return "Opnemen"

    def menu(self):
        import pystray

        return pystray.Menu(
            pystray.MenuItem(self.status_text, None, enabled=lambda item: False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Venster openen", self.on_open_window, default=True),
            pystray.MenuItem(self.record_text, self.on_record),
            pystray.MenuItem(
                "Sneltoets actief",
                self.on_toggle_enabled,
                checked=lambda item: self.dictator.listening,
            ),
            pystray.MenuItem("Instellingen", self.on_open_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Starten met Windows",
                self.on_toggle_autostart,
                checked=lambda item: autostart.is_enabled(),
            ),
            pystray.MenuItem("Logbestand openen", self.on_open_log),
            pystray.MenuItem("Crashlog openen", self.on_open_crash_log),
            pystray.MenuItem("Map openen", self.on_open_folder),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Afsluiten", self.on_quit),
        )

    def _in_background(self, name: str, fn) -> None:
        def runner() -> None:
            try:
                fn()
            except Exception as exc:
                log.error("%s failed: %s", name, exc)
                log.debug("traceback", exc_info=True)

        threading.Thread(target=runner, daemon=True).start()

    def on_open_window(self, icon=None, item=None) -> None:
        if self.ui is not None:
            self.ui.show()

    def on_open_settings(self, icon=None, item=None) -> None:
        if self.ui is not None:
            self.ui.show("settings")

    def on_record(self, icon=None, item=None) -> None:
        self._in_background("record toggle", self.dictator.toggle)

    def on_toggle_enabled(self, icon=None, item=None) -> None:
        def work() -> None:
            self.set_listening(not self.dictator.listening)

        self._in_background("toggle hotkey", work)

    def on_toggle_autostart(self, icon=None, item=None) -> None:
        def work() -> None:
            enabled = autostart.toggle()
            log.info("start with Windows %s", "enabled" if enabled else "disabled")
            self._refresh_icon()
            if self.ui is not None:
                self.ui.emit_toast(
                    "Starten met Windows aan" if enabled else "Starten met Windows uit"
                )

        self._in_background("autostart", work)

    def on_open_log(self, icon=None, item=None) -> None:
        self.open_path(self.log_path if self.log_path.exists() else self.data_dir)

    def on_open_crash_log(self, icon=None, item=None) -> None:
        self.open_crash_log()

    def on_open_folder(self, icon=None, item=None) -> None:
        self.open_path(self.data_dir)

    def on_quit(self, icon=None, item=None) -> None:
        self.quit()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        log.info("quit requested")
        try:
            self.dictator.cancel()
        except Exception:
            pass
        if self.overlay is not None:
            self.overlay.close()
        if self.ui is not None:
            self.ui.destroy()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass

    # -- startup ----------------------------------------------------------
    def start_tray(self) -> None:
        import pystray

        icon = pystray.Icon(
            "dicteer",
            self.image("off"),
            "Dicteer - starten",
            self.menu(),
        )

        def on_ready(ic) -> None:
            self.icon = ic
            ic.visible = True
            self._refresh_icon()
            try:
                self.dictator.start_listening()
            except Exception as exc:
                log.error("could not arm hotkey %s: %s", self.cfg.get("hotkey"), exc)
            log.info("tray ready")

        threading.Thread(target=lambda: icon.run(setup=on_ready), daemon=True).start()
        threading.Thread(target=self.dictator.warmup, daemon=True).start()


def focus_running_instance() -> None:
    """A second launch should bring the existing window forward."""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        hwnd = user32.FindWindowW(None, desktop.TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            log.info("focused the running window")
            return
    except Exception as exc:
        log.debug("could not focus the running window: %s", exc)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            "Dicteer draait al.\n\nKijk naar het microfoonpictogram in het systeemvak.",
            "Dicteer",
            0x00000040,  # MB_ICONINFORMATION
        )
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default=str(app.CONFIG_PATH))
    parser.add_argument("--hidden", action="store_true")
    parser.add_argument("--settings", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    options, _unknown = parser.parse_known_args()

    app.setup_logging(options.verbose)
    # First start after the rename: bring 1.x settings, history and models over
    # before anything reads or writes them.
    app.migrate_legacy_data()
    config_path = Path(options.config)
    crashlog.install(
        enabled=False,
        log_path=config_path.with_name("crash.log"),
        version=desktop.APP_VERSION,
        config_path=config_path,
    )

    try:
        lock = app._acquire_single_instance()
    except RuntimeError:
        log.info("already running, refusing to start a second instance")
        focus_running_instance()
        return 1

    cleaned_up = False
    try:
        cfg = app.load_config(config_path)
        crash_logging = cfg.get("crash_logging", True)
        previous = crashlog.previous_run()
        crashlog.set_enabled(crash_logging)
        crash_notice = crashlog.begin_run(previous, logging=crash_logging)
        if crash_notice.get("pending"):
            log.info("previous run ended unexpectedly; crash log has a new report")
        shell = Shell(
            cfg,
            config_path,
            debug=options.verbose,
            crash_notice=crash_notice,
        )
        shell.ui = desktop.WebUI(
            shell,
            hidden=options.hidden,
            initial_view="settings" if options.settings else None,
        )
        shell.ui.create()

        shell.start_tray()

        log.info("starting window with model %s (%s)", cfg.get("model"), cfg.get("language"))
        shell.ui.run()
        log.info("window closed, shutting down")
        cleaned_up = True
        return 0
    except Exception as exc:
        log.error("startup failed: %s", exc)
        log.debug("traceback", exc_info=True)
        crashlog.record(type(exc), exc, exc.__traceback__, source="startup failure")
        return 1
    finally:
        try:
            if "shell" in locals():
                shell.quit()
        except Exception:
            pass
        try:
            lock.close()
        except Exception:
            pass
        if cleaned_up:
            crashlog.end_run()
        crashlog.close()


if __name__ == "__main__":
    sys.exit(main())
