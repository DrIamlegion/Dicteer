#!/usr/bin/env python3
"""Self-test for Dicteer.

Checks the parts that can be verified without speaking or taking over the
keyboard: config loading, resampling, the record -> transcribe -> deliver state
machine, and the clipboard API. It never sends Ctrl+V.

Run:  .venv\\Scripts\\python.exe selftest.py
"""

from __future__ import annotations

import argparse
import http.server
import importlib.util
import io
import json
import os
import sys
import threading
import time
import wave
from importlib.machinery import SourceFileLoader
from pathlib import Path

import numpy as np

import autostart
import cloud
import crashlog
import desktop
import dictate as app
import history as history_mod
import learning as learning_mod
import settings as settings_mod
import textfix

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def test_config() -> None:
    cfg = app.load_config(app.CONFIG_PATH)
    missing = [k for k in app.DEFAULT_CONFIG if k not in cfg]
    check("config loads with all keys", not missing, f"missing={missing}" if missing else str(app.CONFIG_PATH))
    check("config hotkey is set", bool(cfg.get("hotkey")), cfg.get("hotkey"))

    # A first install seeds this file, not DEFAULT_CONFIG, so the shipped
    # vocabulary can drift from the product name without anything noticing.
    seeded = json.loads(
        (app.APP_DIR / "installer" / "default-config.json").read_text(encoding="utf-8")
    )
    prompt = str(seeded.get("initial_prompt") or "")
    check("the seeded config biases the product name", "Dicteer" in prompt, prompt)


def test_resample() -> None:
    t = np.linspace(0, 1.0, 48000, endpoint=False)
    tone48 = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    out48 = app.to_16k_mono(tone48, 48000)
    check("48 kHz -> 16 kHz length", abs(out48.size - 16000) <= 2, f"{out48.size} samples")

    tone441 = np.sin(2 * np.pi * 440 * t[:44100]).astype(np.float32)
    out441 = app.to_16k_mono(tone441, 44100)
    check("44.1 kHz -> 16 kHz length", abs(out441.size - 16000) <= 2, f"{out441.size} samples")

    passed = app.to_16k_mono(tone48[:16000], 16000)
    check("16 kHz passes through untouched", passed.size == 16000)
    check("output dtype is float32", out48.dtype == np.float32, str(out48.dtype))
    check("decimation keeps the tone amplitude", 0.9 < float(np.max(np.abs(out48))) <= 1.01,
          f"peak={float(np.max(np.abs(out48))):.2f}")


def test_level_dispatch() -> None:
    """The PortAudio callback must never wait for the window's meter."""
    cfg = dict(app.DEFAULT_CONFIG, beeps=False)
    dictator = app.Dictator(cfg, argparse.Namespace(no_paste=True))
    called = threading.Event()
    release = threading.Event()
    seen: list[tuple[float, str]] = []

    def meter(value: float) -> None:
        seen.append((value, threading.current_thread().name))
        called.set()
        release.wait(2.0)

    dictator.on_level = meter
    block = np.ones(256, dtype=np.float32)
    started = time.perf_counter()
    dictator._on_audio_block(block)
    elapsed = time.perf_counter() - started
    reached = called.wait(2.0)
    release.set()

    check("the audio callback does not wait for the meter", elapsed < 0.3, f"{elapsed:.3f}s")
    check("the level reaches the meter", reached and bool(seen), str(seen))
    check(
        "the meter runs off the audio thread",
        bool(seen) and seen[0][1] == "dictate-levels",
        seen[0][1] if seen else "no callback",
    )
    check("the meter carries the RMS", bool(seen) and abs(seen[0][0] - 1.0) < 0.01, str(seen[:1]))


def test_clipboard() -> None:
    try:
        import pyperclip

        previous = pyperclip.paste()
    except Exception as exc:
        check("clipboard readable", False, f"{type(exc).__name__}: {exc}")
        return
    marker = f"dicteer-selftest-{time.time():.0f}"
    try:
        pyperclip.copy(marker)
        check("clipboard round-trip", pyperclip.paste() == marker)
    finally:
        try:
            pyperclip.copy(previous if previous is not None else "")
        except Exception:
            pass


def _working_input():
    """A real input that actually delivers blocks on this machine.

    The Windows default can be Stereo Mix or an unplugged line input, which
    opens fine but never calls the audio callback. Tests should not depend on
    which device the user happens to have selected; this probes and picks one
    that speaks.
    """
    try:
        import sounddevice as sd
    except Exception:
        return None
    for index, info in enumerate(sd.query_devices()):
        if info["max_input_channels"] <= 0:
            continue
        if "stereo" in str(info["name"]).lower() or "mix" in str(info["name"]).lower():
            continue
        recorder = None
        try:
            recorder = app.Recorder(index)
            recorder.start()
            time.sleep(0.25)
            if recorder.frames:
                return index
        except Exception:
            pass
        finally:
            if recorder is not None:
                try:
                    recorder.stop()
                except Exception:
                    pass
    return None


def test_state_machine() -> None:
    cfg = dict(app.DEFAULT_CONFIG)
    cfg["min_seconds"] = 0.2
    cfg["beeps"] = False
    cfg["model"] = "tiny.en"  # not used: transcription is stubbed out
    cfg["input_device"] = _working_input()
    args = argparse.Namespace(no_paste=True)

    dicta = app.Dictator(cfg, args)
    delivered: list[str] = []
    dicta.transcribe_audio = lambda audio: (  # type: ignore[assignment]
        f"self test ok {audio.size}"
        if getattr(audio, "size", 0) > 1000
        else ""
    )
    dicta.deliver = lambda text: delivered.append(text)  # type: ignore[assignment]

    dicta.toggle()  # start
    recording = dicta._state == "recording"
    check("hotkey starts recording", recording, dicta._state)
    time.sleep(1.5)
    dicta.toggle()  # stop -> worker thread
    check("hotkey stops recording", dicta._state == "transcribing", dicta._state)

    deadline = time.time() + 15
    while dicta._state != "idle" and time.time() < deadline:
        time.sleep(0.05)
    check("returns to idle after transcription", dicta._state == "idle", dicta._state)
    check("text was delivered once", len(delivered) == 1, str(delivered))
    check("delivered text carries audio samples", bool(delivered) and "self test ok" in delivered[0],
          delivered[0] if delivered else "")

    dicta.toggle()
    started = dicta._state == "recording"
    time.sleep(0.4)  # long enough to clear min_seconds, so the worker really runs
    dicta.toggle()
    deadline = time.time() + 15
    while dicta._state != "idle" and time.time() < deadline:
        time.sleep(0.05)
    check(
        "second cycle can start again and deliver again",
        started and len(delivered) == 2,
        f"started={started} delivered={delivered}",
    )


def test_start_failure_cleanup() -> None:
    """A microphone that fails to open must not leave the preview running."""
    cfg = dict(app.DEFAULT_CONFIG, beeps=False)
    dicta = app.Dictator(cfg, argparse.Namespace(no_paste=True))
    recorders: list = []

    class FakeStream:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self, timeout: float = 4.0) -> str:
            self.stopped = True
            return ""

    class FailingRecorder:
        def __init__(self, device) -> None:
            self.rate = 16000
            self.device = device
            self.sink = None
            self.stopped = False
            recorders.append(self)

        def start(self) -> None:
            raise RuntimeError("microphone busy")

        def stop(self):
            self.stopped = True
            return np.zeros(0, dtype=np.float32)

    stream = FakeStream()
    original_recorder = app.Recorder
    app.Recorder = FailingRecorder  # type: ignore[assignment]
    dicta._make_stream = lambda rate: stream  # type: ignore[assignment]
    try:
        dicta.toggle()
    finally:
        app.Recorder = original_recorder

    check("a failed microphone start returns to idle", dicta._state == "idle", dicta._state)
    check("a failed microphone start stops the preview stream", stream.stopped)
    check("a failed microphone start detaches the preview", dicta._stream is None, str(dicta._stream))
    check("a failed microphone start detaches the recorder", dicta._recorder is None, str(dicta._recorder))
    check(
        "a failed microphone start closes its recorder",
        bool(recorders) and recorders[-1].stopped,
        str([recorder.stopped for recorder in recorders]),
    )


def test_start_recovery() -> None:
    """After a failed microphone start, the very next dictation must work."""
    cfg = dict(app.DEFAULT_CONFIG, beeps=False, min_seconds=0.2)
    dicta = app.Dictator(cfg, argparse.Namespace(no_paste=True))
    delivered: list[str] = []
    streams: list = []

    class FakeStream:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self, timeout: float = 4.0) -> str:
            self.stopped = True
            return ""

    class FlakyRecorder:
        attempts = 0

        def __init__(self, device) -> None:
            FlakyRecorder.attempts += 1
            self.attempt = FlakyRecorder.attempts
            self.rate = 16000
            self.device = device
            self.sink = None

        def start(self) -> None:
            if self.attempt == 1:
                raise RuntimeError("microphone busy")

        def stop(self):
            return np.ones(16000, dtype=np.float32)

    def make_stream(rate: int):
        stream = FakeStream()
        streams.append(stream)
        return stream

    original_recorder = app.Recorder
    app.Recorder = FlakyRecorder  # type: ignore[assignment]
    dicta._make_stream = make_stream  # type: ignore[assignment]
    dicta.transcribe_audio = lambda audio: "recovered dictation" if getattr(audio, "size", 0) > 1000 else ""
    dicta.deliver = delivered.append  # type: ignore[assignment]
    try:
        dicta.toggle()
        failed_cleanly = (
            dicta._state == "idle"
            and len(streams) == 1
            and streams[0].stopped
            and dicta._stream is None
        )
        check("the first microphone start fails and cleans up", failed_cleanly, dicta._state)
        if not failed_cleanly:
            return

        dicta.toggle()
        recovered_start = dicta._state == "recording"
        check("the next recording starts normally", recovered_start, dicta._state)
        if not recovered_start:
            return
        dicta.toggle()
        deadline = time.time() + 5
        while dicta._state != "idle" and time.time() < deadline:
            time.sleep(0.02)
    finally:
        app.Recorder = original_recorder
        dicta.cancel()

    check("the recovered recording returns to idle", dicta._state == "idle", dicta._state)
    check("the recovered recording is delivered", delivered == ["recovered dictation"], str(delivered))
    check("the recovered preview is stopped", len(streams) == 2 and streams[1].stopped, str([s.stopped for s in streams]))
    check("the recovered recording ends with no preview attached", dicta._stream is None, str(dicta._stream))


