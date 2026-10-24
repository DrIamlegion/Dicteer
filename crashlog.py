#!/usr/bin/env python3
"""Crash logging for Dicteer.

Unhandled exceptions in the main thread, worker threads and the garbage
collector are appended to ``crash.log`` next to the config that started the
app.  The report contains a traceback and environment metadata; it never
contains dictation text, clipboard contents or the encrypted Groq key.

``faulthandler`` is enabled while crash logging is on, so a fatal native crash
(WebView2, PortAudio, a Whisper native library) also leaves a stack trace
instead of disappearing without a window.

The hooks are installed once at startup.  Turning the option off closes the
fatal-crash file and stops writing, without touching an existing crash log.
"""

from __future__ import annotations

import faulthandler
import json
import os
import platform
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CRASH_LOG = APP_DIR / "crash.log"
RUN_MARKER_NAME = "crash-run.json"
MAX_BYTES = 512_000

# ``faulthandler`` dumps a "Windows fatal exception" for COM errors that the
# WinForms/WebView2 layer raises and then handles.  The app keeps running, so
# the dump is noise; without this list the next launch would announce a crash
# that never happened.  Only codes known to be handled belong here - anything
# else is still treated as a real crash and keeps the notice.
HANDLED_WINDOWS_CODES = frozenset({"0x8001010d"})

_WINDOWS_FATAL_CODE = re.compile(r"Windows fatal exception: code (0x[0-9a-fA-F]+)")
# Written by _header() and only by _header(), so its presence in fresh text
# means a structured report rather than a raw faulthandler dump.
_REPORT_HEADER = re.compile(r"^source: ", re.MULTILINE)

_lock = threading.RLock()
_writing = threading.local()
_installed = False
_enabled = False
_path = CRASH_LOG
_version = ""
_config_path: Path | None = None
_fault_file = None
_previous_sys_hook = sys.excepthook
_previous_thread_hook = getattr(threading, "excepthook", None)
_previous_unraisable_hook = getattr(sys, "unraisablehook", None)


# --------------------------------------------------------------------------
# public
# --------------------------------------------------------------------------
def path() -> Path:
    """The crash log used by this process."""
    return _path


def install(
    *,
    enabled: bool = True,
    log_path: Path | str | None = None,
    version: str = "",
    config_path: Path | str | None = None,
) -> Path:
    """Install the hooks once and apply the current crash-logging choice.

    Calling this again is safe; it updates the target path, version and
    configuration path without stacking another set of hooks.
    """
    global _installed, _path, _version, _config_path

    with _lock:
        if log_path is not None:
            target = Path(log_path)
            if target != _path:
                _disable_fault_handler()
                _path = target
        if version:
            _version = str(version)
        if config_path is not None:
            _config_path = Path(config_path)

        if not _installed:
            _previous_sys_hook = sys.excepthook
            _previous_thread_hook = getattr(threading, "excepthook", None)
            _previous_unraisable_hook = getattr(sys, "unraisablehook", None)
            sys.excepthook = _sys_hook
            if _previous_thread_hook is not None:
                threading.excepthook = _thread_hook
            if _previous_unraisable_hook is not None:
                sys.unraisablehook = _unraisable_hook
            _installed = True

        set_enabled(enabled)
    return _path


def set_enabled(enabled: bool) -> None:
    """Turn writing on or off; existing crash logs are kept."""
    global _enabled

    wanted = bool(enabled)
    with _lock:
        _enabled = wanted
        if not _installed:
            return
        if wanted:
            _enable_fault_handler()
        else:
            _disable_fault_handler()


def is_enabled() -> bool:
    return bool(_enabled)


def close() -> None:
    """Stop the fatal-crash handler at process exit."""
    _disable_fault_handler()


# --------------------------------------------------------------------------
# run marker
# --------------------------------------------------------------------------
def previous_run() -> dict:
    """Describe the previous run, before this run starts writing its header.

    A marker next to the crash log is written at startup and removed on a clean
    exit.  When it is still there, the previous process did not reach its
    shutdown path.  ``pending`` is only true when the crash log grew since that
    run started *and* the appended text is a real crash: a stale marker, or a
    dump from a handled WebView2 COM exception, does not produce a misleading
    notice.
    """
    previous = _read_run_marker()
    notice = {"pending": _has_new_report(previous), "path": str(_path)}
    if isinstance(previous, dict):
        for key in ("started", "version"):
            if previous.get(key):
                notice[key] = previous[key]
    return notice


def begin_run(previous: dict | None = None, *, logging: bool = True) -> dict:
    """Remember this run and return the notice for the previous one.

    Call :func:`previous_run` before enabling logging, because enabling writes
    the current startup header.  Pass its result here after logging is on, so
    the marker records the crash-log size that belongs to this run.
    """
    previous = previous or {"pending": False, "path": str(_path)}
    marker = {
        "started": time.time(),
        "pid": os.getpid(),
        "version": _version,
        "logging": bool(logging),
        "log_size": _log_size(),
    }
    try:
        _write_run_marker(marker)
    except Exception:
        pass
    notice = {
        "pending": bool(previous.get("pending")),
        "path": str(_path),
    }
    for key in ("started", "version"):
        if previous.get(key):
            notice[key] = previous[key]
    return notice


def end_run() -> None:
    """Clear the marker after a normal shutdown, so the next start is quiet."""
    try:
        _run_marker_path().unlink(missing_ok=True)
    except Exception:
        pass


def _run_marker_path() -> Path:
    return _path.with_name(RUN_MARKER_NAME)


