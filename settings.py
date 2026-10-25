#!/usr/bin/env python3
"""One place where settings are validated, merged and written.

The window, the tray and the tests all go through ``apply_updates`` so a setting
can never be saved in a state the engine cannot run: a Dutch language with an
English-only model, a hotkey pynput cannot parse, or a thread count of zero.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import cloud
import dictate

MODELS = [
    "tiny.en",
    "base.en",
    "small.en",
    "medium.en",
    "tiny",
    "base",
    "small",
    "large-v3-turbo",
    "medium",
    "large-v3",
]
LANGUAGES = ["en", "nl", "de", "fr", "es", "it", "pt", "auto"]
MODES = ("paste", "type", "clipboard")
ONLINE_BACKENDS = ("local", "groq", "gemini")


class SettingsError(ValueError):
    """Raised with a message that is safe (and useful) to show to the user."""


def _as_int(value, name: str, low: int, high: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise SettingsError(f"{name} moet een getal zijn") from None
    if not low <= number <= high:
        raise SettingsError(f"{name} moet tussen {low} en {high} liggen")
    return number


def _as_float(value, name: str, low: float, high: float) -> float:
    try:
        number = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        raise SettingsError(f"{name} moet een getal zijn") from None
    if not low <= number <= high:
        raise SettingsError(f"{name} moet tussen {low} en {high} liggen")
    return number


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "aan")
    return bool(value)


def normalise_language(value) -> str | None:
    text = str(value or "").strip().lower()
    if text in ("", "auto", "automatisch"):
        return None
    if len(text) != 2 or not text.isalpha():
        raise SettingsError("Taal moet een tweeleterige code zijn, zoals nl of en")
    return text


def apply_updates(current: dict, updates: dict) -> dict:
    """Merge ``updates`` into ``current`` and return a validated copy."""
    known = set(dictate.DEFAULT_CONFIG)
    cfg = dict(current)
    for key, value in (updates or {}).items():
        if key in known:
            cfg[key] = value

    hotkey = str(cfg.get("hotkey", "")).strip()
    try:
        dictate.validate_hotkey(hotkey)
    except ValueError as exc:
        # A ValueError from pynput is not a UI-ready message; the window shows
        # SettingsError text verbatim.
        raise SettingsError(str(exc)) from None
    cfg["hotkey"] = hotkey

    model = str(cfg.get("model") or "").strip()
    if not model:
        raise SettingsError("Kies een model")
    preview = str(cfg.get("preview_model") or "").strip()
    language = normalise_language(cfg.get("language"))
    if language not in (None, "en"):
        for candidate in (model, preview):
            if candidate.endswith(".en"):
                raise SettingsError(
                    f"'{candidate}' begrijpt alleen Engels. Kies voor {language} "
                    "een meertalig model zoals 'small' of 'base'."
                )

    mode = str(cfg.get("mode") or "paste").strip().lower()
    if mode not in MODES:
        raise SettingsError("Leveringswijze moet plakken, typen of klembord zijn")

    backend = str(cfg.get("online_backend") or "local").strip().lower()
    if backend not in ONLINE_BACKENDS:
        raise SettingsError("Online model moet lokaal, Groq of Gemini zijn")

    gemini_model = str(cfg.get("gemini_model") or "").strip()
    choices = [entry["value"] for entry in cloud.GEMINI_MODELS]
    if gemini_model not in choices:
        if gemini_model:
            raise SettingsError("Kies een Gemini-model uit de lijst")
        gemini_model = cloud.GEMINI_MODEL

    device = cfg.get("input_device")
    if isinstance(device, str):
        device = device.strip()
        if not device or device.lower() in ("default", "standaard", "systeemstandaard"):
            device = None
        elif device.isdigit():
            device = int(device)
    if isinstance(device, int) and device < 0:
        device = None

    cfg.update(
        {
            "model": model,
            "preview_model": preview,
            "language": language,
            "mode": mode,
            "online_backend": backend,
            "gemini_model": gemini_model,
            "online_timeout_seconds": _as_int(
                cfg.get("online_timeout_seconds", 30), "Online time-out", 5, 120
            ),
            "input_device": device,
            "cpu_threads": _as_int(cfg.get("cpu_threads", 4), "CPU-threads", 1, 16),
            "beam_size": _as_int(cfg.get("beam_size", 1), "Beam size", 1, 5),
            "max_seconds": _as_int(cfg.get("max_seconds", 120), "Maximale opnameduur", 5, 600),
            "min_seconds": _as_float(cfg.get("min_seconds", 0.35), "Minimale duur", 0.1, 5.0),
            "stream_chunk_seconds": _as_float(
                cfg.get("stream_chunk_seconds", 1.6), "Preview-blok", 0.8, 8.0
            ),
            "stream_overlap_seconds": _as_float(
                cfg.get("stream_overlap_seconds", 0.8), "Preview-overlap", 0.0, 3.0
            ),
            "stream_poll_seconds": _as_float(
                cfg.get("stream_poll_seconds", 0.45), "Preview-interval", 0.2, 2.0
            ),
            "initial_prompt": str(cfg.get("initial_prompt") or "").strip()[:400],
            "hold_to_talk": _as_bool(cfg.get("hold_to_talk", True)),
            "overlay": _as_bool(cfg.get("overlay", True)),
            "stream_preview": _as_bool(cfg.get("stream_preview", True)),
            "restore_clipboard": _as_bool(cfg.get("restore_clipboard", True)),
            "post_space": _as_bool(cfg.get("post_space", True)),
            "beeps": _as_bool(cfg.get("beeps", True)),
            "learn_from_corrections": _as_bool(cfg.get("learn_from_corrections", True)),
            "crash_logging": _as_bool(cfg.get("crash_logging", True)),
        }
    )
    return cfg


def save(path: Path, cfg: dict) -> None:
    """Write atomically so a crash cannot leave half a config behind."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load(path: Path) -> dict:
    return dictate.load_config(path)