def test_merge_overlap() -> None:
    from streaming import merge_overlap

    check("empty committed text passes through", merge_overlap("", "hello world") == "hello world")
    check(
        "overlap is trimmed word by word",
        merge_overlap("open the config file", "the config file and save") == "and save",
    )
    check(
        "overlap ignores case and punctuation",
        merge_overlap("Open the config, file.", "the config file and save") == "and save",
    )
    check(
        "a fully repeated tail adds nothing",
        merge_overlap("alpha beta gamma", "alpha beta gamma") == "",
    )
    check(
        "unrelated text is appended whole",
        merge_overlap("alpha beta", "delta epsilon") == "delta epsilon",
    )


def test_stream_session() -> None:
    """Drive the incremental streamer with a stub that behaves like a model."""
    import streaming

    rate = 16000
    word_samples = 4000  # 0.25 s per word
    truth = [f"word{i}" for i in range(24)]
    pushed = {"samples": 0}

    def fake_transcribe(audio) -> str:
        # A faithful stand-in: it only knows the words contained in the audio
        # it is handed, exactly like a real model transcribing that slice.
        # Each word is encoded as a constant amplitude, so the transcript does
        # not depend on when the worker happens to pick up the slice (the old
        # version inferred the slice from a shared sample counter and dropped a
        # word whenever a push landed mid-transcription).
        found: list[str] = []
        previous = 0.0
        for value in audio:
            code = int(round(float(value)))
            if code and code != previous and 1 <= code <= len(truth):
                found.append(truth[code - 1])
            previous = code
        return " ".join(found)

    partials: list[str] = []
    stream = streaming.StreamTranscriber(
        fake_transcribe,
        partials.append,
        rate=rate,
        chunk_seconds=0.4,
        overlap_seconds=0.4,
        poll_seconds=0.02,
    )
    stream.start()

    blocks = 0
    for index in range(len(truth)):
        block = np.full(word_samples, index + 1, dtype=np.float32)
        stream.push(block)
        pushed["samples"] += word_samples
        blocks += 1
        time.sleep(0.02)

    deadline = time.time() + 10
    while len(stream.text.split()) < len(truth) and time.time() < deadline:
        time.sleep(0.02)
    text = stream.stop()

    check("stream produced preview updates", len(partials) >= 3, f"{len(partials)} updates")
    check("stream ran several transcription passes", stream.previews >= 3, f"{stream.previews} passes")
    check(
        "stream text equals the spoken words exactly (no duplicates, no gaps)",
        text.split() == truth,
        f"got {len(text.split())} of {len(truth)} words: {text[-60:]!r}",
    )
    check("last preview matches the final text", partials and partials[-1] == text)
    check("word count is monotonic", all(
        len(a.split()) <= len(b.split()) for a, b in zip(partials, partials[1:])
    ))


def test_stream_with_model() -> None:
    """The same path, but transcribed by the real model from a real clip."""
    import streaming

    clip = app.APP_DIR / "sample_tts.wav"
    if not clip.exists():
        print("[SKIP] sample_tts.wav not found")
        return

    from faster_whisper.audio import decode_audio

    # Streaming mechanics are language-independent; use the cached English
    # model so this check stays fast whatever the user's config says.
    cfg = app.load_config(app.CONFIG_PATH)
    cfg.update({"beeps": False, "model": "base.en", "preview_model": "", "language": "en"})
    dicta = app.Dictator(cfg, argparse.Namespace(no_paste=True))
    dicta.model()
    audio = decode_audio(str(clip), sampling_rate=16000)
    expected = dicta.transcribe_audio(audio)

    partials: list[str] = []
    stream = streaming.StreamTranscriber(
        dicta.transcribe_preview if hasattr(dicta, "transcribe_preview") else dicta.transcribe_audio,
        partials.append,
        rate=16000,
        chunk_seconds=1.2,
        overlap_seconds=0.6,
        poll_seconds=0.05,
    )
    stream.start()
    block = 8000  # 0.5 s
    first_six = int(16000 * 6)
    for start in range(0, min(audio.size, first_six), block):
        stream.push(audio[start : start + block])
        time.sleep(0.02)
    # Preview passes are real inference: on a busy CPU one 1.2 s slice can take
    # several seconds, so wait for results instead of assuming a fixed deadline.
    deadline = time.time() + 90
    while time.time() < deadline and len(partials) < 2:
        time.sleep(0.2)
    live = stream.stop()

    def content(text: str) -> set[str]:
        import re

        return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2}

    truth_words, live_words = content(expected), content(live)
    overlap = len(truth_words & live_words) / max(1, len(truth_words))
    check("live preview matches the clip", overlap >= 0.6, f"{overlap:.0%} of words, live={live!r}")
    check("live preview is not empty", bool(live_words))
    check("live preview produced intermediate updates", len(partials) >= 2, f"{len(partials)}")


def test_dutch() -> None:
    """The delivered configuration, on real Dutch speech."""
    from faster_whisper.audio import decode_audio

    cfg = app.load_config(app.CONFIG_PATH)
    language = cfg.get("language")
    model = str(cfg.get("model", ""))
    if language != "nl" or model.endswith(".en"):
        print("[SKIP] config is not set to Dutch")
        return

    dicta = app.Dictator(cfg, argparse.Namespace(no_paste=True))
    dicta.model()

    plain = app.APP_DIR / "sample_nl.mp3"
    tech = app.APP_DIR / "sample_nl_tech.mp3"
    if not plain.exists() or not tech.exists():
        print("[SKIP] Dutch sample clips missing")
        return

    started = time.time()
    first = decode_audio(str(plain), sampling_rate=16000)
    text = dicta.transcribe_audio(first)
    elapsed = time.time() - started
    print(f"       nl: {text!r} ({elapsed:.1f}s for {first.size / 16000:.1f}s audio)")
    low = text.lower()
    check("Dutch sentence is transcribed", "donkere modus" in low, text)
    check(
        "Dutch compound word is recognised (base heard 'inluchtpagina')",
        "instellingenpagina" in low.replace(" ", ""),
        text,
    )

    second = decode_audio(str(tech), sampling_rate=16000)
    text2 = dicta.transcribe_audio(second)
    low2 = text2.lower()
    print(f"       nl-tech: {text2!r}")
    check("Dutch technical terms survive", "websocket" in low2 and "maximaal" in low2, text2)
    check("Dutch numbers or accents survive", "vijf" in low2 or "5" in low2, text2)

    dicta.preview_model()
    expected_preview = str(cfg.get("preview_model") or cfg.get("model"))
    check(
        "overlay previews use the configured fast model",
        dicta._preview_name == expected_preview,
        f"{dicta._preview_name} != {expected_preview}",
    )
    snippet = dicta.transcribe_preview(first[: int(1.6 * 16000)])
    check("preview model speaks Dutch too", "donker" in snippet.lower(), snippet)

    # Push the clip through the real delivery path (clipboard only, no keystrokes).
    import pyperclip

    previous = pyperclip.paste()
    try:
        dicta._transcribe_and_deliver(first, 16000, first.size / 16000.0)
        pasted = pyperclip.paste()
    finally:
        try:
            pyperclip.copy(previous if previous is not None else "")
        except Exception:
            pass
    check(
        "Dutch text reaches the clipboard through the delivery path",
        "instellingenpagina" in pasted.lower(),
        pasted,
    )


def test_noise_guard() -> None:
    quiet = np.full(16000, 0.001, dtype=np.float32)
    loud = np.full(16000, 0.05, dtype=np.float32)
    check("a lone short word on a quiet clip is dropped", app.Dictator._is_noise_only("Oh", quiet))
    check("the same word on a loud clip is kept", not app.Dictator._is_noise_only("Yes", loud))
    check("a real sentence is always kept", not app.Dictator._is_noise_only("Oh no", quiet))
    check("long single words are kept", not app.Dictator._is_noise_only("noted", quiet))


def test_hold_to_talk() -> None:
    from pynput.keyboard import Key

    cfg = dict(app.DEFAULT_CONFIG)
    cfg.update(
        {
            "beeps": False,
            "min_seconds": 0.2,
            "hold_to_talk": True,
            "stream_preview": False,
            "model": "tiny.en",
            "input_device": _working_input(),
        }
    )
    dicta = app.Dictator(cfg, argparse.Namespace(no_paste=True))
    delivered: list[str] = []
    dicta.transcribe_audio = lambda audio: "held words" if getattr(audio, "size", 0) > 1000 else ""
    dicta.deliver = lambda text: delivered.append(text)
    dicta.on_partial = lambda text: None

    dicta.start_listening()
    check("hold mode arms a raw key listener", dicta.listening)

    dicta._key_press(Key.f9)
    pressed = dicta._state == "recording"
    check("pressing the hotkey starts recording", pressed, dicta._state)
    time.sleep(0.6)
    check("still recording while the key is held", dicta._state == "recording", dicta._state)
    dicta._key_release(Key.f9)

    deadline = time.time() + 15
    while dicta._state != "idle" and time.time() < deadline:
        time.sleep(0.05)
    check("releasing the hotkey stops and pastes", len(delivered) == 1 and delivered[0] == "held words", str(delivered))

    dicta._key_press(Key.f9)
    time.sleep(0.02)
    dicta._key_release(Key.f9)
    deadline = time.time() + 10
    while dicta._state != "idle" and time.time() < deadline:
        time.sleep(0.05)
    check("a quick tap is discarded", len(delivered) == 1, str(delivered))

    dicta.cfg["hold_to_talk"] = False
    dicta.stop_listening()
    dicta.start_listening()
    check("toggle mode still arms GlobalHotKeys", dicta.listening and dicta._hold_hotkey is None)
    dicta.stop_listening()


