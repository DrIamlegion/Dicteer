#!/usr/bin/env python3
"""Freebuff Dictate - push-to-dictate for any Windows app.

Press the hotkey (default F9) to start recording, press it again to stop.
The clip is transcribed locally by faster-whisper and the text is pasted
into whatever window currently has focus (Freebuff's prompt box included).

By default nothing leaves the machine: no API key, no network after the model
download. The optional online backend sends only the finished utterance to
Groq (free tier) and still falls back to the local model on any failure; the
live preview always stays local.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, "frozen", False))


def default_data_dir() -> Path:
    """Where config, history, logs and models live.

    A checkout keeps them next to the code. The installed build uses
    ``%APPDATA%\\Freebuff Dictate`` instead: the program folder is shared
    between users and protected, and keeping the data separate means an
    upgrade or uninstall cannot take the dictations with it. The environment
    variable exists so the packaged build can be exercised against a
    throwaway folder.
    """
    override = os.environ.get("FREEBUFF_DICTATE_DATA")
    if override:
        return Path(override)
    if not FROZEN:
        return APP_DIR
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    return Path(base) / "Freebuff Dictate" if base else Path.home() / "Freebuff Dictate"


DATA_DIR = default_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "dictate.log"
MODEL_DIR = DATA_DIR / "models"

DEFAULT_CONFIG = {
    "hotkey": "<f9>",
    "hold_to_talk": True,
    "overlay": True,
    "stream_preview": True,
    "stream_chunk_seconds": 1.6,
    "stream_overlap_seconds": 0.8,
    "stream_poll_seconds": 0.45,
    "model": "base.en",
    "preview_model": "",
    "language": "en",
    "input_device": None,
    "compute_type": "int8",
    "cpu_threads": 4,
    "beam_size": 1,
    "mode": "paste",
    "restore_clipboard": True,
    "post_space": True,
    "initial_prompt": "",
    "max_seconds": 120,
    "min_seconds": 0.35,
    "beeps": True,
    "learn_from_corrections": True,
    "crash_logging": True,
    "online_backend": "local",
    "online_timeout_seconds": 30,
}

log = logging.getLogger("dictate")


# --------------------------------------------------------------------------
# setup helpers
# --------------------------------------------------------------------------
def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(file_handler)
    log.setLevel(logging.DEBUG)

    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        console.setLevel(level)
        log.addHandler(console)


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # broken config must not stop dictation
            log.warning("config.json is unreadable (%s); using defaults", exc)
    else:
        try:
            path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception:
            pass
    return cfg


def save_config(path: Path, cfg: dict) -> None:
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def validate_hotkey(hotkey: str) -> None:
    """Raise ValueError when pynput cannot parse the combo (e.g. 'f9')."""
    from pynput.keyboard import HotKey

    if not hotkey or not hotkey.strip():
        raise ValueError("enter a hotkey such as <f9> or <ctrl>+<alt>+d")
    try:
        HotKey.parse(hotkey)
    except Exception as exc:
        raise ValueError(
            f"{hotkey!r} is not a valid hotkey - write keys in angle brackets, "
            f"e.g. <f9> or <ctrl>+<alt>+d ({exc})"
        ) from exc


def beep(kind: str, enabled: bool) -> None:
    """Short audio cues so the app is usable with its window hidden."""
    if not enabled:
        return
    patterns = {
        "start": [(880, 70), (1180, 90)],
        "stop": [(1180, 70)],
        "done": [(1000, 60), (1320, 70)],
        "error": [(300, 220)],
        "busy": [(520, 90)],
    }

    def _play() -> None:
        try:
            import winsound

            for freq, ms in patterns.get(kind, []):
                winsound.Beep(freq, ms)
        except Exception:
            pass

    threading.Thread(target=_play, daemon=True).start()


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------
# Windows exposes the same endpoint through MME, DirectSound, WASAPI and
# WDM-KS.  Showing all of them makes the picker look like a driver dump, so the
# settings list dedupes by name and prefers the modern host API.
_MAPPER_MARKERS = (
    "microsoft sound mapper",
    "microsoft-geluidstoewijzing",
    "primary sound capture",
    "primair stuurprogramma",
)


def _clean_device_name(name: str) -> str:
    text = " ".join(str(name or "").split())
    # Bluetooth endpoints often include the driver path and a placeholder
    # before the human-readable product name.
    if "@system32" in text.casefold() and ";" in text:
        text = text.rsplit(";", 1)[-1].strip()
        text = text.replace("%1", "").replace("%0", "").strip()
        text = text.strip("() ").strip()
    return text


def _hostapi_rank(hostapi: str) -> int:
    name = str(hostapi or "").casefold()
    if "wasapi" in name:
        return 3
    if "wdm-ks" in name:
        return 2
    if "directsound" in name:
        return 1
    return 0


def enumerate_input_devices() -> list[dict]:
    """The real Windows input endpoints, one row per microphone.

    ``id`` is the device name and is what the window saves.  Windows renumbers
    device indices when hardware is plugged in or a Bluetooth headset appears,
    so an index is not a stable setting; the name is resolved again each time.
    """
    import sounddevice as sd

    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        default_index = sd.default.device[0]
    except Exception as exc:
        log.debug("could not enumerate microphones: %s", exc)
        return []

    default_name = ""
    if isinstance(default_index, int) and 0 <= default_index < len(devices):
        default_name = _clean_device_name(devices[default_index].get("name")).casefold()

    best: dict[str, tuple[int, dict]] = {}
    for index, dev in enumerate(devices):
        if int(dev.get("max_input_channels") or 0) < 1:
            continue
        name = _clean_device_name(dev.get("name"))
        key = name.casefold()
        if not key or key in ("input", "input ()"):
            continue
        if any(marker in key for marker in _MAPPER_MARKERS):
            continue
        api = ""
        try:
            api = str(hostapis[dev["hostapi"]]["name"])
        except Exception:
            pass
        entry = {
            "id": name,
            "name": name,
            "label": name,
            "index": index,
            "default": key == default_name,
            "api": api,
            "rate": int(dev.get("default_samplerate") or 0),
        }
        rank = _hostapi_rank(api)
        current = best.get(key)
        if current is None:
            best[key] = (rank, entry)
        else:
            # Prefer the modern API, but never lose the Windows-default mark.
            entry["default"] = entry["default"] or current[1]["default"]
            if rank > current[0]:
                best[key] = (rank, entry)
            elif entry["default"]:
                current[1]["default"] = True

    entries = [entry for _, entry in best.values()]
    entries.sort(key=lambda item: (not item["default"], item["name"].casefold()))
    return entries


def list_devices() -> None:
    entries = enumerate_input_devices()
    print("Input devices (* = Windows default):")
    if not entries:
        print("  (none found)")
    for entry in entries:
        mark = "*" if entry["default"] else " "
        api = f", {entry['api']}" if entry["api"] else ""
        print(
            f" {mark} {entry['name']} "
            f"({entry['rate']} Hz, index {entry['index']}{api})"
        )
    print(
        "\nChoose the microphone in Settings, or set \"input_device\" in "
        "config.json to its name or index."
    )


def resolve_device(setting, fallback: bool = True):
    """Resolve a saved microphone name (or legacy index) to a PortAudio index.

    Names survive Windows renumbering; indices do not.  A stale index is logged
    and treated as the system default instead of failing a dictation.
    """
    import sounddevice as sd

    if setting is None or setting == "":
        return None
    if isinstance(setting, int) or (isinstance(setting, str) and setting.isdigit()):
        index = int(setting)
        try:
            sd.query_devices(index, "input")
            return index
        except Exception:
            message = (
                f"microphone index {index} is no longer an input device"
            )
            if fallback:
                log.warning("%s; using the system default", message)
                return None
            raise RuntimeError(f"{message} (kies opnieuw in Instellingen)") from None

    needle = " ".join(str(setting).split()).casefold()
    entries = enumerate_input_devices()
    exact = [entry for entry in entries if entry["id"].casefold() == needle]
    if exact:
        return exact[0]["index"]
    partial = [entry for entry in entries if needle in entry["name"].casefold()]
    if partial:
        partial.sort(
            key=lambda entry: (
                not entry["name"].casefold().startswith(needle),
                entry["name"].casefold(),
            )
        )
        return partial[0]["index"]

    # A name that was filtered out (for example an old mapper) can still be
    # addressed explicitly from the command line.
    for index, dev in enumerate(sd.query_devices()):
        if int(dev.get("max_input_channels") or 0) > 0 and needle in str(
            dev.get("name")
        ).casefold():
            return index

    message = f"microphone {setting!r} was not found"
    if fallback:
        log.warning("%s; using the system default", message)
        return None
    raise RuntimeError(f"{message} (kies opnieuw in Instellingen)")


def to_16k_mono(audio, src_rate: int):
    """Whisper wants 16 kHz mono float32; mics here run at 44.1/48 kHz."""
    import numpy as np

    if audio.size == 0 or src_rate == 16000:
        return audio.astype(np.float32, copy=False)

    ratio = src_rate / 16000
    step = int(round(ratio))
    if step >= 1 and src_rate % 16000 == 0 and abs(ratio - step) < 1e-6:
        usable = (audio.size // step) * step
        if usable == 0:
            return np.zeros(0, dtype=np.float32)
        # box-average decimation, which also suppresses aliasing
        return audio[:usable].reshape(-1, step).mean(axis=1).astype(np.float32)

    target = max(1, int(round(audio.size / ratio)))
    return np.interp(
        np.linspace(0.0, 1.0, target, dtype=np.float64),
        np.linspace(0.0, 1.0, audio.size, dtype=np.float64),
        audio,
    ).astype(np.float32)


def default_input_device():
    """A usable input index even when Windows reports no global default.

    PortAudio reports ``-1`` for the input side on machines whose default
    endpoint lives in a host API (WDM-KS here). Asking for device ``-1`` then
    raises ``Error querying device -1``, so fall back to the preferred host
    API's endpoint and finally to the first input we can enumerate.
    """
    import sounddevice as sd

    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception as exc:
        log.debug("could not query audio devices: %s", exc)
        return None

    def usable(index) -> bool:
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        return 0 <= index < len(devices) and int(devices[index].get("max_input_channels") or 0) > 0

    try:
        global_default = sd.default.device[0]
    except Exception:
        global_default = -1
    if usable(global_default):
        return int(global_default)

    ranked = sorted(
        range(len(hostapis)),
        key=lambda index: _hostapi_rank(hostapis[index].get("name")),
        reverse=True,
    )
    for index in ranked:
        candidate = hostapis[index].get("default_input_device", -1)
        if usable(candidate):
            return int(candidate)

    entries = enumerate_input_devices()
    return entries[0]["index"] if entries else None


class Recorder:
    def __init__(self, device, sink=None):
        import sounddevice as sd

        if device is None or (isinstance(device, int) and device < 0):
            device = default_input_device()
        if device is None:
            raise RuntimeError(
                "Geen invoermicrofoon gevonden. Sluit een microfoon aan en kies hem in Instellingen."
            )
        self.device = device
        self.sink = sink  # called with every block from the audio callback
        try:
            info = sd.query_devices(device, "input")
        except Exception as exc:
            raise RuntimeError(f"Microfoon is niet beschikbaar: {exc}") from None
        self.rate = int(info["default_samplerate"])
        self.frames = []
        self.stream = None
        self._lock = threading.Lock()

    def start(self) -> None:
        import sounddevice as sd

        self.frames = []
        self.stream = sd.InputStream(
            device=self.device,
            channels=1,
            samplerate=self.rate,
            dtype="float32",
            blocksize=0,
            callback=self._callback,
        )
        self.stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("audio status: %s", status)
        chunk = indata[:, 0].copy()
        with self._lock:
            self.frames.append(chunk)
        if self.sink is not None:
            try:
                self.sink(chunk)
            except Exception as exc:  # never let the UI/stream kill the capture
                log.debug("audio sink failed: %s", exc)

    def stop(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                log.debug("stream close failed: %s", exc)
        with self._lock:
            frames, self.frames = self.frames, []
        if not frames:
            import numpy as np

            return np.zeros(0, dtype="float32")
        import numpy as np

        return np.concatenate(frames)

    @property
    def duration(self) -> float:
        return sum(f.size for f in self.frames) / float(self.rate)

    def level(self) -> float:
        """RMS of the most recent block, for meters and input checks."""
        import numpy as np

        with self._lock:
            if not self.frames:
                return 0.0
            block = self.frames[-1]
        return float(np.sqrt(np.mean(block**2))) if block.size else 0.0


# --------------------------------------------------------------------------
# transcription + delivery
# --------------------------------------------------------------------------
class Dictator:
    def __init__(self, cfg: dict, args: argparse.Namespace):
        self.cfg = cfg
        self.args = args
        self._model = None
        self._model_lock = threading.Lock()
        self._preview_model = None
        self._preview_name = ""
        self._preview_lock = threading.Lock()
        self._state = "idle"  # idle | recording | transcribing
        self._state_lock = threading.Lock()
        self._recorder = None
        self._record_started = 0.0
        self._watchdog = None
        self._toggle_lock = threading.Lock()
        self._listener = None
        self.on_state = None  # optional callback(state: str), used by the tray UI
        self.on_partial = None  # optional callback(text: str) with live preview text
        self.on_level = None  # optional callback(rms: float) for a level meter
        self.on_delivered = None  # optional callback(text: str, meta: dict) after a paste
        # The level callback reaches the window, which must never run on the
        # PortAudio thread: a slow UI would then make the capture miss blocks.
        self._level_value = 0.0
        self._level_lock = threading.Lock()
        self._level_event = threading.Event()
        self._level_thread: threading.Thread | None = None
        # A cancellation token belongs to one transcription job. It lets quit
        # invalidate a slow final pass before it can paste into another app.
        self._cancel_requested = threading.Event()
        self._cancel_token: threading.Event | None = None
        # Hooks used by the window's correction learning. Both are optional so
        # the console/CLI path and the tests run exactly as before.
        self.postprocess = None  # callback(text: str) -> str, applied to transcripts
        self.extra_prompt = None  # callback() -> str, learned vocabulary for Whisper
        self.cloud = None  # optional cloud.CloudTranscriber, set by the window shell
        self.on_notice = None  # callback(message: str) for a failed online attempt
        self._cloud_warning = ""
        self._last_backend = "local"
        self._last_model = str(cfg.get("model") or "")
        self._stream = None
        self._hold_hotkey = None
        self._hold_keys: set = set()
        self._hold_active = False

    # -- state ------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def listening(self) -> bool:
        listener = self._listener
        return bool(listener is not None and getattr(listener, "running", False))

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self._state = state
        callback = self.on_state
        if callback is not None:
            try:
                callback(state)
            except Exception as exc:  # a broken UI must never stop dictation
                log.debug("state callback failed: %s", exc)

    # -- model ------------------------------------------------------------
    def model(self):
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    from faster_whisper import WhisperModel

                    threads = int(self.cfg.get("cpu_threads") or 0)
                    log.info("loading model %s (first run downloads it, ~250 MB)", self.cfg["model"])
                    started = time.time()
                    self._model = WhisperModel(
                        self.cfg["model"],
                        device="cpu",
                        compute_type=self.cfg.get("compute_type", "int8"),
                        cpu_threads=threads,
                        download_root=str(MODEL_DIR),
                    )
                    log.info("model ready in %.1fs", time.time() - started)
                    language = self.cfg.get("language")
                    if language and language != "en" and str(self.cfg.get("model", "")).endswith(".en"):
                        log.warning(
                            "model %s only understands English but language is %r - use a "
                            "multilingual model such as 'small'",
                            self.cfg.get("model"),
                            language,
                        )
        return self._model

    def preview_model(self):
        """The cheaper model behind the live overlay.

        Re-previewing a slice costs almost the same as previewing a slightly
        longer one (Whisper always runs the 30 s encoder), so a smaller model
        buys responsiveness far more than a larger chunk would: on this CPU
        'small' needs ~2.3 s per pass while 'base' needs ~0.7 s.
        """
        name = str(self.cfg.get("preview_model") or "").strip()
        main = str(self.cfg.get("model") or "")
        if not name or name == main:
            self._preview_name = main
            return self.model()
        if self._preview_model is None:
            with self._preview_lock:
                if self._preview_model is None:
                    from faster_whisper import WhisperModel

                    log.info("loading preview model %s", name)
                    started = time.time()
                    self._preview_model = WhisperModel(
                        name,
                        device="cpu",
                        compute_type=self.cfg.get("compute_type", "int8"),
                        cpu_threads=int(self.cfg.get("cpu_threads") or 0),
                        download_root=str(MODEL_DIR),
                    )
                    self._preview_name = name
                    log.info("preview model ready in %.1fs", time.time() - started)
        return self._preview_model

    def warmup(self) -> None:
        try:
            self.model()
            name = str(self.cfg.get("preview_model") or "").strip()
            if name and name != str(self.cfg.get("model") or ""):
                self.preview_model()
        except Exception as exc:
            log.error("could not load model: %s", exc)

    # -- text -------------------------------------------------------------
    def _prompt(self):
        """The configured vocabulary plus anything learned from corrections."""
        base = str(self.cfg.get("initial_prompt") or "").strip()
        extra = ""
        if self.extra_prompt is not None:
            try:
                extra = str(self.extra_prompt() or "").strip()
            except Exception as exc:  # learning must never break dictation
                log.debug("extra prompt failed: %s", exc)
        combined = ", ".join(part for part in (base, extra) if part)
        if len(combined) > 400:
            combined = combined[:400].rsplit(",", 1)[0].strip()
        return combined or None

    def _postprocess(self, text: str) -> str:
        callback = self.postprocess
        if callback is None:
            return text
        try:
            result = callback(text)
            return " ".join(str(result if result is not None else text).split())
        except Exception as exc:
            log.debug("post-processing failed: %s", exc)
            return text

    def transcribe_audio(self, audio) -> str:
        """Final transcript: the online backend when selected, local on failure."""
        backend = str(self.cfg.get("online_backend") or "local").strip().lower()
        self._last_backend = "local"
        self._last_model = str(self.cfg.get("model") or "")
        if backend != "local" and self.cloud is not None:
            try:
                started = time.time()
                text = self.cloud.transcribe(
                    audio,
                    language=(self.cfg.get("language") or None),
                    prompt=self._prompt(),
                    timeout=float(self.cfg.get("online_timeout_seconds", 30)),
                )
                log.info("online transcript from %s in %.1fs", backend, time.time() - started)
                self._last_backend = backend
                self._last_model = str(getattr(self.cloud, "model", backend))
                self._cloud_warning = ""
                return self._postprocess(" ".join(str(text).split()))
            except Exception as exc:  # network, key, quota - never lose the clip
                message = str(exc) or type(exc).__name__
                log.warning("online transcription failed: %s", message)
                self._notify_cloud(message)
        return self._transcribe_local(audio)

    def reset_cloud_warning(self) -> None:
        """Allow the next cloud failure to be reported again."""
        self._cloud_warning = ""

    def _notify_cloud(self, message: str) -> None:
        """Tell the window once per distinct failure, then stay quiet."""
        if message == self._cloud_warning:
            return
        self._cloud_warning = message
        callback = self.on_notice
        if callback is None:
            return
        try:
            callback(f"Online model niet gebruikt: {message}")
        except Exception as exc:
            log.debug("notice callback failed: %s", exc)

    def _transcribe_local(self, audio) -> str:
        segments, info = self.model().transcribe(
            audio,
            language=(self.cfg.get("language") or None),
            beam_size=int(self.cfg.get("beam_size", 1)),
            vad_filter=True,
            vad_parameters={
                # Slightly stricter than the defaults: constant mic hum is what
                # makes Whisper invent phrases like "and then and and and" out
                # of an empty room.
                "threshold": 0.6,
                "min_silence_duration_ms": 500,
                "min_speech_duration_ms": 250,
            },
            initial_prompt=self._prompt(),
            condition_on_previous_text=False,
            no_speech_threshold=0.68,
        )
        parts = [seg.text.strip() for seg in segments]
        text = " ".join(p for p in parts if p)
        log.debug(
            "detected language=%s prob=%.2f duration=%.1fs",
            info.language,
            info.language_probability,
            info.duration,
        )
        return self._postprocess(" ".join(text.split()))

    def transcribe_preview(self, audio) -> str:
        """Cheap, low-latency pass for the live overlay.

        VAD roughly doubles the cost of a short slice and these slices are
        mostly speech anyway, so the preview skips it; the final paste still
        uses the full pipeline.
        """
        segments, _info = self.preview_model().transcribe(
            audio,
            language=(self.cfg.get("language") or None),
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        return self._postprocess(" ".join(" ".join(seg.text.strip() for seg in segments).split()))

    def transcribe_file(self, path: str) -> str:
        segments, _info = self.model().transcribe(
            path,
            language=(self.cfg.get("language") or None),
            beam_size=int(self.cfg.get("beam_size", 1)),
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
            initial_prompt=self._prompt(),
            condition_on_previous_text=False,
        )
        return self._postprocess(" ".join(" ".join(seg.text.strip() for seg in segments).split()))

    # -- delivery ---------------------------------------------------------
    def deliver(self, text: str) -> None:
        import pyperclip

        if self.cfg.get("post_space"):
            text = text + " "

        previous = None
        if self.cfg.get("restore_clipboard", True):
            try:
                previous = pyperclip.paste()
            except Exception:
                previous = None
        pyperclip.copy(text)

        mode = "clipboard" if self.args.no_paste else self.cfg.get("mode", "paste")
        if mode == "clipboard":
            log.info("copied to clipboard (%d chars)", len(text))
            return

        time.sleep(0.05)
        from pynput.keyboard import Controller, Key, KeyCode

        keyboard = Controller()
        if mode == "type":
            keyboard.type(text)
        else:
            # vk 0x56 is the physical 'V' key, which is what ctrl+v expects
            v_key = KeyCode.from_vk(0x56)
            keyboard.press(Key.ctrl_l)
            time.sleep(0.02)
            keyboard.press(v_key)
            keyboard.release(v_key)
            time.sleep(0.02)
            keyboard.release(Key.ctrl_l)
        log.info("pasted %d chars", len(text))

        if self.cfg.get("restore_clipboard", True) and previous is not None:
            time.sleep(0.6)
            try:
                pyperclip.copy(previous)
            except Exception:
                pass

    # -- recording --------------------------------------------------------
    def _start(self) -> None:
        device = resolve_device(self.cfg.get("input_device"))
        recorder = Recorder(device)
        self._cancel_requested.clear()
        self._ensure_level_thread()
        stream = None
        try:
            stream = self._make_stream(recorder.rate)
            self._stream = stream
            recorder.sink = self._on_audio_block
            self._recorder = recorder
            recorder.start()
        except Exception:
            # A busy or unplugged microphone must not leave the preview thread
            # running against a stream that will never receive audio.
            self._stream = None
            self._recorder = None
            recorder.sink = None
            if stream is not None:
                try:
                    stream.stop(timeout=1.0)
                except Exception as exc:
                    log.debug("could not stop preview after start failure: %s", exc)
            try:
                recorder.stop()
            except Exception as exc:
                log.debug("could not close recorder after start failure: %s", exc)
            raise
        self._record_started = time.time()
        log.info("recording at %d Hz (device: %s)", self._recorder.rate, self._recorder.device)
        beep("start", self.cfg.get("beeps", True))
        self._watchdog = threading.Thread(target=self._watch_max, daemon=True)
        self._watchdog.start()

    def _make_stream(self, rate: int):
        """Live preview is only worth it when something displays it."""
        if (
            not self.cfg.get("stream_preview", True)
            or not self.cfg.get("overlay", True)
            or self.on_partial is None
        ):
            return None
        from streaming import StreamTranscriber

        stream = StreamTranscriber(
            self.transcribe_preview,
            self._emit_partial,
            rate=rate,
            chunk_seconds=float(self.cfg.get("stream_chunk_seconds", 1.6)),
            overlap_seconds=float(self.cfg.get("stream_overlap_seconds", 0.8)),
            poll_seconds=float(self.cfg.get("stream_poll_seconds", 0.45)),
            max_seconds=float(self.cfg.get("max_seconds", 120)),
        )
        stream.start()
        log.debug("live preview started at %d Hz", rate)
        return stream

    def _ensure_level_thread(self) -> None:
        """Start the meter dispatcher once, outside the audio callback."""
        if self.on_level is None:
            return
        with self._level_lock:
            thread = self._level_thread
            if thread is not None and thread.is_alive():
                return
            self._level_thread = threading.Thread(
                target=self._level_loop,
                name="dictate-levels",
                daemon=True,
            )
            self._level_thread.start()

    def _queue_level(self, rms: float) -> None:
        """Publish the newest RMS; the audio callback never waits for the UI."""
        with self._level_lock:
            self._level_value = float(rms)
            self._level_event.set()
        self._ensure_level_thread()

    def _level_loop(self) -> None:
        while True:
            if not self._level_event.wait(0.5):
                continue
            self._level_event.clear()
            with self._level_lock:
                value = self._level_value
            callback = self.on_level
            if callback is not None:
                try:
                    callback(value)
                except Exception as exc:  # a broken meter must not stop capture
                    log.debug("level callback failed: %s", exc)
            # Coalesce callbacks when the UI is slower than the audio blocks.
            time.sleep(0.08)

    def _on_audio_block(self, block) -> None:
        stream = self._stream
        if stream is not None:
            stream.push(block)
        if self.on_level is not None:
            import numpy as np

            self._queue_level(float(np.sqrt(np.mean(block**2))))

    @staticmethod
    def _is_noise_only(text: str, audio) -> bool:
        """Whisper sometimes invents a word ("Oh") out of microphone noise.

        Such an invention is always a single very short word sitting on a very
        quiet recording, so both conditions must hold before the text is thrown
        away - a genuinely whispered "yes" is louder than this.
        """
        import numpy as np

        words = text.split()
        if len(words) != 1 or len(words[0]) > 3:
            return False
        if not getattr(audio, "size", 0):
            return True
        rms = float(np.sqrt(np.mean(audio**2)))
        log.debug("single-word result %r at rms %.4f", text, rms)
        return rms < 0.012

    def _emit_partial(self, text: str) -> None:
        callback = self.on_partial
        if callback is None:
            return
        try:
            callback(text)
        except Exception as exc:
            log.debug("partial callback failed: %s", exc)

    def _watch_max(self) -> None:
        limit = float(self.cfg.get("max_seconds", 120))
        while True:
            time.sleep(0.25)
            if self._state != "recording":
                return
            if time.time() - self._record_started >= limit:
                log.info("hit max_seconds (%ss), stopping", limit)
                self.toggle()
                return

    def _finish_recording(self) -> None:
        recorder = self._recorder
        if recorder is None or self._cancel_requested.is_set():
            self._set_state("idle")
            return
        audio = recorder.stop()
        rate = recorder.rate
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop(timeout=2.0)
        duration = audio.size / float(rate) if rate else 0.0
        log.info("stopped after %.1fs", duration)
        beep("stop", self.cfg.get("beeps", True))
        token = threading.Event()
        self._cancel_token = token
        threading.Thread(
            target=self._transcribe_and_deliver,
            args=(audio, rate, duration, token),
            daemon=True,
        ).start()

    def _transcribe_and_deliver(
        self, audio, rate: int, duration: float, token: threading.Event | None = None
    ) -> None:
        def cancelled() -> bool:
            return token is not None and token.is_set()

        try:
            if duration < float(self.cfg.get("min_seconds", 0.35)):
                log.warning("clip too short (%.2fs), ignored", duration)
                beep("error", self.cfg.get("beeps", True))
                return
            audio16 = to_16k_mono(audio, rate)
            started = time.time()
            text = self.transcribe_audio(audio16)
            log.info("transcribed in %.1fs: %r", time.time() - started, text)
            if cancelled():
                log.info("transcription cancelled before delivery")
                return
            if not text:
                log.warning("no speech detected")
                beep("error", self.cfg.get("beeps", True))
                return
            if self._is_noise_only(text, audio16):
                log.warning("ignored %r: too quiet to be speech", text)
                beep("error", self.cfg.get("beeps", True))
                return
            # Show the final wording, which may differ from the live preview.
            self._emit_partial(text)
            if cancelled():
                log.info("transcription cancelled before paste")
                return
            self.deliver(text)
            callback = self.on_delivered
            if callback is not None:
                try:
                    callback(
                        text,
                        {
                            "duration": duration,
                            "model": self._last_model,
                            "backend": self._last_backend,
                            "language": self.cfg.get("language") or "auto",
                            "mode": "clipboard"
                            if self.args.no_paste
                            else self.cfg.get("mode", "paste"),
                        },
                    )
                except Exception as exc:
                    log.debug("delivered callback failed: %s", exc)
            beep("done", self.cfg.get("beeps", True))
        except Exception as exc:
            log.error("transcription failed: %s", exc)
            log.debug("traceback", exc_info=True)
            beep("error", self.cfg.get("beeps", True))
        finally:
            if not cancelled():
                self._set_state("idle")

    def toggle(self) -> None:
        with self._toggle_lock:
            state = self._state
            if state == "transcribing":
                log.info("busy, ignoring hotkey")
                beep("busy", self.cfg.get("beeps", True))
                return
            if state == "idle":
                self._set_state("recording")
                try:
                    self._start()
                except Exception as exc:
                    log.error("could not start recording: %s", exc)
                    beep("error", self.cfg.get("beeps", True))
                    self._set_state("idle")
            else:
                self._set_state("transcribing")
                self._finish_recording()

    # -- single shot ------------------------------------------------------
    def record_once(self, seconds: float) -> None:
        device = resolve_device(self.cfg.get("input_device"))
        recorder = Recorder(device)
        rms_log = []
        recorder.start()
        log.info("recording %.0fs from device %s ...", seconds, recorder.device)
        for _ in range(int(seconds * 4)):
            time.sleep(0.25)
            if recorder.frames:
                import numpy as np

                chunk = recorder.frames[-1]
                rms_log.append(float(np.sqrt(np.mean(chunk**2))))
        audio = recorder.stop()
        rate = recorder.rate
        peak = max(rms_log) if rms_log else 0.0
        log.info("captured %.1fs at %d Hz, peak level %.4f", audio.size / rate, rate, peak)
        if peak < 0.0005:
            log.warning("input looks silent - wrong device or muted mic?")
        text = self.transcribe_audio(to_16k_mono(audio, rate))
        print(f"\nTranscript: {text!r}")

    # -- hotkey listener --------------------------------------------------
    def start_listening(self) -> None:
        """Arm the global hotkey. Safe to call when already listening."""
        from pynput import keyboard

        if self.listening:
            return
        hotkey = self.cfg.get("hotkey", "<f9>")
        hold = bool(self.cfg.get("hold_to_talk", False))
        self._hold_active = False
        if hold:
            keys = keyboard.HotKey.parse(hotkey)
            self._hold_keys = set(keys)
            self._hold_hotkey = keyboard.HotKey(keys, self._hold_press)
            # GlobalHotKeys only reports presses, so hold-to-talk needs the raw
            # listener to see the key coming back up.
            listener = keyboard.Listener(on_press=self._key_press, on_release=self._key_release)
        else:
            self._hold_hotkey = None
            self._hold_keys = set()
            listener = keyboard.GlobalHotKeys({hotkey: self.toggle})
        listener.start()
        self._listener = listener
        log.info(
            "listening for %s (%s, delivery: %s, beeps: %s)",
            hotkey,
            "hold to talk" if hold else "press to start/stop",
            "clipboard" if self.args.no_paste else self.cfg.get("mode", "paste"),
            self.cfg.get("beeps", True),
        )
        self._notify_listening()

    def _key_press(self, key) -> None:
        listener = self._listener
        if listener is None or self._hold_hotkey is None:
            return
        try:
            self._hold_hotkey.press(listener.canonical(key))
        except Exception as exc:
            log.debug("key press handling failed: %s", exc)

    def _key_release(self, key) -> None:
        listener = self._listener
        if listener is None or self._hold_hotkey is None:
            return
        canonical = listener.canonical(key)
        try:
            self._hold_hotkey.release(canonical)
        except Exception as exc:
            log.debug("key release handling failed: %s", exc)
        # Any key of the combo ending the hold is what a user expects; for a
        # single-key hotkey that is simply the key they were holding.
        if self._hold_active and canonical in self._hold_keys:
            self._hold_release()

    def _hold_press(self) -> None:
        if self._hold_active:
            return
        self._hold_active = True
        log.info("hold to talk: pressed")
        if self._state == "idle":
            self.toggle()
        elif self._state == "transcribing":
            beep("busy", self.cfg.get("beeps", True))

    def _hold_release(self) -> None:
        if not self._hold_active:
            return
        self._hold_active = False
        log.info("hold to talk: released")
        if self._state == "recording":
            self.toggle()

    def stop_listening(self) -> None:
        listener, self._listener = self._listener, None
        self._hold_active = False
        self._hold_hotkey = None
        self._hold_keys = set()
        if listener is not None:
            try:
                listener.stop()
            except Exception as exc:
                log.debug("listener stop failed: %s", exc)
            log.info("hotkey disabled")
        self._notify_listening()

    def _notify_listening(self) -> None:
        callback = self.on_state
        if callback is not None:
            try:
                callback(self._state)
            except Exception:
                pass

    def cancel(self) -> None:
        """Drop an in-flight recording or final pass without pasting it."""
        self._cancel_requested.set()
        token = self._cancel_token
        if token is not None:
            token.set()
        self.stop_listening()
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop(timeout=1.0)
        if self._state == "recording":
            recorder, self._recorder = self._recorder, None
            if recorder is not None:
                try:
                    recorder.stop()
                except Exception:
                    pass
        self._set_state("idle")

    def apply_config(self, cfg: dict) -> None:
        """Adopt edited settings, reloading only what actually changed."""
        old = self.cfg
        self.cfg = cfg
        if old.get("online_backend") != cfg.get("online_backend"):
            self.reset_cloud_warning()
        if any(old.get(k) != cfg.get(k) for k in ("hotkey", "hold_to_talk")) and self.listening:
            self.stop_listening()
            self.start_listening()
        reload_keys = (
            "model",
            "preview_model",
            "compute_type",
            "cpu_threads",
            "beam_size",
            "language",
        )
        if any(old.get(k) != cfg.get(k) for k in reload_keys):
            log.info("settings changed, reloading model %s", cfg.get("model"))
            with self._model_lock:
                self._model = None
            with self._preview_lock:
                self._preview_model = None
            threading.Thread(target=self.warmup, daemon=True).start()

    # -- main loop --------------------------------------------------------
    def run(self) -> None:
        self.start_listening()
        while self.listening:
            time.sleep(0.2)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Voice-to-text for any Windows app (Freebuff included); offline by default."
    )
    parser.add_argument("--config", default=str(CONFIG_PATH), help="path to config.json")
    parser.add_argument("--list-devices", action="store_true", help="show microphones and exit")
    parser.add_argument("--test-wav", metavar="FILE", help="transcribe an audio file and exit")
    parser.add_argument("--device", help="override input device (index or name fragment)")
    parser.add_argument("--model", help="override model (tiny/base/small/medium/large-v3 ...)")
    parser.add_argument("--no-paste", action="store_true", help="copy to clipboard only")
    parser.add_argument("--once", type=float, metavar="SECONDS", help="record once, print text, exit")
    parser.add_argument("--no-warmup", action="store_true", help="do not preload the model")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)

    if args.list_devices:
        list_devices()
        return 0

    cfg = load_config(Path(args.config))
    if args.model:
        cfg["model"] = args.model
    if args.device:
        cfg["input_device"] = args.device

    try:
        lock_handle = _acquire_single_instance()
    except RuntimeError:
        log.error("Freebuff Dictate is already running.")
        return 1

    dictator = Dictator(cfg, args)
    if str(cfg.get("online_backend") or "local") != "local":
        import cloud as cloud_mod

        credentials = cloud_mod.CredentialStore(
            Path(args.config).resolve().parent / "credentials.json"
        )
        dictator.cloud = cloud_mod.CloudTranscriber(credentials)
        dictator.on_notice = lambda message: print(f"  ! {message}")

    try:
        if args.test_wav:
            if not args.no_warmup:
                dictator.model()
            started = time.time()
            text = dictator.transcribe_file(args.test_wav)
            print(f"\nTranscript ({time.time() - started:.1f}s): {text}")
            return 0

        if args.once:
            dictator.model()
            dictator.record_once(args.once)
            return 0

        backend = str(cfg.get("online_backend") or "local")
        print("=" * 68)
        print("  Freebuff Dictate - " + ("online via Groq" if backend != "local" else "offline") + " voice to text")
        print(f"  hotkey: {cfg.get('hotkey')}   model: {cfg.get('model')}   "
              f"mode: {cfg.get('mode')}")
        print("  press the hotkey to start talking, press it again to paste")
        print("  Ctrl+C quits")
        print("=" * 68)
        if not args.no_warmup:
            threading.Thread(target=dictator.warmup, daemon=True).start()
        dictator.run()
    except KeyboardInterrupt:
        log.info("bye")
    finally:
        try:
            lock_handle.close()
        except Exception:
            pass
    return 0


def _acquire_single_instance():
    """Prevent two copies from both pasting the same sentence."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(DATA_DIR / "dictate.lock", "a+b")
    try:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except Exception as exc:
        handle.close()
        raise RuntimeError("locked") from exc
    return handle


if __name__ == "__main__":
    sys.exit(main())
