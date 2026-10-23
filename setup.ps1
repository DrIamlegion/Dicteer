# Dicteer - one-time setup / repair.
# Re-runnable: reinstalls the pinned dependencies and pre-downloads the model.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host 'Creating virtual environment...' -ForegroundColor Cyan
    python -m venv .venv
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
}

Write-Host 'Installing dependencies...' -ForegroundColor Cyan
& $python -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

Write-Host 'Pre-downloading the speech model (first time only, ~250 MB)...' -ForegroundColor Cyan
& $python -c "import json, pathlib; from faster_whisper import WhisperModel; cfg = json.loads(pathlib.Path('config.json').read_text()); WhisperModel(cfg['model'], device='cpu', compute_type=cfg.get('compute_type','int8'), download_root='models'); print('model ready:', cfg['model'])"
if ($LASTEXITCODE -ne 0) { throw "model download failed (exit $LASTEXITCODE)" }

Write-Host ''
Write-Host 'Done. Start with:  Dicteer.vbs  (opens the window, tray keeps running)' -ForegroundColor Green
Write-Host 'For a console with live status use:  dictate.bat' -ForegroundColor Green
Write-Host 'The window uses the Microsoft Edge WebView2 runtime, which ships with Windows 10/11.' -ForegroundColor Green
Write-Host 'To start with Windows, tick "Start met Windows" in Settings.' -ForegroundColor Green