def test_overlay_states() -> None:
    import ctypes
    import ctypes.wintypes as wt

    overlay_module = importlib.util.spec_from_file_location(
        "overlay_mod", str(app.APP_DIR / "overlay.py")
    )
    module = importlib.util.module_from_spec(overlay_module)
    overlay_module.loader.exec_module(module)

    user32 = ctypes.windll.user32
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

    def overlay_windows() -> list[tuple[int, bool]]:
        found: list[tuple[int, bool]] = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def callback(hwnd, _lp):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != os.getpid():
                return True
            cls = ctypes.create_unicode_buffer(256)
            txt = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            user32.GetWindowTextW(hwnd, txt, 256)
            if cls.value == "TkTopLevel" and txt.value == module.TITLE:
                found.append((hwnd, bool(user32.IsWindowVisible(hwnd))))
            return True

        user32.EnumWindows(callback, 0)
        return found

    foreground = user32.GetForegroundWindow()
    overlay = module.Overlay()
    deadline = time.time() + 10
    while time.time() < deadline and not any(visible for _h, visible in overlay_windows()):
        overlay.show("recording")
        time.sleep(0.3)
    overlay.set_text("the quick brown fox jumps over the lazy dog")
    time.sleep(0.6)

    windows = overlay_windows()
    visible = [h for h, shown in windows if shown]
    check("overlay window is on screen", bool(visible), str(windows))
    if visible:
        hwnd = visible[0]
        style = user32.GetWindowLongW(hwnd, module.GWL_EXSTYLE)
        check(
            "overlay is marked not to take focus",
            bool(style & module.WS_EX_NOACTIVATE),
            hex(style),
        )
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        check("overlay uses the designed width", abs(width - module.WIDTH) <= 4, str(width))
        screen_h = user32.GetSystemMetrics(1)
        check(
            "overlay sits near the bottom of the screen",
            rect.bottom > screen_h * 0.6,
            f"bottom={rect.bottom} screen={screen_h}",
        )
    check("overlay never stole keyboard focus", user32.GetForegroundWindow() == foreground)

    overlay.hide()
    deadline = time.time() + 10
    while time.time() < deadline and any(shown for _h, shown in overlay_windows()):
        time.sleep(0.2)
    check("overlay hides again", not any(shown for _h, shown in overlay_windows()))
    overlay.close()