def _read_run_marker() -> dict | None:
    try:
        data = json.loads(_run_marker_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_run_marker(marker: dict) -> None:
    target = _run_marker_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(marker, indent=1), encoding="utf-8")
    os.replace(tmp, target)


def _log_size() -> int:
    try:
        return int(_path.stat().st_size)
    except Exception:
        return 0


def _has_new_report(previous) -> bool:
    if not isinstance(previous, dict) or not previous.get("logging", True):
        return False
    try:
        offset = int(previous.get("log_size", 0))
    except (TypeError, ValueError):
        offset = 0
    size = _log_size()
    if size <= offset:
        return False
    try:
        with _path.open("rb") as handle:
            handle.seek(max(offset, 0))
            fresh = handle.read().decode("utf-8", "replace")
    except Exception:
        # An unreadable tail is no reason to hide a possible crash.
        return True
    return _has_real_report(fresh)


def _has_real_report(fresh: str) -> bool:
    """Whether the text appended since the marker describes a real crash.

    A structured report always counts.  A bare faulthandler dump counts unless
    every exception code in it is one the app survives; an unknown or mixed
    dump keeps the notice, because a real native crash must still be reported.
    """
    if not fresh.strip():
        return False
    if _REPORT_HEADER.search(fresh):
        return True
    codes = [code.lower() for code in _WINDOWS_FATAL_CODE.findall(fresh)]
    if not codes:
        return True
    return any(code not in HANDLED_WINDOWS_CODES for code in codes)


def record(
    exc_type,
    exc_value,
    exc_traceback,
    *,
    source: str = "uncaught exception",
    thread_name: str | None = None,
) -> Path | None:
    """Append one crash report, or return ``None`` when logging is off."""
    if not _enabled or getattr(_writing, "active", False):
        return None
    _writing.active = True
    try:
        return write_report(
            _path,
            exc_type,
            exc_value,
            exc_traceback,
            source=source,
            version=_version,
            config_path=_config_path,
            thread_name=thread_name or threading.current_thread().name,
        )
    except Exception:
        # A full disk or a locked file must never turn one crash into two.
        return None
    finally:
        _writing.active = False


def write_report(
    target: Path | str,
    exc_type,
    exc_value,
    exc_traceback,
    *,
    source: str = "uncaught exception",
    version: str = "",
    config_path: Path | str | None = None,
    thread_name: str | None = None,
) -> Path:
    """Write a crash report to ``target``.

    Kept separate from the global hooks so tests and maintenance tools can
    produce a report without installing handlers in their own process.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(_header(source, version, config_path, thread_name))
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=handle)
            handle.write("\n")
            handle.flush()
    return target


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------
def _sys_hook(exc_type, exc_value, exc_traceback) -> None:
    try:
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            record(exc_type, exc_value, exc_traceback, source="uncaught exception")
    finally:
        try:
            if _previous_sys_hook is not None:
                _previous_sys_hook(exc_type, exc_value, exc_traceback)
        except Exception:
            pass


def _thread_hook(args) -> None:
    try:
        if not issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            thread = getattr(args, "thread", None)
            record(
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
                source="uncaught exception in thread",
                thread_name=getattr(thread, "name", None) or "thread",
            )
    finally:
        try:
            if _previous_thread_hook is not None:
                _previous_thread_hook(args)
        except Exception:
            pass


def _unraisable_hook(args) -> None:
    try:
        if not issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            record(
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
                source="unraisable exception",
                thread_name=threading.current_thread().name,
            )
    finally:
        try:
            if _previous_unraisable_hook is not None:
                _previous_unraisable_hook(args)
        except Exception:
            pass


# --------------------------------------------------------------------------
# fatal crashes
# --------------------------------------------------------------------------
def _enable_fault_handler() -> None:
    global _fault_file

    if _fault_file is not None:
        return
    try:
        _path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        _fault_file = _path.open("a", encoding="utf-8", buffering=1)
        if _fault_file.tell() == 0:
            label = f" {_version}" if _version else ""
            _fault_file.write(f"Dicteer crash log{label}\n")
            _fault_file.flush()
        faulthandler.enable(file=_fault_file, all_threads=True)
    except Exception:
        if _fault_file is not None:
            try:
                _fault_file.close()
            except Exception:
                pass
            _fault_file = None


def _disable_fault_handler() -> None:
    global _fault_file

    try:
        faulthandler.disable()
    except Exception:
        pass
    file = _fault_file
    _fault_file = None
    if file is not None:
        try:
            file.close()
        except Exception:
            pass


def _rotate_if_needed() -> None:
    try:
        if _path.exists() and _path.stat().st_size > MAX_BYTES:
            previous = _path.with_name(_path.stem + ".old" + _path.suffix)
            if previous.exists():
                previous.unlink()
            _path.replace(previous)
    except Exception:
        pass


# --------------------------------------------------------------------------
# report formatting
# --------------------------------------------------------------------------
def _header(
    source: str,
    version: str,
    config_path: Path | str | None,
    thread_name: str | None,
) -> str:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "",
        "=" * 72,
        f"{stamp}  Dicteer {version}".rstrip(),
        f"source: {source}",
        f"thread: {thread_name or threading.current_thread().name}",
        f"python: {platform.python_version()} ({platform.system()} {platform.release()})",
        f"executable: {sys.executable}",
    ]
    if config_path is not None:
        lines.append(f"config: {config_path}")
    lines.append("=" * 72)
    return "\n".join(lines) + "\n"
