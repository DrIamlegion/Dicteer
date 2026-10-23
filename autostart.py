#!/usr/bin/env python3
"""Launch-at-login support for Dicteer.

Windows has no registry-free API for this in the standard library, so the
shortcut is written into the user's Startup folder through the shell's
WScript.Shell COM object. Everything stays per-user: no admin rights, no
scheduled tasks, and deleting the .lnk is a complete uninstall of the feature.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
LINK_NAME = "Dicteer.lnk"
ENTRY = APP_DIR / "tray.pyw"


def frozen() -> bool:
    """True inside the packaged build, where the exe is the entry point."""
    return bool(getattr(sys, "frozen", False))


def pythonw() -> Path:
    """pythonw.exe runs without a console window; fall back to the current exe."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def target() -> Path:
    """What Windows should start at login."""
    return Path(sys.executable) if frozen() else pythonw()


def arguments() -> str:
    """Installed build: just ``--hidden``; checkout: the script and its flag."""
    return "--hidden" if frozen() else f'"{ENTRY}" --hidden'


def working_dir() -> Path:
    return Path(sys.executable).resolve().parent if frozen() else APP_DIR


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set, so the Startup folder cannot be found")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path:
    return startup_dir() / LINK_NAME


def is_enabled() -> bool:
    try:
        return shortcut_path().exists()
    except RuntimeError:
        return False


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def enable() -> Path:
    """Create the login shortcut and return its path."""
    link = shortcut_path()
    link.parent.mkdir(parents=True, exist_ok=True)

    script = (
        "$ErrorActionPreference='Stop';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut({link});"
        "$s.TargetPath={target};"
        "$s.Arguments={args};"
        "$s.WorkingDirectory={cwd};"
        "$s.IconLocation={icon};"
        "$s.Description='Dicteer - offline voice to text';"
        "$s.Save()"
    ).format(
        link=_ps_quote(str(link)),
        target=_ps_quote(str(target())),
        args=_ps_quote(arguments()),
        cwd=_ps_quote(str(working_dir())),
        icon=_ps_quote(f"{target()},0"),
    )

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        creationflags=flags,
    )
    if result.returncode != 0 or not link.exists():
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"could not create {link.name}: {detail or 'unknown error'}")
    return link


def disable() -> None:
    """Remove the login shortcut; harmless when it does not exist."""
    try:
        shortcut_path().unlink()
    except FileNotFoundError:
        pass


def toggle() -> bool:
    """Flip autostart and report the new state."""
    if is_enabled():
        disable()
        return False
    enable()
    return True


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "enable":
        print(f"enabled: {enable()}")
    elif action == "disable":
        disable()
        print("disabled")
    elif action == "toggle":
        print("enabled" if toggle() else "disabled")
    else:
        print(f"{'enabled' if is_enabled() else 'disabled'} ({shortcut_path()})")