def load_tray():
    """tray.pyw is not importable by name; load it from its path."""
    path = app.APP_DIR / "tray.pyw"
    loader = SourceFileLoader("tray_app", str(path))
    spec = importlib.util.spec_from_loader("tray_app", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_hotkey_validation() -> None:
    for good in ("<f9>", "<ctrl>+<alt>+d", "<ctrl>+<shift>+<space>"):
        try:
            app.validate_hotkey(good)
        except ValueError as exc:
            check(f"accepts hotkey {good}", False, str(exc))
            return
    for bad in ("f9", "", "<notakey>"):
        try:
            app.validate_hotkey(bad)
        except ValueError:
            continue
        check(f"rejects invalid hotkey {bad!r}", False, "no error raised")
        return
    check("hotkey validation accepts valid combos and rejects invalid ones", True)


def test_listen_control() -> None:
    cfg = dict(app.DEFAULT_CONFIG)
    cfg["beeps"] = False
    dicta = app.Dictator(cfg, argparse.Namespace(no_paste=True))

    seen: list[str] = []
    dicta.on_state = seen.append

    dicta.start_listening()
    check("start_listening arms the hotkey", dicta.listening)

    rearmed = dict(cfg)
    rearmed["hotkey"] = "<f8>"
    dicta.apply_config(rearmed)
    check(
        "apply_config re-arms a changed hotkey",
        dicta.listening and dicta.cfg["hotkey"] == "<f8>",
        dicta.cfg["hotkey"],
    )

    dicta.stop_listening()
    check("stop_listening disarms the hotkey", not dicta.listening)
    check("state callbacks fired", bool(seen), str(seen))

    quiet = dict(cfg)
    quiet["mode"] = "clipboard"
    dicta.apply_config(quiet)
    check("non-model changes do not touch the model", dicta._model is None)


def test_shell_ui() -> None:
    """The tray menu, status lines and icons, without opening any window."""
    import tempfile

    tray = load_tray()

    images = {key: tray.mic_image(color) for key, color in tray.COLORS.items()}
    sizes_ok = all(img.size == (tray.SIZE, tray.SIZE) and img.mode == "RGBA" for img in images.values())
    check("tray icons are 64x64 RGBA", sizes_ok)
    centers = {key: img.getpixel((32, 20)) for key, img in images.items()}
    check(
        "each state has its own colour",
        len(set(centers.values())) == len(centers),
        str(centers),
    )
    check("idle icon is transparent at the corner", images["idle"].getpixel((1, 1))[3] == 0)

    with tempfile.TemporaryDirectory() as tmp:
        cfg = dict(app.DEFAULT_CONFIG)
        cfg.update({"overlay": False, "beeps": False})  # no Tk thread, no sounds
        shell = tray.Shell(cfg, Path(tmp) / "config.json")
        data_dir = Path(tmp).resolve()
        check("history follows the config that produced it", shell.history.path.parent == data_dir, str(shell.history.path))
        check("learning follows the config too", shell.learning.path.parent == data_dir, str(shell.learning.path))
        check("the crash log follows the config too",
              shell.crash_log_path == data_dir / "crash.log", str(shell.crash_log_path))

        entries = list(shell.menu())
        check("tray menu builds", len(entries) >= 9, f"{len(entries)} entries")
        check("status line says paused while disarmed", "Gepauzeerd" in shell.status_text())
        check("tooltip says the hotkey is off", "sneltoets uit" in shell._tooltip())
        check("record item offers to record", shell.record_text() == "Opnemen")

        shell.dictator.start_listening()
        check("status line says ready when armed", "Klaar" in shell.status_text())
        check("tooltip mentions the hotkey", "houd" in shell._tooltip().lower())
        shell.dictator._set_state("recording")
        check("status line follows recording", "Opnemen" in shell.status_text())
        check("tooltip follows recording", "opnemen" in shell._tooltip().lower())
        check("record item flips to stop", "Stop" in shell.record_text())
        shell.dictator._set_state("idle")
        check("record item returns to record", shell.record_text() == "Opnemen")
        shell.dictator.stop_listening()
        check("disarmed icon is the grey one", shell._current_key() == "off")

        shell.history.add("een twee drie", duration=1.5, model="small", language="nl", mode="paste")
        stats = shell.stats()
        check("the dashboard stats count today's words", stats["today_count"] == 1 and stats["today_words"] == 3, str(stats))
        check("history payload carries stats", shell.history_payload()["stats"]["total_words"] == 3)

        item = shell.history.add("De inluchtpagina is traag.", duration=2.0, model="small", language="nl", mode="paste")
        corrected = shell.correct_history(item["id"], "De inlogpagina is traag.")
        check("a window correction updates the stored text", shell.history.get(item["id"])["text"] == "De inlogpagina is traag.")
        check("a window correction teaches the engine", shell.learning.apply_text("inluchtpagina") == "inlogpagina")
        check("the correction response carries the learning",
              corrected["learning"]["corrections"][0]["heard"] == "inluchtpagina", str(corrected["learning"]["corrections"]))
        check("the engine prompt gets technical vocabulary only", shell.learning.prompt_extra() == "", shell.learning.prompt_extra())
        added = shell.add_vocabulary("features")
        check("the window can add a word by hand",
              added["ok"] and any(row["word"] == "features" for row in added["learning"]["vocabulary"]),
              str(added.get("learning")))
        check("a hand-added word reaches the model prompt",
              "features" in shell.learning.prompt_extra(), shell.learning.prompt_extra())
        refused = shell.add_vocabulary("twee woorden")
        check("a hand-added word refuses spaces",
              refused["ok"] is False and bool(refused.get("error")), str(refused))
        check("the shell puts a spoken version back together before delivery",
              shell.dictator._postprocess("Dicteer versie 2.01") == "Dicteer versie 2.0.1",
              shell.dictator._postprocess("Dicteer versie 2.01"))

        shell.cfg["learn_from_corrections"] = False
        second = shell.history.add("De inluchtpagina is weer traag.", duration=2.0, model="small", language="nl", mode="paste")
        shell.correct_history(second["id"], "De inlogpagina is weer traag.")
        check("the learning switch stops new learning",
              len(shell.learning.corrections()) == 1 and shell.learning.corrections()[0]["count"] == 1,
              str(shell.learning.corrections()))
        check("the corrected text is still saved when learning is off",
              shell.history.get(second["id"])["text"] == "De inlogpagina is weer traag.")
        shell.cfg["learn_from_corrections"] = True

        class FakeUI:
            def __init__(self) -> None:
                self.toasts: list[tuple[str, str]] = []
                self.storage: list[str] = []

            def emit_toast(self, text: str, kind: str = "ok") -> None:
                self.toasts.append((text, kind))

            def emit_storage_warnings(self, warnings: list[str]) -> None:
                self.storage = list(warnings)

        fake_ui = FakeUI()
        shell.ui = fake_ui  # type: ignore[assignment]
        blocked = Path(tmp) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        shell.history.path = blocked / "history.json"
        shell.history.add("niet opgeslagen")
        shell.history.add("nog steeds niet")
        check(
            "history write failures reach the window as one toast",
            len(fake_ui.toasts) == 1
            and fake_ui.toasts[0][1] == "error"
            and "geschiedenis" in fake_ui.toasts[0][0].lower(),
            str(fake_ui.toasts),
        )
        check(
            "history write failures stay in the storage banner",
            len(fake_ui.storage) == 1 and "geschiedenis" in fake_ui.storage[0].lower(),
            str(fake_ui.storage),
        )
        shell.learning.path = blocked / "learning.json"
        shell.learning.learn_from_diff("De inluchtpagina is traag.", "De inlogpagina is traag.")
        check(
            "learning write failures also reach the window",
            len(fake_ui.toasts) == 2 and "correcties" in fake_ui.toasts[1][0].lower(),
            str(fake_ui.toasts),
        )
        check(
            "learning write failures join the storage banner",
            len(fake_ui.storage) == 2 and "correcties" in fake_ui.storage[1].lower(),
            str(fake_ui.storage),
        )


def test_settings_validation() -> None:
    """The one place where settings are validated and written."""
    import tempfile

    base = dict(app.DEFAULT_CONFIG)
    base.update({"model": "small", "language": "nl", "hotkey": "<f9>"})

    try:
        settings_mod.apply_updates(base, {"hotkey": "f9"})
    except settings_mod.SettingsError:
        check("invalid hotkey is refused by the settings layer", True)
    else:
        check("invalid hotkey is refused by the settings layer", False, "no error raised")

    try:
        settings_mod.apply_updates(base, {"model": "small.en"})
    except settings_mod.SettingsError as exc:
        check("an English-only model is refused for Dutch", "alleen Engels" in str(exc), str(exc))
    else:
        check("an English-only model is refused for Dutch", False, "no error raised")

    merged = settings_mod.apply_updates(
        base,
        {
            "mode": "clipboard",
            "beeps": False,
            "post_space": "false",
            "initial_prompt": "  Freebuff, Codebuff  ",
            "cpu_threads": "6",
            "input_device": "default",
        },
    )
    check("settings merge the delivery mode", merged["mode"] == "clipboard")
    check("settings merge booleans", merged["beeps"] is False and merged["post_space"] is False)
    check("the vocabulary prompt is trimmed", merged["initial_prompt"] == "Freebuff, Codebuff")
    check("numeric fields are converted", merged["cpu_threads"] == 6, str(merged["cpu_threads"]))
    check("the system microphone becomes null", merged["input_device"] is None)
    check("correction learning defaults to on", merged["learn_from_corrections"] is True)
    check("correction learning can be switched off",
          settings_mod.apply_updates(base, {"learn_from_corrections": False})["learn_from_corrections"] is False)
    check("crash logging defaults to on", merged["crash_logging"] is True)
    check("crash logging can be switched off",
          settings_mod.apply_updates(base, {"crash_logging": False})["crash_logging"] is False)

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "config.json"
        settings_mod.save(target, merged)
        saved = json.loads(target.read_text(encoding="utf-8"))
        check("save writes every config key", set(app.DEFAULT_CONFIG) <= set(saved))
        check("save keeps the Dutch prompt readable", saved["initial_prompt"] == "Freebuff, Codebuff")
        check("save leaves no temporary file behind", not target.with_suffix(".json.tmp").exists())
        reloaded = settings_mod.load(target)
        check("a saved config loads back", reloaded["mode"] == "clipboard" and reloaded["language"] == "nl")

    check("english is allowed with an english-only model",
          settings_mod.apply_updates(dict(base, language="en"), {"model": "base.en"})["model"] == "base.en")
    check("the online backend defaults to local", base["online_backend"] == "local")
    check("the online backend can be switched to Groq",
          settings_mod.apply_updates(base, {"online_backend": "groq"})["online_backend"] == "groq")
    check("the online backend can be switched to Gemini",
          settings_mod.apply_updates(base, {"online_backend": "gemini"})["online_backend"] == "gemini")
    check("the Gemini model defaults to the fast one",
          settings_mod.apply_updates(base, {"online_backend": "gemini"})["gemini_model"] == cloud.GEMINI_MODEL)
    check("a slower Gemini model can be chosen",
          settings_mod.apply_updates(
              base, {"online_backend": "gemini", "gemini_model": "gemini-3.6-flash"}
          )["gemini_model"] == "gemini-3.6-flash")
    try:
        settings_mod.apply_updates(
            base, {"online_backend": "gemini", "gemini_model": "gemini-9-ultra"}
        )
    except settings_mod.SettingsError as exc:
        check("an unknown Gemini model is refused", "Gemini-model" in str(exc), str(exc))
    else:
        check("an unknown Gemini model is refused", False, "no error raised")
    check("every offered Gemini model is accepted",
          all(
              settings_mod.apply_updates(
                  base, {"online_backend": "gemini", "gemini_model": entry["value"]}
              )["gemini_model"] == entry["value"]
              for entry in cloud.GEMINI_MODELS
          )) 
    try:
        settings_mod.apply_updates(base, {"online_backend": "somewhere"})
    except settings_mod.SettingsError as exc:
        check("an unknown online backend is refused", "lokaal, Groq of Gemini" in str(exc), str(exc))
    else:
        check("an unknown online backend is refused", False, "no error raised")
    try:
        settings_mod.apply_updates(base, {"online_timeout_seconds": 1})
    except settings_mod.SettingsError:
        check("an unusable online timeout is refused", True)
    else:
        check("an unusable online timeout is refused", False, "no error raised")


def test_crashlog() -> None:
    """A crash report is written without any user text or secrets."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "crash.log"
        try:
            raise RuntimeError("this is a crash log test")
        except RuntimeError:
            crashlog.write_report(
                target,
                *sys.exc_info(),
                source="selftest",
                version="test-version",
                config_path=Path(tmp) / "config.json",
            )
        text = target.read_text(encoding="utf-8")
        check("crash log records the exception", "RuntimeError: this is a crash log test" in text)
        check("crash log records the source and version", "selftest" in text and "test-version" in text)
        check("crash log does not include a Groq key", "gsk_" not in text)
        check("crash log is written next to the config", target.parent == Path(tmp))


def append_faulthandler_dump(target: Path, label: str) -> None:
    """Append what faulthandler writes for a Windows exception, verbatim.

    ``label`` is whatever follows the colon, e.g. ``code 0x8001010d`` for a COM
    error or ``access violation`` for a real native crash.
    """
    with Path(target).open("a", encoding="utf-8") as handle:
        handle.write(
            f"Windows fatal exception: {label}\n\n"
            "Thread 0x00001b30 (most recent call first):\n"
            '  File "threading.py", line 359 in wait\n'
            '  File "tqdm\\_monitor.py", line 69 in run\n\n'
            "Current thread 0x00003ad0 (most recent call first):\n"
            '  File "webview\\platforms\\winforms.py", line 808 in create\n'
        )


def test_crash_notice() -> None:
    """Only an unclean run with a new crash report raises a startup notice."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "crash.log"
        crashlog.install(
            enabled=False,
            log_path=target,
            version="notice-test",
            config_path=Path(tmp) / "config.json",
        )
        check("the first launch has no crash notice", not crashlog.previous_run()["pending"])

        crashlog.set_enabled(True)
        first = crashlog.begin_run(crashlog.previous_run(), logging=True)
        check("a clean start has no crash notice", not first["pending"], str(first))
        crashlog.end_run()
        check("a clean run removes its marker", not (Path(tmp) / "crash-run.json").exists())
        check("a clean run leaves the next start quiet", not crashlog.previous_run()["pending"])

        crashlog.begin_run(crashlog.previous_run(), logging=True)
        try:
            raise RuntimeError("notice test")
        except RuntimeError:
            crashlog.write_report(target, *sys.exc_info(), source="selftest")
        crashlog.set_enabled(False)
        previous = crashlog.previous_run()
        check("a crash after the marker is noticed", previous["pending"], str(previous))

        crashlog.set_enabled(True)
        notice = crashlog.begin_run(previous, logging=True)
        check("the notice survives the new run", notice["pending"] and bool(notice.get("started")), str(notice))
        crashlog.end_run()
        crashlog.set_enabled(False)
        check("cleaning up silences the next start", not crashlog.previous_run()["pending"])

        # WebView2 raises RPC_E_CANTCALLOUT_ININPUTSYNCCALL, faulthandler dumps
        # it, and the app carries on.  That dump must not be announced as a
        # crash when the next window opens.
        crashlog.set_enabled(True)
        crashlog.begin_run(crashlog.previous_run(), logging=True)
        append_faulthandler_dump(target, "code 0x8001010d")
        crashlog.set_enabled(False)
        previous = crashlog.previous_run()
        check("a handled COM exception is not announced as a crash", not previous["pending"], str(previous))
        crashlog.end_run()

        crashlog.set_enabled(True)
        crashlog.begin_run(crashlog.previous_run(), logging=True)
        append_faulthandler_dump(target, "code 0xc0000005")
        crashlog.set_enabled(False)
        previous = crashlog.previous_run()
        check("an unrecognised exception code is still announced", previous["pending"], str(previous))
        crashlog.end_run()

        crashlog.set_enabled(True)
        crashlog.begin_run(crashlog.previous_run(), logging=True)
        append_faulthandler_dump(target, "access violation")
        crashlog.set_enabled(False)
        previous = crashlog.previous_run()
        check("a native access violation is still announced", previous["pending"], str(previous))
        crashlog.end_run()

        crashlog.set_enabled(True)
        crashlog.begin_run(crashlog.previous_run(), logging=True)
        append_faulthandler_dump(target, "code 0x8001010d")
        try:
            raise RuntimeError("crash after a COM dump")
        except RuntimeError:
            crashlog.write_report(target, *sys.exc_info(), source="selftest")
        crashlog.set_enabled(False)
        previous = crashlog.previous_run()
        check(
            "a real report after a COM dump is still announced",
            previous["pending"],
            str(previous),
        )
        crashlog.end_run()

        crashlog.install(
            enabled=False,
            log_path=crashlog.CRASH_LOG,
            version=desktop.APP_VERSION,
            config_path=app.CONFIG_PATH,
        )
        crashlog.close()


def test_window_placement() -> None:
    """The window opens centred, and never mostly off-screen."""
    import tempfile

    left, top, right, bottom = desktop.work_area()
    width, height = right - left, bottom - top

    with tempfile.TemporaryDirectory() as tmp:
        first = desktop.Placement(Path(tmp) / "ui-state.json").resolve()
        check("first launch gets a usable size", desktop.MIN_WIDTH <= first["width"] <= width, f"{first['width']}x{first['height']}")
        centered_x = abs((first["x"] + first["width"] / 2) - (left + width / 2))
        check("first launch is horizontally centred", centered_x <= 2, f"offset={centered_x:.0f}px")
        check("first launch is on screen", top <= first["y"] < bottom, str(first["y"]))

        state_path = Path(tmp) / "ui-state.json"
        state_path.write_text(json.dumps({"x": 99999, "y": -9999, "width": 20, "height": 20}), encoding="utf-8")
        clamped = desktop.Placement(state_path).resolve()
        check("tiny remembered sizes grow to the minimum",
              clamped["width"] >= desktop.MIN_WIDTH and clamped["height"] >= desktop.MIN_HEIGHT,
              f"{clamped['width']}x{clamped['height']}")
        check("an off-screen position is pulled back",
              left - 40 <= clamped["x"] <= right - 160 and top - 10 <= clamped["y"] <= bottom - 120,
              f"{clamped['x']},{clamped['y']}")
        check("a broken state file falls back to the centre",
              desktop.Placement(Path(tmp) / "missing.json").resolve()["width"] >= desktop.MIN_WIDTH)


def test_learning() -> None:
    """Corrections and vocabulary learned from edits in the window."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "learning.json"
        store = learning_mod.LearningStore(path)

        learned = store.learn_from_diff(
            "Voeg een donkere modus toe aan de inluchtpagina.",
            "Voeg een donkere modus toe aan de inlogpagina.",
        )
        rows = store.corrections()
        check("a single-word fix is learned", learned["corrections"] == 1, str(learned))
        check("the learned pair is exact",
              bool(rows) and rows[0]["heard"] == "inluchtpagina" and rows[0]["fixed"] == "inlogpagina",
              str(rows))
        check("the correction is applied to future text",
              store.apply_text("De inluchtpagina is traag.") == "De inlogpagina is traag.",
              store.apply_text("De inluchtpagina is traag."))
        check("the correction keeps an all-caps spelling",
              store.apply_text("INLUCHTPAGINA") == "inlogpagina".upper(),
              store.apply_text("INLUCHTPAGINA"))
        check("the correction does not change substrings",
              store.apply_text("inluchtpaginaat") == "inluchtpaginaat")
        check("a plain word fix does not pollute the prompt",
              "inlogpagina" not in store.prompt_extra(), store.prompt_extra())

        mixed = store.learn_from_diff("zet de web socket aan", "zet de WebSocket aan")
        words = [row["word"] for row in store.vocabulary()]
        check("a multi-word edit contributes vocabulary", "WebSocket" in words, str(words))
        check("a multi-word edit teaches no half phrase",
              all(row["heard"] not in ("web", "socket") for row in store.corrections()),
              str(store.corrections()))
        check("learned vocabulary reaches the prompt", "WebSocket" in store.prompt_extra(), store.prompt_extra())
        check("a multi-word edit reports its vocabulary", mixed["words"] >= 1, str(mixed))

        store.learn_from_diff("voeg het toe voor codebuff", "Voeg het toe voor Codebuff")
        words = [row["word"] for row in store.vocabulary()]
        check("a capitalisation fix teaches a proper noun", "Codebuff" in words, str(words))
        check("sentence-start capitalisation is not vocabulary", "Voeg" not in words, str(words))

        ignored = store.learn_from_diff("de pagina", "het pagina")
        check("stopword-only changes are ignored", ignored == {"corrections": 0, "words": 0}, str(ignored))

        check("a hand-typed word lands in the prompt",
              store.add_word("features") is True and "features" in store.prompt_extra(), store.prompt_extra())
        check("typing the same word again counts it",
              store.add_word("features") is True
              and next(row for row in store.vocabulary() if row["word"] == "features")["count"] == 2,
              str(store.vocabulary()[:2]))
        check("a hand-typed word keeps its hyphen and drops punctuation",
              store.add_word("plug-in,") is True
              and any(row["word"] == "plug-in" for row in store.vocabulary()),
              str([row["word"] for row in store.vocabulary()]))
        check("a word with a space is refused", store.add_word("twee woorden") is False)
        check("an empty word is refused", store.add_word("   ") is False)
        check("a refused word is never stored",
              all(row["word"] not in ("twee woorden", "") for row in store.vocabulary()))

        reloaded = learning_mod.LearningStore(path)
        check("learning survives a restart",
              reloaded.apply_text("inluchtpagina") == "inlogpagina" and "WebSocket" in reloaded.prompt_extra())
        check("hand-typed words survive a restart", "features" in reloaded.prompt_extra(), reloaded.prompt_extra())
        reloaded.forget_correction("inluchtpagina")
        check("forgetting a correction stops it applying", reloaded.apply_text("inluchtpagina") == "inluchtpagina")
        reloaded.forget_word("WebSocket")
        check("forgetting a word removes it from the prompt", "WebSocket" not in reloaded.prompt_extra())
        reloaded.clear()
        check("clearing forgets everything", reloaded.payload()["total"] == 0, str(reloaded.payload()))
        check("an empty store still applies text unchanged", reloaded.apply_text("niets te doen") == "niets te doen")


def test_version_numbers() -> None:
    """A spoken version keeps its components; decimals and part numbers do not."""

    def fixed(text: str) -> str:
        return textfix.fix_versions(text)

    check("a spoken version keeps its components",
          fixed("Dicteer versie 2.01") == "Dicteer versie 2.0.1", fixed("Dicteer versie 2.01"))
    check("punctuation around the number survives",
          fixed("versie 2.01, de nieuwe.") == "versie 2.0.1, de nieuwe.",
          fixed("versie 2.01, de nieuwe."))
    check("a three-part version is left alone",
          fixed("versie 2.0.1") == "versie 2.0.1", fixed("versie 2.0.1"))
    check("two-part versions stay two-part",
          fixed("versie 10.12 is uit") == "versie 10.12 is uit", fixed("versie 10.12 is uit"))
    check("a version with a single milestone is not invented",
          fixed("versie 2.0") == "versie 2.0", fixed("versie 2.0"))
    check("a decimal outside a version survives",
          fixed("dat kost 2.05 euro") == "dat kost 2.05 euro", fixed("dat kost 2.05 euro"))
    check("an IP address survives",
          fixed("het adres is 192.168.0.1") == "het adres is 192.168.0.1",
          fixed("het adres is 192.168.0.1"))
    check("a list of versions is fixed throughout",
          fixed("versies 2.01, 2.02 en 2.03") == "versies 2.0.1, 2.0.2 en 2.0.3",
          fixed("versies 2.01, 2.02 en 2.03"))
    check("a glued v keeps its prefix",
          fixed("v2.01 staat klaar") == "v2.0.1 staat klaar", fixed("v2.01 staat klaar"))
    check("English wording works too",
          fixed("version 2.010 fixed it") == "version 2.0.10 fixed it",
          fixed("version 2.010 fixed it"))
    check("the word after the number ends the run",
          fixed("versie 2.01 van Dicteer 2.05") == "versie 2.0.1 van Dicteer 2.05",
          fixed("versie 2.01 van Dicteer 2.05"))
    check("text without a dot is untouched",
          fixed("geen nummers hier") == "geen nummers hier", fixed("geen nummers hier"))


def test_licence_notices() -> None:
    """The installer carries the MIT text and the notices for what it bundles."""
    root = app.APP_DIR
    notices_path = root / "THIRD-PARTY-NOTICES.txt"
    notices = notices_path.read_text(encoding="utf-8") if notices_path.exists() else ""
    check("the third-party notices are in the project", bool(notices), str(notices_path))
    # The bundled FFmpeg is GPL because it enables these two encoders; a
    # notices file that forgets them is worse than none at all.
    for needle in ("FFmpeg", "libx264", "libx265", "GPL version 3", "LGPL version 3"):
        check(f"the notices name {needle}", needle in notices)

    licence_path = root / "LICENSE"
    licence = licence_path.read_text(encoding="utf-8") if licence_path.exists() else ""
    check("the project licence is MIT", licence.startswith("MIT License"))

    texts = {
        "GPL-3.0.txt": "GNU GENERAL PUBLIC LICENSE",
        "GPL-2.0.txt": "GNU GENERAL PUBLIC LICENSE",
        "LGPL-3.0.txt": "GNU LESSER GENERAL PUBLIC LICENSE",
        "Apache-2.0.txt": "Apache License",
    }
    for name, heading in texts.items():
        path = root / "licenses" / name
        body = path.read_text(encoding="utf-8") if path.exists() else ""
        check(f"the {name} text is included", heading in body, f"{len(body)} bytes")

    nsi_path = root / "installer" / "Dicteer.nsi"
    nsi = nsi_path.read_text(encoding="utf-8") if nsi_path.exists() else ""
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.txt"):
        check(f"the installer ships {name}", f'File "${{ROOT}}\\{name}"' in nsi)
        check(f"the uninstaller removes {name}", f'Delete "$INSTDIR\\{name}"' in nsi)
    for name in texts:
        check(f"the installer ships licenses\\{name}", f'File "${{ROOT}}\\licenses\\{name}"' in nsi)
    check("the uninstaller removes the licenses folder", 'RMDir /r "$INSTDIR\\licenses"' in nsi)


def test_cloud_credentials() -> None:
    """The key is encrypted, round-trips, and never appears in the file."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "credentials.json"
        store = cloud.CredentialStore(path)
        key = "gsk_" + "a1b2c3d4" * 4
        check("no key is reported before one is stored", not store.has("groq"))
        store.set(key)
        check("the key is reported after storing", store.has("groq"))
        check("the key decrypts for the same user", store.get("groq") == key)
        raw = path.read_text(encoding="utf-8")
        check("the plain key is not written to disk", key not in raw)
        check("the stored value is not plain base64", key not in cloud.base64.b64decode(json.loads(raw)["groq"]).decode("utf-8", "replace"))
        check("a removed key is gone", store.clear("groq") and not store.has("groq"))
        try:
            store.set("kort")
        except cloud.CredentialError:
            check("an obviously invalid key is refused", True)
        else:
            check("an obviously invalid key is refused", False, "no error raised")


def test_cloud_http() -> None:
    """The multipart request carries the key, model, language and audio."""
    import tempfile

    seen: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler name
            length = int(self.headers.get("Content-Length") or 0)
            seen["headers"] = dict(self.headers)
            seen["body"] = self.rfile.read(length)
            payload = json.dumps({"text": "  hallo   wereld "}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = cloud.CredentialStore(Path(tmp) / "credentials.json")
            key = "gsk_" + "test" * 8
            store.set(key)
            client = cloud.CloudTranscriber(
                store,
                url=f"http://127.0.0.1:{server.server_port}/openai/v1/audio/transcriptions",
                timeout=5,
            )
            text = client.transcribe(
                np.full(1600, 0.1, dtype=np.float32),
                language="nl",
                prompt="Freebuff, Codebuff",
            )
            body = seen.get("body", b"")
            headers = seen.get("headers", {})
            check("the online transcript is returned", text == "hallo wereld", repr(text))
            check("the request is authorized with the stored key",
                  headers.get("Authorization") == f"Bearer {key}")
            check("the request is multipart",
                  "multipart/form-data; boundary=" in headers.get("Content-Type", ""))
            check("the request names the Groq turbo model",
                  b'name="model"' in body and cloud.GROQ_MODEL.encode() in body)
            check("the language hint is sent", b'name="language"' in body and b"nl" in body)
            check("learned vocabulary is sent as the prompt",
                  b'name="prompt"' in body and b"Freebuff, Codebuff" in body)
            check("the audio is a 16 kHz mono WAV",
                  b"RIFF" in body and b"WAVE" in body)
            with wave.open(io.BytesIO(cloud.wav_bytes(np.zeros(1600, dtype=np.float32)))) as handle:
                check("the WAV header is 16 kHz mono 16-bit",
                      (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) == (16000, 1, 2))
    finally:
        server.shutdown()
        server.server_close()


def test_cloud_failure_message() -> None:
    import tempfile

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            # Drain the upload before answering: replying while the multipart
            # body is still arriving makes Windows reset the loopback
            # connection about one run in three, hiding the 401 from the
            # client and failing this check for no good reason.
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            payload = b'{"error": {"message": "Invalid API Key"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = cloud.CredentialStore(Path(tmp) / "credentials.json")
            store.set("gsk_" + "x" * 32)
            client = cloud.CloudTranscriber(store, url=f"http://127.0.0.1:{server.server_port}/", timeout=5)
            try:
                client.transcribe(np.zeros(1600, dtype=np.float32))
            except cloud.CloudError as exc:
                check("a rejected key gets a readable message", "ongeldig" in str(exc), str(exc))
            else:
                check("a rejected key gets a readable message", False, "no error raised")
    finally:
        server.shutdown()
        server.server_close()


def test_cloud_fallback() -> None:
    """A failing online pass never loses the dictation."""
    cfg = dict(app.DEFAULT_CONFIG)
    cfg.update({"online_backend": "groq", "beeps": False, "model": "small"})
    dicta = app.Dictator(cfg, argparse.Namespace(no_paste=True))
    audio = np.zeros(1600, dtype=np.float32)
    notices: list[str] = []
    dicta.on_notice = notices.append
    dicta._transcribe_local = lambda _audio: "lokale tekst"  # type: ignore[assignment]
    dicta.postprocess = lambda text: text.replace("tekst", "tekst!")  # type: ignore[assignment]

    class FakeCloud:
        model = "whisper-large-v3-turbo"

        def transcribe(self, _audio, **_kwargs):
            return "online  tekst"

    dicta.cloud = FakeCloud()
    check("the online backend is used when selected",
          dicta.transcribe_audio(audio) == "online tekst!", dicta.transcribe_audio(audio))
    check("the online model is remembered for history",
          dicta._last_model == "whisper-large-v3-turbo" and dicta._last_backend == "groq")

    class BrokenCloud:
        model = "whisper-large-v3-turbo"

        def transcribe(self, _audio, **_kwargs):
            raise cloud.CloudError("limiet bereikt")

    dicta.cloud = BrokenCloud()
    before = len(notices)
    check("a failed online pass falls back to local",
          dicta.transcribe_audio(audio) == "lokale tekst", dicta.transcribe_audio(audio))
    check("the fallback is reported once", len(notices) == before + 1, str(notices))
    check("the local model is recorded after a fallback",
          dicta._last_backend == "local" and dicta._last_model == "small")
    count = len(notices)
    dicta.transcribe_audio(audio)
    check("the same failure is not reported twice", len(notices) == count, str(notices))

    cfg["online_backend"] = "local"
    dicta.cloud = FakeCloud()
    check("the local backend never calls the cloud",
          dicta.transcribe_audio(audio) == "lokale tekst")


def test_gemini_http() -> None:
    """One generateContent call carries the key, the instruction and the clip."""
    import tempfile

    seen: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler name
            length = int(self.headers.get("Content-Length") or 0)
            seen["path"] = self.path
            seen["headers"] = dict(self.headers)
            seen["body"] = self.rfile.read(length)
            payload = json.dumps(
                {
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "  hallo "}, {"text": "wereld "}]},
                            "finishReason": "STOP",
                        }
                    ]
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = cloud.CredentialStore(Path(tmp) / "credentials.json")
            key = "AIza" + "test" * 9
            store.set(key, "gemini")
            client = cloud.CloudTranscriber(
                store,
                backend="gemini",
                url=f"http://127.0.0.1:{server.server_port}/v1beta/models/"
                    f"{cloud.GEMINI_MODEL}:generateContent",
                timeout=5,
            )
            text = client.transcribe(
                np.full(1600, 0.1, dtype=np.float32),
                language="nl",
                prompt="Freebuff, Codebuff",
            )
            body = json.loads(seen.get("body") or b"{}")
            headers = seen.get("headers", {})
            parts = ((body.get("contents") or [{}])[0].get("parts") or [])
            audio = (parts[1] if len(parts) > 1 else {}).get("inline_data") or {}
            instruction = parts[0].get("text") if parts else ""

            check("the Gemini transcript joins the answer parts", text == "hallo wereld", repr(text))
            # urllib capitalises header names, so compare them without case.
            sent = {name.lower(): value for name, value in headers.items()}
            check("the Gemini key travels in its own header",
                  sent.get("x-goog-api-key") == key, str(sent.get("x-goog-api-key")))
            check("the request asks Gemini for JSON",
                  headers.get("Content-Type") == "application/json")
            check("the request names the Gemini model in its path",
                  cloud.GEMINI_MODEL in seen.get("path", "") and seen.get("path", "").endswith(":generateContent"),
                  seen.get("path", ""))
            check("the instruction names the spoken language", "Dutch" in instruction, instruction)
            check("the instruction carries the learned vocabulary",
                  "Freebuff, Codebuff" in instruction, instruction)
            check("the instruction demands only the transcript",
                  "Return only the transcript" in instruction, instruction)
            check("the clip is sent inline as 16 kHz WAV",
                  audio.get("mime_type") == "audio/wav", str(audio.get("mime_type")))
            decoded = cloud.base64.b64decode(str(audio.get("data") or ""))
            check("the inline clip is the recorded audio, not a placeholder",
                  decoded[:4] == b"RIFF" and b"WAVE" in decoded[:16], repr(decoded[:16]))
            check("the model is asked for a deterministic answer",
                  (body.get("generationConfig") or {}).get("temperature") == 0)

            other = cloud.CloudTranscriber(store, backend="groq")
            try:
                other.transcribe(np.zeros(1600, dtype=np.float32))
            except cloud.CloudError as exc:
                check("a Gemini key does not authorize Groq", "geen sleutel" in str(exc).lower(), str(exc))
            else:
                check("a Gemini key does not authorize Groq", False, "no error raised")
    finally:
        server.shutdown()
        server.server_close()


