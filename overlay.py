#!/usr/bin/env python3
"""A small always-on-top overlay that shows dictated words as they arrive.

The overlay must never take keyboard focus: the whole point is that the text
later gets pasted into the window the user is looking at, so stealing focus
would send the paste to the wrong place. That is why it is built as an
``overrideredirect`` Tk window carrying ``WS_EX_NOACTIVATE`` and is shown with
``SW_SHOWNOACTIVATE`` rather than by activating it.

Tk is not thread-safe, so all widget work happens on one dedicated thread and
every other thread talks to it through a queue.
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import threading

log = logging.getLogger("dictate")

BG = "#0f172a"
FG = "#e5e7eb"
MUTED = "#94a3b8"
ACCENTS = {
    "recording": "#ef4444",
    "transcribing": "#f59e0b",
    "idle": "#3b82f6",
}

TITLE = "Dicteer Overlay"
WIDTH = 500
TEXT_WIDTH = WIDTH - 60
BOTTOM_MARGIN = 90
MAX_CHARS = 420

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010


class Overlay:
    """Fire-and-forget overlay; every public method is safe from any thread."""

    def __init__(self, bottom_margin: int = BOTTOM_MARGIN):
        self._commands: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="dictate-overlay", daemon=True)
        self._thread.start()
        self._bottom_margin = bottom_margin
        self._visible = False
        self._level = 0.0
        self._target = 0.0
        self._state = "idle"

    # -- public api -------------------------------------------------------
    def show(self, state: str = "recording") -> None:
        self._commands.put(("show", state))

    def hide(self) -> None:
        self._commands.put(("hide", None))

    def set_text(self, text: str) -> None:
        self._commands.put(("text", text))

    def set_state(self, state: str) -> None:
        self._commands.put(("state", state))

    def set_level(self, rms: float) -> None:
        self._commands.put(("level", float(rms)))

    def close(self) -> None:
        self._commands.put(("quit", None))

    # -- tk thread --------------------------------------------------------
    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception as exc:
            log.error("overlay disabled, tkinter unavailable: %s", exc)
            return

        try:
            # Do not become tkinter's default root: other Tk users in this
            # process (the verification script, for instance) then get their own
            # interpreter instead of accidentally binding to this thread's.
            tk.NoDefaultRoot()
            # Creating a Tk root maps a window, and Tk maps an
            # overrideredirect window as soon as idle tasks are processed.
            # Both would take focus away from whatever the user is typing
            # into, so the window is parked offscreen, marked no-activate and
            # withdrawn before anything gets a chance to activate it.
            previous = self._foreground()
            root = tk.Tk()
            root.withdraw()
            root.overrideredirect(True)
            # Borderless, so this is not drawn - but it makes the window
            # identifiable to window tools and to the verification script.
            root.title(TITLE)
            self._root = root
            try:
                root.geometry("1x1+-10000+-10000")
            except Exception:
                pass
            self._no_activate()
            try:
                root.update_idletasks()
            except Exception:
                pass
            root.withdraw()
            self._restore_focus(previous)
        except Exception as exc:
            log.error("overlay disabled, no display: %s", exc)
            return

        self._tk = tk
        try:
            root.attributes("-topmost", True)
            root.attributes("-alpha", 0.97)
        except Exception:
            pass

        self._frame = tk.Frame(root, bg=BG, highlightthickness=0, padx=16, pady=12)
        self._frame.pack(fill="both", expand=True)
        self._frame.columnconfigure(1, weight=1)

        self._dot = tk.Canvas(self._frame, width=12, height=12, bg=BG, highlightthickness=0)
        self._dot.grid(row=0, column=0, sticky="w")
        self._dot_item = self._dot.create_oval(2, 2, 11, 11, fill=ACCENTS["recording"], outline="")

        self._heading = tk.Label(
            self._frame,
            text="Luisteren",
            bg=BG,
            fg=FG,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        )
        self._heading.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self._meter = tk.Canvas(self._frame, width=88, height=14, bg=BG, highlightthickness=0)
        self._meter.grid(row=0, column=2, sticky="e")
        self._bars = [
            self._meter.create_rectangle(2 + i * 12, 12, 10 + i * 12, 12, fill=ACCENTS["recording"], outline="")
            for i in range(7)
        ]

        self._text = tk.Label(
            self._frame,
            text="",
            bg=BG,
            fg=FG,
            font=("Segoe UI", 12),
            wraplength=TEXT_WIDTH,
            justify="left",
            anchor="w",
        )
        self._text.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 2))

        self._hint = tk.Label(
            self._frame,
            text="laat los om te plakken",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        )
        self._hint.grid(row=2, column=0, columnspan=3, sticky="w")

        root.after(40, self._pump)
        log.info("overlay ready")
        try:
            root.mainloop()
        except Exception as exc:
            log.error("overlay loop crashed: %s", exc)
        log.info("overlay closed")

    @staticmethod
    def _foreground():
        if os.name != "nt":
            return None
        try:
            return ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            return None

    @staticmethod
    def _restore_focus(previous) -> None:
        """Give keyboard focus back to whatever had it before we appeared."""
        if not previous or os.name != "nt":
            return
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            target = user32.GetWindowThreadProcessId(previous, None)
            current = kernel32.GetCurrentThreadId()
            attached = False
            if target and target != current:
                attached = bool(user32.AttachThreadInput(current, target, True))
            user32.SetForegroundWindow(previous)
            if attached:
                user32.AttachThreadInput(current, target, False)
        except Exception as exc:
            log.debug("could not restore focus: %s", exc)

    def _hwnd(self) -> int | None:
        if os.name != "nt":
            return None
        try:
            return int(self._root.wm_frame(), 16)
        except Exception:
            try:
                return int(self._root.winfo_id())
            except Exception:
                return None

    def _no_activate(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            return
        try:
            user32 = ctypes.windll.user32
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            )
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        except Exception as exc:
            log.debug("could not set no-activate styles: %s", exc)

    def _place(self) -> None:
        root = self._root
        root.update_idletasks()
        height = max(70, self._frame.winfo_reqheight())
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        x = max(0, (screen_w - WIDTH) // 2)
        y = max(0, screen_h - height - self._bottom_margin)
        root.geometry(f"{WIDTH}x{height}+{x}+{y}")
        self._height = height

    def _pump(self) -> None:
        try:
            while True:
                command, payload = self._commands.get_nowait()
                if command == "quit":
                    self._root.quit()
                    return
                if command == "show":
                    self.set_state_now(payload or "recording")
                    self._root.deiconify()
                    self._place()
                    self._no_activate()
                    hwnd = self._hwnd()
                    if hwnd:
                        try:
                            ctypes.windll.user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                        except Exception:
                            pass
                    self._visible = True
                elif command == "hide":
                    self._root.withdraw()
                    self._visible = False
                elif command == "text":
                    text = payload if len(payload) <= MAX_CHARS else payload[-MAX_CHARS:]
                    self._text.configure(text=text)
                    self._place()
                elif command == "state":
                    self.set_state_now(payload)
                elif command == "level":
                    self._target = max(0.0, min(1.0, (payload * 6.0) ** 0.5))
        except queue.Empty:
            pass
        except Exception as exc:
            log.debug("overlay command failed: %s", exc)

        self._level = max(self._target, self._level * 0.82)
        try:
            for index, bar in enumerate(self._bars):
                height = 2 + int(self._level * 10 * (0.55 + 0.45 * ((index % 3) / 2)))
                self._meter.coords(bar, 2 + index * 12, 12 - height, 10 + index * 12, 12)
        except Exception:
            pass
        try:
            self._root.after(40, self._pump)
        except Exception:
            pass

    def set_state_now(self, state: str) -> None:
        self._state = state
        colour = ACCENTS.get(state, ACCENTS["idle"])
        heading = {"recording": "Luisteren", "transcribing": "Omzetten", "idle": "Dicteren"}.get(
            state, "Dicteren"
        )
        hint = {"recording": "laat los om te plakken", "transcribing": "even geduld...", "idle": ""}.get(state, "")
        try:
            self._dot.itemconfigure(self._dot_item, fill=colour)
            for bar in self._bars:
                self._meter.itemconfigure(bar, fill=colour)
            self._heading.configure(text=heading)
            self._hint.configure(text=hint)
        except Exception:
            pass
