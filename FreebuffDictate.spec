# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the packaged Freebuff Dictate app.

One folder rather than one file: CTranslate2, PyAV, WebView2 bridging and Tk
all ship native libraries, and a folder starts without unpacking ~300 MB into
%TEMP% on every launch. The installer ships that folder.

Build with ``build-installer.ps1`` (or ``pyinstaller FreebuffDictate.spec``).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
NAME = "Freebuff Dictate"

datas = [
    (str(ROOT / "ui"), "ui"),
    (str(ROOT / "assets"), "assets"),
]
binaries = []
hiddenimports = [
    # Windows integrations that are imported lazily at runtime.
    "clr",
    "pythonnet",
    "webview.platforms.winforms",
    "pystray._win32",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "sounddevice",
    "pyperclip",
    "PIL.ImageDraw",
    "tkinter",
    "faster_whisper",
    "ctranslate2",
    "av",
    "tokenizers",
    "huggingface_hub",
    "onnxruntime",
]

# These wheels load DLLs, tokenizer JSON and JavaScript shims from their own
# directories; collect_all keeps the data and native libraries together with
# the modules the hooks cannot infer.
for package in (
    "faster_whisper",
    "ctranslate2",
    "av",
    "tokenizers",
    "huggingface_hub",
    "onnxruntime",
    "webview",
    "pythonnet",
    "pystray",
    "pynput",
):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# A .pyc records the absolute path it was compiled from, so caches collected
# from site-packages would ship the builder's own folder name inside the app.
# They are regenerated at runtime and never needed as data files.
datas = [entry for entry in datas if "__pycache__" not in entry[0]]

a = Analysis(
    [str(ROOT / "tray.pyw")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "IPython",
        "matplotlib",
        "pandas",
        "scipy",
        "torch",
        "tensorflow",
        "PyQt5",
        "PySide6",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "icon.ico"),
    version=str(ROOT / "installer" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=NAME,
)