def test_cloud_status() -> None:
    """The window learns which providers have a key, never the key itself."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = cloud.CredentialStore(Path(tmp) / "credentials.json")
        gemini_key = "AIza" + "y" * 35
        store.set(gemini_key, "gemini")
        local = cloud.status(store, "local")
        gemini = cloud.status(store, "gemini")
        groq = cloud.status(store, "groq")

        check("the status lists both online providers",
              set(gemini["providers"]) == {"groq", "gemini"}, str(sorted(gemini["providers"])))
        check("settings and the providers offer the same backends",
              set(settings_mod.ONLINE_BACKENDS) == {"local"} | set(cloud.PROVIDERS),
              str(settings_mod.ONLINE_BACKENDS))
        check("the chosen backend is reported", gemini["backend"] == "gemini" and gemini["has_key"])
        check("keys are tracked per provider",
              gemini["keys"] == {"groq": False, "gemini": True}, str(gemini["keys"]))
        check("the other provider still shows no key", not groq["has_key"])
        check("local needs no key",
              local["backend"] == "local" and not local["has_key"] and local["model"] == "")
        check("the card is told the Gemini model and key page",
              gemini["model"] == cloud.GEMINI_MODEL
              and gemini["keys_url"].startswith("https://")
              and gemini["key_hint"] == "AIza...")
        slower = cloud.status(store, "gemini", "gemini-3.6-flash")
        check("the status reports the chosen Gemini model",
              slower["gemini_model"] == "gemini-3.6-flash"
              and slower["model"] == "gemini-3.6-flash"
              and slower["providers"]["gemini"]["model"] == "gemini-3.6-flash",
              str(slower["model"]))
        check("the window is offered the models and their notes",
              [entry["value"] for entry in gemini["gemini_models"]]
              == [entry["value"] for entry in cloud.GEMINI_MODELS]
              and all(entry["note"] for entry in gemini["gemini_models"]))
        check("an unknown Gemini model falls back to the fast one",
              cloud.status(store, "gemini", "gemini-9-ultra")["gemini_model"] == cloud.GEMINI_MODEL)
        check("no key value reaches the window",
              gemini_key not in json.dumps(gemini))
        check("an unknown backend reports local",
              cloud.status(store, "somewhere")["backend"] == "local")


def test_updates() -> None:
    """The release check compares versions and never interrupts dictation."""
    import updates

    check("version keys pad the missing parts", updates.version_key("v1.7") == (1, 7, 0))
    check("version keys ignore a pre-release suffix",
          updates.version_key("1.7.0-beta.1") == (1, 7, 0))
    try:
        updates.version_key("latest")
    except ValueError:
        check("a tag that is not a version is rejected", True)
    else:
        check("a tag that is not a version is rejected", False, "no ValueError")

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()
            return False

    seen: dict = {}

    def opener(payload, error=None):
        def call(request, timeout=None):
            if error is not None:
                raise error
            seen["url"] = request.full_url
            seen["headers"] = {key.lower(): value for key, value in request.header_items()}
            return Response(json.dumps(payload).encode("utf-8"))

        return call

    release = {
        "tag_name": "v1.7.0",
        "name": "Dicteer v1.7.0",
        "html_url": "https://github.com/DrIamlegion/Dicteer/releases/tag/v1.7.0",
    }
    result = updates.check("1.6", opener=opener(release))
    check("a newer release is offered", result.get("available") is True, repr(result))
    check("the new version is named without the v", result.get("latest") == "1.7.0", str(result.get("latest")))
    check("the release page becomes the download link",
          str(result.get("url")).endswith("/tag/v1.7.0"), str(result.get("url")))
    check("the request identifies itself to GitHub",
          seen["headers"].get("user-agent") == updates.USER_AGENT, str(seen.get("headers")))
    check("the request asks for the GitHub JSON API",
          seen["headers"].get("accept") == "application/vnd.github+json")
    check("the check reads the latest release endpoint", seen["url"] == updates.API_URL, seen["url"])

    same = dict(release, tag_name="v1.6")
    check("the installed version is not offered again",
          updates.check("1.6", opener=opener(same)).get("available") is False)
    older = dict(release, tag_name="v1.5.2")
    check("an older tag is not offered", updates.check("1.6", opener=opener(older)).get("available") is False)
    same_version = dict(release, tag_name="v1.6.0")
    check("1.6 and v1.6.0 are the same release",
          updates.check("1.6", opener=opener(same_version)).get("available") is False)

    foreign = dict(release, html_url="https://example.com/freebuff-setup.exe")
    check("a foreign download link is replaced by our release page",
          updates.check("1.6", opener=opener(foreign))["url"].startswith(
              f"https://github.com/{updates.REPO}/releases/"))
    check("our release pages are accepted",
          updates.is_release_url("https://github.com/DrIamlegion/Dicteer/releases/tag/v1.6"))
    check("other pages are refused", not updates.is_release_url("https://example.com/releases"))

    offline = updates.check("1.6", opener=opener({}, error=OSError("no network")))
    check("offline returns a message instead of raising",
          offline.get("ok") is False and bool(offline.get("error")), repr(offline))
    check("a release without a tag is refused",
          updates.check("1.6", opener=opener({"name": "no tag"})).get("ok") is False)


def test_legacy_data_migration() -> None:
    """A 1.x data folder moves into the new name once, without overwriting."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "AppData" / "Roaming"
        old_dir = base / app.LEGACY_PRODUCT_NAME
        new_dir = base / "Dicteer"
        (old_dir / "models").mkdir(parents=True)
        (old_dir / "config.json").write_text('{"language": "nl"}', encoding="utf-8")
        (old_dir / "history.json").write_text('{"items": []}', encoding="utf-8")
        (old_dir / "models" / "model.bin").write_bytes(b"weights")

        saved = (app.DATA_DIR, app.FROZEN, os.environ.get("APPDATA"))
        app.DATA_DIR = new_dir
        app.FROZEN = True
        os.environ["APPDATA"] = str(base)
        try:
            moved = app.migrate_legacy_data()
            check("the old data folder is reported as moved", moved == old_dir, str(moved))
            check("settings land in the new folder",
                  (new_dir / "config.json").read_text(encoding="utf-8") == '{"language": "nl"}')
            check("history lands in the new folder", (new_dir / "history.json").exists())
            check("models move with the rest",
                  (new_dir / "models" / "model.bin").read_bytes() == b"weights")
            check("the old folder is gone once emptied", not old_dir.exists())
            check("a second start has nothing left to do", app.migrate_legacy_data() is None)

            # A folder that already has settings wins over the old one.
            old_dir.mkdir(parents=True)
            (old_dir / "config.json").write_text('{"language": "en"}', encoding="utf-8")
            check("an existing install is not overwritten", app.migrate_legacy_data() is None)
            check("the current config is untouched",
                  (new_dir / "config.json").read_text(encoding="utf-8") == '{"language": "nl"}')

            # The app opens its log before this runs, so the target folder is
            # never empty on a real first start; that must not look like data.
            import shutil as _shutil
            _shutil.rmtree(new_dir)
            (new_dir / "models").mkdir(parents=True)
            (new_dir / "dictate.log").write_text("starting\n", encoding="utf-8")
            (new_dir / "dictate.lock").write_bytes(b"")
            check("a log and a lock are not data", app._new_folder_in_use() is False)
            moved = app.migrate_legacy_data()
            check("a folder with only its own log still migrates", moved == old_dir, str(moved))
            check("the 1.x settings arrive next to the fresh log",
                  (new_dir / "config.json").read_text(encoding="utf-8") == '{"language": "en"}')
            check("the fresh log is left alone",
                  (new_dir / "dictate.log").read_text(encoding="utf-8") == "starting\n")

            # A folder that already holds history is a real 2.x install.
            old_dir.mkdir(parents=True)
            (old_dir / "config.json").write_text('{"language": "en"}', encoding="utf-8")
            (new_dir / "history.json").write_text('{"items": []}', encoding="utf-8")
            check("a used 2.x folder is not taken over", app.migrate_legacy_data() is None)
        finally:
            app.DATA_DIR, app.FROZEN = saved[0], saved[1]
            if saved[2] is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = saved[2]


