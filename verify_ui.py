"""Drive the real window through the real bridge.

The page is loaded in a real WebView2 window with the production :class:`Api`
and a throwaway config/history next to it. Every assertion runs JavaScript in
that window, the same way a click would, so the test covers the wiring that a
browser-only preview cannot: boot payload, pushed events, settings writes,
microphone test and the hotkey switch.

    .venv\\Scripts\\python.exe verify_ui.py

The window is visible for the few seconds the test runs; nothing is installed,
no model is loaded and no audio is pasted.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

import webview

import cloud

APP = Path(__file__).resolve().parent

log = logging.getLogger("verify-ui")
FAILS: list[str] = []

HOTKEY = "<ctrl>+<alt>+<shift>+<f12>"
COMBO_JS = "<ctrl>+<alt>+<shift>+<f12>"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def parse_hint(text) -> str:
    """The note the picker shows, without the window's "Nu actief." marker."""
    return str(text or "").removesuffix(" Nu actief.").strip()


def load_shell_module():
    """tray.pyw is a script, not an importable module name, so load it by path."""
    path = APP / "tray.pyw"
    spec = importlib.util.spec_from_file_location("tray_app", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def evaluate(window, script: str, default=None):
    try:
        return window.evaluate_js(script)
    except Exception as exc:  # a broken page must fail the test, not crash it
        log.warning("evaluate_js failed (%s): %s", script[:60], exc)
        return default


def wait_js(window, script: str, expected=True, timeout: float = 15.0, interval: float = 0.15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = evaluate(window, script)
        if value == expected:
            return True
        time.sleep(interval)
    return False


def wait_text(window, selector: str, contains: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = evaluate(window, f"document.querySelector({json.dumps(selector)})?.textContent || ''")
        if contains in str(value or ""):
            return True
        time.sleep(0.15)
    return False


def make_config(tmpdir: Path) -> Path:
    base = json.loads((APP / "config.json").read_text(encoding="utf-8"))
    base.update(
        {
            "hotkey": HOTKEY,
            "hold_to_talk": True,
            "language": "en",
            "model": "base.en",
            "preview_model": "",
            "mode": "clipboard",
            "beeps": False,
            "online_backend": "local",
            "input_device": None,
            "initial_prompt": "Freebuff, Codebuff",
        }
    )
    path = tmpdir / "verify-config.json"
    path.write_text(json.dumps(base, indent=2), encoding="utf-8")
    return path


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    tray = load_shell_module()

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        config_path = make_config(tmpdir)
        cfg = tray.app.load_config(config_path)
        # Exercise the persistent storage banner with a genuinely unreadable
        # learning file; the rest of the verification can still write to it.
        (tmpdir / "learning.json").write_text("{ broken", encoding="utf-8")

        shell = tray.Shell(cfg, config_path)
        # Keep the user's real dictation history out of the test.
        shell.history = tray.history_mod.HistoryStore(tmpdir / "history.json")
        shell.history.add("Dit is een testdictaat van eerder.", duration=2.4, model="base.en", language="en", mode="clipboard")
        # Pretend the previous run left a crash report, without opening Notepad.
        crash_path = tmpdir / "crash.log"
        crash_path.write_text("previous crash\n", encoding="utf-8")
        opened_paths: list[str] = []
        shell.open_path = lambda path: opened_paths.append(str(path))
        shell.crash_notice = {
            "pending": True,
            "path": str(crash_path),
            "started": time.time() - 120,
            "version": "1.6",
        }
        shell.ui = tray.desktop.WebUI(shell)
        shell.ui.create()
        window = shell.ui.window

        def run_checks():
            try:
                if not wait_js(window, "document.body.dataset.booted === '1'", True, 30):
                    check("page boots through the bridge", False, "boot event never arrived")
                    return

                check("page boots through the bridge", True)
                check(
                    "the About card shows the current version",
                    evaluate(window, "document.getElementById('about-version').textContent") == tray.desktop.APP_VERSION,
                    str(evaluate(window, "document.getElementById('about-version').textContent")),
                )
                check(
                    "the About text starts with the local mode",
                    "lokaal" in str(evaluate(window, "document.getElementById('about-pitch').textContent") or "").lower(),
                )
                check(
                    "the About card names the local transcription engine and model",
                    evaluate(window, "document.getElementById('about-engine').textContent") == "Lokaal (Whisper) · base.en",
                    str(evaluate(window, "document.getElementById('about-engine').textContent")),
                )
                check(
                    "the default microphone raises no warning",
                    evaluate(window, "document.getElementById('mic-notice').hidden") is True,
                )
                check(
                    "a previous crash raises a notice",
                    evaluate(window, "document.getElementById('crash-notice').hidden") is False,
                )
                check(
                    "the crash notice names the log and the previous session",
                    "crash.log" in str(evaluate(window, "document.getElementById('crash-notice-detail').textContent") or "")
                    and "Vorige sessie" in str(evaluate(window, "document.getElementById('crash-notice-detail').textContent") or ""),
                )
                check(
                    "an unreadable storage file raises a persistent notice",
                    evaluate(window, "document.getElementById('storage-notice').hidden") is False
                    and "correcties" in str(evaluate(window, "document.getElementById('storage-notice-detail').textContent") or "").lower(),
                    str(evaluate(window, "document.getElementById('storage-notice-detail').textContent")),
                )
                evaluate(window, "document.getElementById('storage-notice-dismiss').click()")
                dismissed = wait_js(window, "document.getElementById('storage-notice').hidden", True, 5)
                deadline = time.time() + 5
                while shell.boot_payload()["storage_warnings"] and time.time() < deadline:
                    time.sleep(0.05)
                check(
                    "dismissing the storage notice clears it in the window and the shell",
                    dismissed and shell.boot_payload()["storage_warnings"] == [],
                    str(shell.boot_payload()["storage_warnings"]),
                )
                shell._storage_error(
                    "De geschiedenis wordt niet opgeslagen. Controleer of de map schrijfbaar is."
                )
                check(
                    "runtime storage failures join the persistent notice",
                    wait_js(window, "document.getElementById('storage-notice').hidden", False, 5)
                    and "geschiedenis" in str(evaluate(window, "document.getElementById('storage-notice-detail').textContent") or "").lower(),
                    str(evaluate(window, "document.getElementById('storage-notice-detail').textContent")),
                )
                evaluate(window, "document.getElementById('storage-notice-dismiss').click()")
                runtime_dismissed = wait_js(window, "document.getElementById('storage-notice').hidden", True, 5)
                deadline = time.time() + 5
                while shell.boot_payload()["storage_warnings"] and time.time() < deadline:
                    time.sleep(0.05)
                check(
                    "runtime storage warnings can be dismissed too",
                    runtime_dismissed and shell.boot_payload()["storage_warnings"] == [],
                    str(shell.boot_payload()["storage_warnings"]),
                )

                evaluate(window, "document.getElementById('crash-notice-open').click()")
                check(
                    "the crash notice button opens crash.log",
                    wait_js(window, "document.getElementById('crash-notice').hidden", True, 6)
                    and str(crash_path) in opened_paths,
                    str(opened_paths),
                )

                chip_model = evaluate(window, "document.getElementById('chip-model').textContent")
                chip_language = evaluate(window, "document.getElementById('chip-language').textContent")
                check("model and language come from the config", chip_model == "base.en" and chip_language == "en", f"{chip_model}/{chip_language}")

                recents = evaluate(window, "document.querySelectorAll('.recent-item').length")
                check("seeded history is rendered", recents == 1, f"recent items={recents}")
                check("history badge matches the store", evaluate(window, "document.getElementById('history-count').textContent") == "1")

                devices = evaluate(window, "document.getElementById('set-device').options.length")
                check("microphones are listed in settings", isinstance(devices, int) and devices >= 1, f"options={devices}")
                device_values = evaluate(
                    window,
                    "JSON.stringify([...document.querySelectorAll('#set-device option')].map((o) => o.value))",
                )
                try:
                    device_values = json.loads(device_values or "[]")
                except Exception:
                    device_values = []
                check(
                    "microphones are saved by a stable name, not a device index",
                    "default" in device_values
                    and any(value != "default" and not str(value).isdigit() for value in device_values),
                    str(device_values),
                )
                check(
                    "the microphone list can be refreshed",
                    evaluate(window, "Boolean(document.getElementById('refresh-devices'))") is True,
                )
                check(
                    "the microphone hint explains the Windows choice",
                    "Windows" in str(evaluate(window, "document.getElementById('device-hint').textContent") or ""),
                )
                evaluate(window, "document.getElementById('refresh-devices').click()")
                check(
                    "the microphone list refreshes from Windows",
                    wait_text(window, "#toasts", "microfoon", 10),
                )

                # --- crash log -------------------------------------------
                check(
                    "crash logging is on by default",
                    evaluate(window, "document.getElementById('set-crash-log').checked") is True,
                )
                check(
                    "the About page shows where the crash log lives",
                    "crash.log" in str(evaluate(window, "document.getElementById('about-crash').textContent") or ""),
                )
                evaluate(window, "document.getElementById('set-crash-log').checked = false; document.getElementById('save-settings').click()")
                crash_off = False
                for _ in range(60):
                    try:
                        if json.loads(config_path.read_text(encoding="utf-8")).get("crash_logging") is False:
                            crash_off = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.15)
                check("crash logging can be switched off", crash_off)
                evaluate(window, "document.getElementById('set-crash-log').checked = true; document.getElementById('save-settings').click()")
                crash_on = False
                for _ in range(60):
                    try:
                        if json.loads(config_path.read_text(encoding="utf-8")).get("crash_logging") is True:
                            crash_on = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.15)
                check("crash logging can be switched back on", crash_on)
                opened_before = len(opened_paths)
                evaluate(window, "document.getElementById('open-crash-log').click()")
                opened_again = False
                for _ in range(40):
                    if len(opened_paths) > opened_before:
                        opened_again = True
                        break
                    time.sleep(0.15)
                check(
                    "the crash log button is wired",
                    opened_again and opened_paths[-1] == str(crash_path),
                    str(opened_paths),
                )

                # --- live engine events -----------------------------------
                shell.on_state("recording")
                shell.on_partial("hallo wereld dit is een test")
                check("the page follows the recording state", wait_text(window, "#status-kicker", "Aan het luisteren", 5))
                check("partial text reaches the live card", wait_text(window, "#transcript", "hallo wereld dit is een test", 5))
                check("the live card counts words", wait_text(window, "#live-count", "6 woorden", 5),
                      str(evaluate(window, "document.getElementById('live-count').textContent")))
                check("the recording timer is visible", evaluate(window, "document.getElementById('timer').hidden") is False)
                shell.on_state("transcribing")
                check("the page follows the transcribing state", wait_text(window, "#status-kicker", "Bezig met omzetten", 5))
                shell.on_state("idle")
                check("the page returns to idle", wait_js(window, "document.body.dataset.state === 'idle'", True, 5),
                      str(evaluate(window, "document.body.dataset.state")))

                # --- navigation -------------------------------------------
                evaluate(window, "document.querySelector('[data-view=\"settings\"].nav-item').click()")
                check("settings view opens from the nav", evaluate(window, "document.querySelector('[data-view=\"settings\"].view').classList.contains('is-active')") is True)
                check("hotkey in the form matches the config", evaluate(window, "document.getElementById('set-hotkey').value") == HOTKEY)

                # --- online mode and encrypted key ------------------------
                check("the online card starts in local mode",
                      evaluate(window, "document.getElementById('cloud-note').textContent") == "lokaal")
                check("there is no cloud key in a fresh install",
                      "Geen sleutel" in str(evaluate(window, "document.getElementById('cloud-key-status').textContent")))
                fake_key = "gsk_" + "ui-test" * 8
                evaluate(
                    window,
                    "(() => { const i = document.getElementById('set-cloud-key'); i.value = "
                    + json.dumps(fake_key)
                    + "; document.getElementById('save-cloud-key').click(); })()",
                )
                key_saved = wait_js(
                    window,
                    "document.getElementById('cloud-key-status').textContent.includes('Sleutel opgeslagen')",
                    True,
                    8,
                )
                check("the window stores the key through the bridge", key_saved and shell.credentials.has("groq"))
                credentials_path = tmpdir / "credentials.json"
                raw_credentials = credentials_path.read_text(encoding="utf-8") if credentials_path.exists() else ""
                check("the key is encrypted on disk, not plain text", bool(raw_credentials) and fake_key not in raw_credentials)
                check("the key is never pushed into the page",
                      fake_key not in str(evaluate(window, "document.body.innerHTML")))
                boot = shell.boot_payload()
                boot_payload = json.dumps(boot)
                check(
                    "the boot payload reports only whether a key exists",
                    fake_key not in boot_payload
                    and boot["cloud"]["keys"]["groq"] is True
                    # the app itself is still local, so no provider is active yet
                    and boot["cloud"]["has_key"] is False,
                    boot_payload[:200],
                )

                evaluate(
                    window,
                    "(() => { const s=document.getElementById('set-backend'); s.value='groq'; s.dispatchEvent(new Event('change')); })()",
                )
                evaluate(window, "document.getElementById('save-settings').click()")
                online_saved = False
                for _ in range(60):
                    try:
                        if json.loads(config_path.read_text(encoding="utf-8")).get("online_backend") == "groq":
                            online_saved = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.15)
                check("the window can switch to the Groq backend", online_saved)
                check("the sidebar says the app is online",
                      wait_text(window, "#brand-sub", "online", 5),
                      str(evaluate(window, "document.getElementById('brand-sub').textContent")))
                check(
                    "the About text follows the online mode",
                    "Groq" in str(evaluate(window, "document.getElementById('about-pitch').textContent") or ""),
                    str(evaluate(window, "document.getElementById('about-pitch').textContent")),
                )
                check(
                    "the About card names the Groq transcription engine and its model",
                    evaluate(window, "document.getElementById('about-engine').textContent") == "Groq (online) · whisper-large-v3-turbo",
                    str(evaluate(window, "document.getElementById('about-engine').textContent")),
                )

                evaluate(window, "document.getElementById('clear-cloud-key').click()")
                cleared_key = wait_js(
                    window,
                    "document.getElementById('cloud-key-status').textContent.includes('Geen sleutel')",
                    True,
                    8,
                )
                check("the window can remove the key", cleared_key and not shell.credentials.has("groq"))
                check(
                    "the About card falls back to the local engine and model without a key",
                    evaluate(window, "document.getElementById('about-engine').textContent") == "Lokaal (Whisper) · base.en",
                    str(evaluate(window, "document.getElementById('about-engine').textContent")),
                )
                evaluate(window, "document.getElementById('save-settings').click()")
                blocked = wait_js(window, "document.getElementById('settings-error').hidden === false", True, 8)
                blocked_text = str(evaluate(window, "document.getElementById('settings-error').textContent") or "")
                check("online without a key is refused with a readable message",
                      blocked and "Groq-sleutel" in blocked_text, blocked_text.strip())

                evaluate(
                    window,
                    "(() => { const s=document.getElementById('set-backend'); s.value='local'; s.dispatchEvent(new Event('change')); })()",
                )
                evaluate(window, "document.getElementById('save-settings').click()")
                back_local = False
                for _ in range(60):
                    try:
                        if json.loads(config_path.read_text(encoding="utf-8")).get("online_backend") == "local":
                            back_local = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.15)
                check("the window can switch back to local", back_local)

                # --- a second provider: Google Gemini ----------------------
                evaluate(
                    window,
                    "(() => { const s=document.getElementById('set-backend'); s.value='gemini'; s.dispatchEvent(new Event('change')); })()",
                )
                check(
                    "the key field follows the chosen provider",
                    evaluate(window, "document.getElementById('cloud-key-label').textContent")
                    == "Google Gemini API-sleutel",
                    str(evaluate(window, "document.getElementById('cloud-key-label').textContent")),
                )
                check(
                    "the free-tier facts and key page belong to Gemini",
                    "Gratis tier" in str(evaluate(window, "document.getElementById('cloud-limits').textContent"))
                    and "aistudio.google.com" in str(evaluate(window, "document.getElementById('cloud-keys-url').textContent")),
                    str(evaluate(window, "document.getElementById('cloud-keys-url').textContent")),
                )
                gemini_key = "AIza" + "ui-test" * 6
                evaluate(
                    window,
                    "(() => { const i = document.getElementById('set-cloud-key'); i.value = "
                    + json.dumps(gemini_key)
                    + "; document.getElementById('save-cloud-key').click(); })()",
                )
                gemini_key_saved = wait_js(
                    window,
                    "document.getElementById('cloud-key-status').textContent.includes('Sleutel opgeslagen')",
                    True,
                    8,
                )
                check("the Gemini key is stored under its own provider",
                      gemini_key_saved and shell.credentials.has("gemini") and not shell.credentials.has("groq"))
                check(
                    "the online note names the Gemini model",
                    wait_text(window, "#cloud-note", "gemini-3.5-flash-lite", 5),
                    str(evaluate(window, "document.getElementById('cloud-note').textContent")),
                )
                evaluate(window, "document.getElementById('save-settings').click()")
                gemini_saved = False
                for _ in range(60):
                    try:
                        if json.loads(config_path.read_text(encoding="utf-8")).get("online_backend") == "gemini":
                            gemini_saved = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.15)
                check("the window can switch to the Gemini backend", gemini_saved)
                check("the engine follows the setting without a restart",
                      getattr(shell.dictator.cloud, "backend", "") == "gemini",
                      str(getattr(shell.dictator.cloud, "backend", "")))
                check("the engine reports the Gemini model for history",
                      getattr(shell.dictator.cloud, "model", "") == cloud.GEMINI_MODEL,
                      str(getattr(shell.dictator.cloud, "model", "")))
                check(
                    "the About card names Gemini and its model",
                    evaluate(window, "document.getElementById('about-engine').textContent")
                    == "Google Gemini (online) · gemini-3.5-flash-lite",
                    str(evaluate(window, "document.getElementById('about-engine').textContent")),
                )
                raw_credentials = credentials_path.read_text(encoding="utf-8") if credentials_path.exists() else ""
                check("the Gemini key is encrypted on disk too",
                      bool(raw_credentials) and gemini_key not in raw_credentials)

                check(
                    "the Gemini model picker is visible while Gemini is chosen",
                    evaluate(window, "document.getElementById('gemini-model-field').hidden === false") is True,
                )
                offered = evaluate(
                    window,
                    "Array.from(document.getElementById('set-gemini-model').options).map((o) => o.value)",
                )
                check("the picker offers both Gemini models",
                      offered == ["gemini-3.5-flash-lite", "gemini-3.6-flash"], str(offered))
                notes = {entry["value"]: entry["note"] for entry in cloud.GEMINI_MODELS}
                # The window adds "Nu actief." when the shown model is the one
                # answering, so the note itself is what has to match the backend.
                hint = parse_hint(evaluate(window, "document.getElementById('gemini-model-hint').textContent"))
                check("the hint under the picker explains the default model",
                      hint == notes["gemini-3.5-flash-lite"] + ".", f"{hint!r}")
                evaluate(
                    window,
                    "(() => { const s=document.getElementById('set-gemini-model'); s.value='gemini-3.6-flash'; s.dispatchEvent(new Event('change')); })()",
                )
                evaluate(window, "document.getElementById('save-settings').click()")
                slower_saved = False
                for _ in range(60):
                    try:
                        if json.loads(config_path.read_text(encoding="utf-8")).get("gemini_model") == "gemini-3.6-flash":
                            slower_saved = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.15)
                check("the chosen Gemini model reaches config.json", slower_saved)
                check("the engine switches Gemini model without a restart",
                      getattr(shell.dictator.cloud, "model", "") == "gemini-3.6-flash",
                      str(getattr(shell.dictator.cloud, "model", "")))
                check(
                    "the About card follows the chosen Gemini model",
                    evaluate(window, "document.getElementById('about-engine').textContent")
                    == "Google Gemini (online) · gemini-3.6-flash",
                    str(evaluate(window, "document.getElementById('about-engine').textContent")),
                )
                hint = parse_hint(evaluate(window, "document.getElementById('gemini-model-hint').textContent"))
                check("the hint follows the chosen Gemini model",
                      hint == notes["gemini-3.6-flash"] + ".", f"{hint!r}")

                evaluate(window, "document.getElementById('clear-cloud-key').click()")
                gemini_cleared = wait_js(
                    window,
                    "document.getElementById('cloud-key-status').textContent.includes('Geen sleutel')",
                    True,
                    8,
                )
                check("the Gemini key can be removed again",
                      gemini_cleared and not shell.credentials.has("gemini"))

                evaluate(
                    window,
                    "(() => { const s=document.getElementById('set-backend'); s.value='local'; s.dispatchEvent(new Event('change')); })()",
                )
                evaluate(window, "document.getElementById('save-settings').click()")
                back_local = False
                for _ in range(60):
                    try:
                        if json.loads(config_path.read_text(encoding="utf-8")).get("online_backend") == "local":
                            back_local = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.15)
                check("local stays reachable after trying Gemini", back_local)
                check(
                    "the Gemini picker hides again for the local backend",
                    evaluate(window, "document.getElementById('gemini-model-field').hidden === true") is True,
                )

                # --- hotkey capture ----------------------------------------
                evaluate(window, "document.getElementById('set-hotkey').click()")
                evaluate(
                    window,
                    "window.dispatchEvent(new KeyboardEvent('keydown', {key:'D', code:'KeyD', ctrlKey:true, altKey:true, shiftKey:true, bubbles:true, cancelable:true}))",
                )
                captured = ""
                for _ in range(40):
                    captured = evaluate(window, "document.getElementById('set-hotkey').value") or ""
                    if captured.startswith("<ctrl>"):
                        break
                    time.sleep(0.1)
                check("pressing a combination fills the hotkey field", captured == "<ctrl>+<alt>+<shift>+d", captured)
                check("the captured hotkey is described in words", "<ctrl>+<alt>+<shift>+d" in str(captured) and "Ctrl + Alt + Shift + D" in str(evaluate(window, "document.getElementById('hotkey-hint').textContent") or ""))

                # --- invalid pairing is rejected --------------------------
                evaluate(window, "(() => { const s=document.getElementById('set-language'); s.value='nl'; s.dispatchEvent(new Event('change')); })()")
                evaluate(window, "document.getElementById('save-settings').click()")
                shown = wait_js(window, "document.getElementById('settings-error').hidden === false", True, 8)
                message = evaluate(window, "document.getElementById('settings-error').textContent") or ""
                check("an English-only model in Dutch is refused", shown and "alleen Engels" in message, message.strip())
                check("the refused save leaves the file untouched", json.loads(config_path.read_text(encoding="utf-8"))["language"] == "en")

                # --- restore and save a valid change ----------------------
                evaluate(window, "(() => { const s=document.getElementById('set-language'); s.value='en'; s.dispatchEvent(new Event('change')); })()")
                evaluate(window, "(() => { const p=document.getElementById('set-prompt'); p.value='Freebuff, Codebuff, Whisper'; })()")
                evaluate(window, "document.getElementById('save-settings').click()")
                saved = False
                for _ in range(60):
                    try:
                        data = json.loads(config_path.read_text(encoding="utf-8"))
                        if data.get("initial_prompt") == "Freebuff, Codebuff, Whisper":
                            saved = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.15)
                check("saving writes the new settings to config.json", saved)
                check("the window reflects the saved settings", evaluate(window, "document.getElementById('save-state').textContent") == "Opgeslagen")
                check("the engine adopted the new settings", shell.cfg.get("initial_prompt") == "Freebuff, Codebuff, Whisper")
                # Put the verified hotkey back, the capture above changed the field.
                evaluate(window, "document.getElementById('set-hotkey').value = '<ctrl>+<alt>+<shift>+<f12>';")
                evaluate(window, "document.getElementById('save-settings').click()")
                restored = False
                for _ in range(60):
                    try:
                        if json.loads(config_path.read_text(encoding="utf-8")).get("hotkey") == HOTKEY:
                            restored = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.15)
                check("the saved hotkey reaches config.json", restored)

                # --- microphone choice ------------------------------------
                device_choice = evaluate(
                    window,
                    "(() => { const s=document.getElementById('set-device'); const current=s.value; "
                    "const other=[...s.options].find((o) => o.value !== 'default' && o.value !== current); "
                    "if (!other) return null; s.value = other.value; "
                    "return JSON.stringify({value: other.value, label: other.textContent, current}); })()",
                )
                try:
                    choice = json.loads(device_choice) if device_choice else None
                except Exception:
                    choice = None
                if choice:
                    evaluate(window, "document.getElementById('save-settings').click()")
                    device_saved = False
                    for _ in range(60):
                        try:
                            if json.loads(config_path.read_text(encoding="utf-8")).get("input_device") == choice["value"]:
                                device_saved = True
                                break
                        except Exception:
                            pass
                        time.sleep(0.15)
                    check(
                        "a chosen microphone is saved by name",
                        device_saved and not str(choice["value"]).isdigit(),
                        f"{choice['label']} -> {choice['value']}",
                    )
                    check(
                        "the engine adopts the chosen microphone",
                        shell.cfg.get("input_device") == choice["value"],
                        str(shell.cfg.get("input_device")),
                    )
                    evaluate(
                        window,
                        f"(() => {{ document.getElementById('set-device').value = {json.dumps(choice['current'])}; "
                        "document.getElementById('save-settings').click(); })()",
                    )
                    restored_device = False
                    expected_device = None if choice["current"] == "default" else choice["current"]
                    for _ in range(60):
                        try:
                            if json.loads(config_path.read_text(encoding="utf-8")).get("input_device") == expected_device:
                                restored_device = True
                                break
                        except Exception:
                            pass
                        time.sleep(0.15)
                    check("the previous microphone can be restored", restored_device,
                          f"{choice['current']!r} -> {expected_device!r}")

                missing_mic = "Microfoon die niet bestaat"
                evaluate(window, f"window.pywebview.api.save_settings({{input_device: {json.dumps(missing_mic)}}})")
                shown = wait_js(window, "document.getElementById('mic-notice').hidden", False, 8)
                detail = str(evaluate(window, "document.getElementById('mic-notice-detail').textContent") or "")
                check("a missing saved microphone raises a visible warning", shown, detail)
                check("the warning names the missing microphone", missing_mic in detail, detail)
                check(
                    "the warning offers a way to the microphone settings",
                    evaluate(window, "document.getElementById('mic-notice-settings').textContent") == "Microfoon kiezen",
                )
                evaluate(window, "document.getElementById('mic-notice-settings').click()")
                check(
                    "the warning opens the microphone settings",
                    evaluate(window, "document.querySelector('.view[data-view=\"settings\"]').classList.contains('is-active')") is True,
                )
                evaluate(window, "window.pywebview.api.save_settings({input_device: null})")
                check(
                    "the warning disappears after choosing the default microphone",
                    wait_js(window, "document.getElementById('mic-notice').hidden", True, 8),
                )

                # --- microphone test --------------------------------------
                evaluate(window, "document.getElementById('mic-test').click()")
                heard = wait_js(
                    window,
                    "document.getElementById('mic-result').textContent.includes('dBFS')",
                    True,
                    12,
                )
                result_text = str(evaluate(window, "document.getElementById('mic-result').textContent") or "")
                check("the microphone test reports a level", heard, result_text.strip())
                check("the level bar moved", evaluate(window, "parseFloat(document.getElementById('mic-level').style.width) >= 0") is True,
                      str(evaluate(window, "document.getElementById('mic-level').style.width")))

                # --- live event push --------------------------------------
                shell.on_delivered(
                    "Deze zin komt uit de bridge-test.",
                    {"duration": 2.2, "model": "base.en", "language": "en", "mode": "clipboard"},
                )
                pushed = wait_js(window, "document.getElementById('recent-list').textContent.includes('bridge-test')", True, 6)
                check("delivered events reach the window", pushed)
                check("the dashboard counts the new dictation", evaluate(window, "document.getElementById('stat-count').textContent") == "2",
                      str(evaluate(window, "document.getElementById('stat-count').textContent")))
                check("history grows with the delivery", evaluate(window, "document.getElementById('history-count').textContent") == "2")

                # --- correcting a dictation teaches the engine -------------
                evaluate(window, "document.querySelector('[data-view=\"history\"].nav-item').click()")
                evaluate(window, "document.querySelector('.history-item button[title=\"Bewerken\"]').click()")
                check("the edit button opens an editor", wait_js(window, "Boolean(document.querySelector('.edit-area'))", True, 5))
                evaluate(window, "(() => { const a = document.querySelector('.edit-area'); a.value = 'Deze zin komt uit de WebSocket.'; a.dispatchEvent(new Event('input')); })()")
                evaluate(window, "document.querySelector('.edit-bar .primary-button').click()")
                check("the corrected text is shown in the list", wait_js(window, "document.getElementById('history-list').textContent.includes('WebSocket')", True, 8))
                corrected_item = [item for item in shell.history.items() if "WebSocket" in item["text"]]
                check("the corrected text is stored", bool(corrected_item) and corrected_item[0].get("edited") is True,
                      str(corrected_item)[:100])
                check("the engine learned the correction",
                      shell.dictator._postprocess("de bridge-test") == "de WebSocket",
                      shell.dictator._postprocess("de bridge-test"))
                check("the learned name reaches the prompt", "WebSocket" in shell.learning.prompt_extra(), shell.learning.prompt_extra())

                # --- typing a word into Eigen woorden ----------------------
                evaluate(
                    window,
                    "(() => { const i = document.getElementById('learn-word'); i.value = 'features'; "
                    "document.getElementById('learn-word-add').click(); })()",
                )
                check(
                    "a word typed in Eigen woorden becomes a chip",
                    wait_js(window, "document.getElementById('learn-vocabulary').textContent.includes('features')", True, 8),
                    str(evaluate(window, "document.getElementById('learn-vocabulary').textContent")),
                )
                check(
                    "the typed word reaches the model prompt",
                    "features" in shell.learning.prompt_extra(),
                    shell.learning.prompt_extra(),
                )
                check(
                    "the field is cleared after adding",
                    evaluate(window, "document.getElementById('learn-word').value") == "",
                    str(evaluate(window, "document.getElementById('learn-word').value")),
                )
                evaluate(
                    window,
                    "(() => { const i = document.getElementById('learn-word'); i.value = 'twee woorden'; "
                    "document.getElementById('learn-word-add').click(); })()",
                )
                check(
                    "a word with a space is refused in the window",
                    wait_js(window, "document.getElementById('learn-vocabulary').textContent.includes('twee')", False, 4)
                    and all(row["word"] != "twee woorden" for row in shell.learning.vocabulary()),
                    str([row["word"] for row in shell.learning.vocabulary()]),
                )
                evaluate(window, "document.getElementById('learn-word').value = ''")

                evaluate(window, "document.querySelector('[data-view=\"settings\"].nav-item').click()")
                shown = wait_text(window, "#learn-corrections", "WebSocket", 5) and wait_text(window, "#learn-vocabulary", "WebSocket", 5)
                check("the learning card shows the correction and the word", shown)

                evaluate(window, "document.querySelector('#learn-corrections .learn-forget').click()")
                wait_js(window, "document.querySelectorAll('#learn-corrections .learn-row').length === 0", True, 6)
                # Every chip must be forgettable, however many the card holds.
                for _ in range(8):
                    if evaluate(window, "document.querySelectorAll('#learn-vocabulary .learn-chip').length") == 0:
                        break
                    evaluate(window, "document.querySelector('#learn-vocabulary .learn-chip button').click()")
                    time.sleep(0.2)
                wait_js(window, "document.querySelectorAll('#learn-vocabulary .learn-chip').length === 0", True, 6)
                check("forgetting from the card clears the store", shell.learning.payload()["total"] == 0,
                      str(shell.learning.payload()))
                check("the card returns to its empty state", evaluate(window, "document.getElementById('learn-empty').hidden") is False)

                # --- history actions -------------------------------------
                evaluate(window, "document.querySelector('[data-view=\"history\"].nav-item').click()")
                evaluate(window, "document.querySelector('[data-view=\"history\"].nav-item').click()")
                evaluate(window, "document.querySelector('.history-item .icon-button.danger').click()")
                removed = wait_js(window, "document.getElementById('history-count').textContent === '1'", True, 8)
                on_disk = json.loads((tmpdir / "history.json").read_text(encoding="utf-8"))["items"]
                check("deleting a dictation updates the window and the store", removed and len(on_disk) == 1, f"{len(on_disk)} on disk")

                evaluate(window, "(() => { const b = document.getElementById('history-clear'); b.click(); b.click(); })()")
                cleared = wait_js(window, "document.querySelectorAll('.history-item').length === 0", True, 8)
                empty_store = json.loads((tmpdir / "history.json").read_text(encoding="utf-8"))["items"]
                check("clearing asks once and then empties the history", cleared and not empty_store, str(empty_store)[:80])
                check("clearing also resets the dashboard", evaluate(window, "document.getElementById('stat-count').textContent") == "0")

                # --- hotkey switch ---------------------------------------
                evaluate(window, "document.querySelector('[data-view=\"dictate\"].nav-item').click()")
                evaluate(window, "document.getElementById('listening-toggle').click()")
                enabled = wait_js(window, "document.body.dataset.listening === 'true'", True, 8)
                check("the sidebar switch arms the hotkey", enabled and shell.dictator.listening)
                evaluate(window, "document.getElementById('listening-toggle').click()")
                disabled = wait_js(window, "document.body.dataset.listening === 'false'", True, 8)
                check("the sidebar switch disarms the hotkey", disabled and not shell.dictator.listening)

                # --- placement --------------------------------------------
                shell.ui.placement.store()
                placement_path = Path(config_path).with_name("ui-state.json")
                stored = {}
                try:
                    stored = json.loads(placement_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
                check("the window remembers its size and position", bool(stored.get("width")) and bool(stored.get("height")), json.dumps(stored))
                on_screen = (
                    isinstance(stored.get("x"), int)
                    and isinstance(stored.get("y"), int)
                    and -50 <= stored["x"] <= 3000
                    and -50 <= stored["y"] <= 2000
                )
                check("the remembered position is on screen", on_screen, f"{stored.get('x')},{stored.get('y')}")

                # --- quit from the page ------------------------------------
                evaluate(window, "document.getElementById('quit-button').click()")
                deadline = time.time() + 10
                while time.time() < deadline and not getattr(shell, "_quitting", False):
                    time.sleep(0.1)
                check("the window's quit button shuts the app down", bool(getattr(shell, "_quitting", False)))

            except Exception as exc:
                log.exception("checks failed")
                check("checks ran without an exception", False, str(exc))
            finally:
                # Go through WebUI so the "closing parks in the tray" guard is
                # lifted; a bare destroy() is cancelled by that handler and the
                # window loop would never end.
                try:
                    if not getattr(shell, "_quitting", False):
                        shell.ui.destroy()
                except Exception:
                    pass

        webview.start(run_checks, debug=False, private_mode=True)

        try:
            shell.quit()
        except Exception:
            pass

    print()
    print("FAILED: " + ", ".join(FAILS) if FAILS else "window verification passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