def test_history_stats() -> None:
    now = time.time()
    items = [
        {"text": "een twee drie", "duration": 2.0, "ts": now},
        {"text": "vier vijf", "duration": 1.5, "ts": now - 86400},
    ]
    stats = history_mod.stats(items, now=now)
    check("history stats count today only", stats["today_count"] == 1 and stats["today_words"] == 3, str(stats))
    check("history stats keep totals", stats["total_count"] == 2 and stats["total_words"] == 5, str(stats))
    check("history stats sum speech time", abs(stats["total_seconds"] - 3.5) < 0.01, str(stats["total_seconds"]))
    check("empty history reports zeroes", history_mod.stats([], now=now)["today_count"] == 0)


def test_corrupt_learning() -> None:
    """A hand-edited learning.json must not block the app at startup."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "learning.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "corrections": {
                        "inluchtpagina": {"fixed": "inlogpagina", "count": "many", "ts": "gisteren"},
                        "": {"fixed": "leeg", "count": 1, "ts": 0},
                        "zonderfix": {"count": 4, "ts": 0},
                    },
                    "vocabulary": {
                        "WebSocket": {"count": "2", "ts": "nooit"},
                        "": {"count": 1, "ts": 0},
                    },
                }
            ),
            encoding="utf-8",
        )
        try:
            store = learning_mod.LearningStore(path)
        except Exception as exc:
            check("corrupt learning.json does not block startup", False, f"{type(exc).__name__}: {exc}")
            return

        rows = store.corrections()
        check(
            "a corrupt count falls back instead of raising",
            len(rows) == 1 and rows[0]["heard"] == "inluchtpagina" and rows[0]["count"] == 1,
            str(rows),
        )
        check("a corrupt timestamp is accepted", store.payload()["total"] == 2, str(store.payload()))
        check(
            "usable corrections still apply",
            store.apply_text("De inluchtpagina is traag.") == "De inlogpagina is traag.",
            store.apply_text("De inluchtpagina is traag."),
        )
        check("usable vocabulary still reaches the prompt", "WebSocket" in store.prompt_extra(), store.prompt_extra())

        path.write_text("{ this is not json", encoding="utf-8")
        try:
            broken = learning_mod.LearningStore(path)
            check("unparseable learning.json starts fresh", broken.payload()["total"] == 0, str(broken.payload()))
            check(
                "unparseable learning.json is reported",
                bool(broken.read_error) and "correcties" in broken.read_error.lower(),
                str(broken.read_error),
            )
        except Exception as exc:
            check("unparseable learning.json starts fresh", False, f"{type(exc).__name__}: {exc}")


def test_corrupt_history() -> None:
    """Malformed history values must not break the dashboard or startup."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "history.json"
        now = time.time()
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {"id": "a", "text": "goed", "duration": 2.0, "ts": now},
                        {"id": "b", "text": "kapot", "duration": "geen getal", "ts": "geen tijd"},
                        {"id": "c", "text": "negatief", "duration": -3, "ts": now},
                        {"id": "d", "text": None, "duration": 1, "ts": now},
                    ],
                }
            ),
            encoding="utf-8",
        )
        try:
            store = history_mod.HistoryStore(path)
            items = store.items()
            stats = history_mod.stats(items, now=now)
        except Exception as exc:
            check("corrupt history.json does not block startup", False, f"{type(exc).__name__}: {exc}")
            return

        check("empty history rows are dropped", len(items) == 3, str(items))
        check(
            "a corrupt duration becomes zero",
            items[1]["duration"] == 0.0 and items[2]["duration"] == 0.0,
            str([item["duration"] for item in items]),
        )
        check(
            "history stats survive corrupt values",
            stats["total_count"] == 3
            and stats["total_words"] == 3
            and abs(stats["total_seconds"] - 2.0) < 0.01
            and stats["today_count"] == 2,
            str(stats),
        )

        path.write_text("{ this is not json", encoding="utf-8")
        try:
            broken = history_mod.HistoryStore(path)
            check("unparseable history.json starts empty", broken.items() == [], str(broken.items()))
            check(
                "unparseable history.json is reported",
                bool(broken.read_error) and "geschiedenis" in broken.read_error.lower(),
                str(broken.read_error),
            )
        except Exception as exc:
            check("unparseable history.json starts empty", False, f"{type(exc).__name__}: {exc}")


def test_storage_write_errors() -> None:
    """A failed JSON write is reported once, then again after a recovery."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        blocked = Path(tmp) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")

        history_notices: list[str] = []
        history_store = history_mod.HistoryStore(
            blocked / "history.json", on_error=history_notices.append
        )
        history_store.add("eerste")
        history_store.add("tweede")
        check(
            "a history write failure is reported once",
            len(history_notices) == 1 and "geschiedenis" in history_notices[0].lower(),
            str(history_notices),
        )

        history_store.path = Path(tmp) / "history.json"
        history_store.add("derde")
        check("history writes recover after the map is usable", history_store.path.is_file())
        history_store.path = blocked / "history-again.json"
        history_store.add("vierde")
        check(
            "a new history failure is reported again",
            len(history_notices) == 2,
            str(history_notices),
        )

        learning_notices: list[str] = []
        learning_store = learning_mod.LearningStore(
            blocked / "learning.json", on_error=learning_notices.append
        )
        learning_store.learn_from_diff("De inluchtpagina is traag.", "De inlogpagina is traag.")
        learning_store.learn_from_diff("De inluchtpagina is weer traag.", "De inlogpagina is weer traag.")
        check(
            "a learning write failure is reported once",
            len(learning_notices) == 1 and "correcties" in learning_notices[0].lower(),
            str(learning_notices),
        )

        learning_store.path = Path(tmp) / "learning.json"
        learning_store.clear()
        check("learning writes recover after the map is usable", learning_store.path.is_file())
        learning_store.path = blocked / "learning-again.json"
        learning_store.learn_from_diff("De inluchtpagina is traag.", "De inlogpagina is traag.")
        check(
            "a new learning failure is reported again",
            len(learning_notices) == 2,
            str(learning_notices),
        )


def test_storage_read_warnings() -> None:
    """Startup read failures are queued until the window can show them."""
    import tempfile

    tray = load_tray()
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "history.json").write_text("{ broken", encoding="utf-8")
        (Path(tmp) / "learning.json").write_text("[1, 2]", encoding="utf-8")
        cfg = dict(app.DEFAULT_CONFIG, overlay=False, beeps=False)
        shell = tray.Shell(cfg, Path(tmp) / "config.json")
        check(
            "a broken history file is recorded at startup",
            bool(shell.history.read_error) and "geschiedenis" in shell.history.read_error.lower(),
            str(shell.history.read_error),
        )
        check(
            "a broken learning file is recorded at startup",
            bool(shell.learning.read_error) and "correcties" in shell.learning.read_error.lower(),
            str(shell.learning.read_error),
        )

        warnings = shell.boot_payload().get("storage_warnings", [])
        check(
            "startup storage warnings are in the boot payload",
            len(warnings) == 2
            and "geschiedenis" in warnings[0].lower()
            and "correcties" in warnings[1].lower(),
            str(warnings),
        )
        result = tray.desktop.Api(shell).dismiss_storage_warnings()
        check(
            "the storage banner can be dismissed from the window",
            result.get("ok") is True and shell.boot_payload()["storage_warnings"] == [],
            str(shell.boot_payload()["storage_warnings"]),
        )


def test_storage_backup_of_unreadable_files() -> None:
    """A file we could not read survives the first write that replaces it."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        broken_history = "{ this is not json"
        history_path = folder / "history.json"
        history_path.write_text(broken_history, encoding="utf-8")
        notices: list[str] = []
        store = history_mod.HistoryStore(history_path, on_error=notices.append)
        check("a damaged history file is flagged for backup", store._backup_pending is True)

        store.add("eerste")
        backups = sorted(folder.glob("history.json.*.bak"))
        check(
            "the unreadable history file is backed up",
            len(backups) == 1,
            str([path.name for path in backups]),
        )
        check(
            "the history backup holds the original bytes",
            bool(backups) and backups[0].read_text(encoding="utf-8") == broken_history,
            backups[0].read_text(encoding="utf-8") if backups else "no backup",
        )
        check(
            "the history file is rewritten after the backup",
            json.loads(history_path.read_text(encoding="utf-8"))["items"][0]["text"] == "eerste",
        )
        check("a successful backup raises no write warning", notices == [], str(notices))

        store.add("tweede")
        check(
            "history is backed up once per damaged file",
            len(list(folder.glob("history.json.*.bak"))) == 1,
            str([path.name for path in folder.glob("history.json.*.bak")]),
        )

        quiet = history_mod.HistoryStore(folder / "healthy.json")
        quiet.add("gezond")
        check(
            "a readable history file is not backed up",
            list(folder.glob("healthy.json.*.bak")) == [],
            str([path.name for path in folder.glob("healthy.json.*.bak")]),
        )

        broken_learning = "[1, 2]"
        learning_path = folder / "learning.json"
        learning_path.write_text(broken_learning, encoding="utf-8")
        learn = learning_mod.LearningStore(learning_path)
        learn.learn_from_diff("De inluchtpagina is traag.", "De inlogpagina is traag.")
        learning_backups = sorted(folder.glob("learning.json.*.bak"))
        check(
            "the unreadable learning file is backed up",
            len(learning_backups) == 1,
            str([path.name for path in learning_backups]),
        )
        check(
            "the learning backup holds the original bytes",
            bool(learning_backups) and learning_backups[0].read_text(encoding="utf-8") == broken_learning,
            learning_backups[0].read_text(encoding="utf-8") if learning_backups else "no backup",
        )
        check(
            "the learning file is rewritten after the backup",
            "inluchtpagina" in learning_path.read_text(encoding="utf-8"),
        )

        # A backup that cannot be made must not cost the user their data.
        guarded_path = folder / "guarded.json"
        guarded_path.write_text(broken_history, encoding="utf-8")
        guarded_notices: list[str] = []
        guarded = history_mod.HistoryStore(guarded_path, on_error=guarded_notices.append)
        real_copy = history_mod.backup.copy_aside

        def no_space(_path):
            raise OSError("geen schijfruimte")

        history_mod.backup.copy_aside = no_space
        try:
            guarded.add("mag niet verdwijnen")
        finally:
            history_mod.backup.copy_aside = real_copy
        check(
            "without a backup the damaged file is left untouched",
            guarded_path.read_text(encoding="utf-8") == broken_history,
            guarded_path.read_text(encoding="utf-8"),
        )
        check(
            "a failed backup is reported as a storage problem",
            len(guarded_notices) == 1 and "geschiedenis" in guarded_notices[0].lower(),
            str(guarded_notices),
        )


def test_cancel_transcription() -> None:
    """Cancel during the final pass must suppress the paste, not just hide it."""
    cfg = dict(app.DEFAULT_CONFIG, beeps=False, min_seconds=0.1)
    dicta = app.Dictator(cfg, argparse.Namespace(no_paste=True))
    started = threading.Event()
    release = threading.Event()
    delivered: list[str] = []

    def slow_transcribe(_audio):
        started.set()
        release.wait(3.0)
        return "deze tekst mag niet geplakt worden"

    dicta.transcribe_audio = slow_transcribe  # type: ignore[assignment]
    dicta.deliver = delivered.append  # type: ignore[assignment]
    dicta._set_state("transcribing")
    token = threading.Event()
    dicta._cancel_token = token
    worker = threading.Thread(
        target=dicta._transcribe_and_deliver,
        args=(np.zeros(16000, dtype=np.float32), 16000, 1.0, token),
        daemon=True,
    )
    worker.start()
    check("the transcription worker starts", started.wait(2.0))
    dicta.cancel()
    release.set()
    worker.join(3.0)

    check("cancel during transcription returns to idle", dicta._state == "idle", dicta._state)
    check("the cancelled transcription is not pasted", delivered == [], str(delivered))
    check("the transcription worker stopped", not worker.is_alive())


def test_autostart() -> None:
    # The installed build has no interpreter to point at: the shortcut must
    # start the exe itself, with --hidden.
    real_frozen = getattr(sys, "frozen", False)
    real_executable = sys.executable
    try:
        sys.frozen = True
        sys.executable = r"C:\Program Files\Dicteer\Dicteer.exe"
        check(
            "an installed build starts its own exe at login",
            autostart.target() == Path(sys.executable),
            str(autostart.target()),
        )
        check(
            "an installed build passes --hidden",
            autostart.arguments() == "--hidden",
            autostart.arguments(),
        )
        check(
            "the login shortcut starts in the install folder",
            autostart.working_dir() == Path(sys.executable).parent,
            str(autostart.working_dir()),
        )
    finally:
        if real_frozen:
            sys.frozen = real_frozen
        else:
            delattr(sys, "frozen")
        sys.executable = real_executable

    was_enabled = autostart.is_enabled()
    try:
        link = autostart.enable()
        check("autostart creates the Startup shortcut", link.exists(), str(link))
        raw = link.read_bytes()
        encoded = "tray.pyw".encode("utf-16-le")
        check(
            "shortcut launches tray.pyw",
            b"tray.pyw" in raw or encoded in raw,
        )
        check(
            "shortcut runs without a console (pythonw)",
            b"pythonw.exe" in raw or "pythonw.exe".encode("utf-16-le") in raw,
        )
        autostart.disable()
        check("autostart removes the Startup shortcut", not link.exists())
        reported = autostart.toggle()
        check("toggle reports the new state", reported is True, str(reported))
        check("toggle back off", autostart.toggle() is False and not link.exists())
    except Exception as exc:
        check("autostart round-trip", False, f"{type(exc).__name__}: {exc}")
    finally:
        if was_enabled and not autostart.is_enabled():
            autostart.enable()
        elif not was_enabled:
            autostart.disable()
    check(
        "login state is left exactly as it was",
        autostart.is_enabled() == was_enabled,
        f"was={was_enabled} now={autostart.is_enabled()}",
    )


def main() -> int:
    app.setup_logging(False)
    print(f"python {sys.version.split()[0]}  model dir {app.MODEL_DIR}")
    test_config()
    test_resample()
    test_level_dispatch()
    test_clipboard()
    test_state_machine()
    test_start_failure_cleanup()
    test_start_recovery()
    test_hotkey_validation()
    test_listen_control()
    test_merge_overlap()
    test_stream_session()
    test_stream_with_model()
    test_dutch()
    test_noise_guard()
    test_hold_to_talk()
    test_overlay_states()
    test_shell_ui()
    test_settings_validation()
    test_crashlog()
    test_crash_notice()
    test_window_placement()
    test_learning()
    test_version_numbers()
    test_licence_notices()
    test_cloud_credentials()
    test_cloud_http()
    test_gemini_http()
    test_cloud_failure_message()
    test_cloud_fallback()
    test_cloud_status()
    test_updates()
    test_legacy_data_migration()
    test_history_stats()
    test_corrupt_learning()
    test_corrupt_history()
    test_storage_write_errors()
    test_storage_read_warnings()
    test_storage_backup_of_unreadable_files()
    test_cancel_transcription()
    test_autostart()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all self-tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
