# Freebuff Dictate - build the packaged app and the Windows installer.
#
#   .\build-installer.ps1              tests, then exe + installer
#   .\build-installer.ps1 -SkipTests   exe + installer only
#   .\build-installer.ps1 -ExeOnly     exe only, no installer
#
# Output:
#   dist\Freebuff Dictate\                      the app folder (run the .exe in it)
#   dist\Freebuff-Dictate-Setup-<version>.exe   the installer
#
# Everything is per-user: no administrator rights are needed to build or to
# install. The first build downloads PyInstaller and NSIS into the project.
param(
    [switch]$SkipTests,
    [switch]$ExeOnly,

    # Code signing. A .pfx file with its password, or the thumbprint of a
    # certificate in CurrentUser\My / LocalMachine\My (also works for a token).
    # The environment variables keep the password out of the command line.
    [string]$PfxPath = $env:FREEBUFF_SIGN_PFX,
    [string]$PfxPassword = $env:FREEBUFF_SIGN_PFX_PASSWORD,
    [string]$CertThumbprint = $env:FREEBUFF_SIGN_THUMBPRINT,
    [string]$TimestampServer = 'http://timestamp.digicert.com',

    # Fail instead of shipping an unsigned build (use this for releases).
    [switch]$RequireSignature
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# A local build runs from the venv setup.ps1 creates. CI checks the repository
# out fresh - no venv, but a prepared Python on PATH - so fall back to that
# instead of refusing to build there.
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $onPath = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $onPath) {
        throw "No .venv found and no python on PATH. Run .\setup.ps1 first, then this script again."
    }
    $python = $onPath.Source
    Write-Warning "No .venv in this checkout; using $python."
}

$version = (& $python (Join-Path $PSScriptRoot 'installer\version.py')).Trim()
if (-not $version) { throw 'Could not read APP_VERSION from desktop.py' }
$fileVersion = "$version.0.0"
Write-Host "Freebuff Dictate $version" -ForegroundColor Cyan

# The exe's Windows version resource comes from version_info.txt, which repeats
# the number by hand; stop here rather than ship an exe labelled with an old one.
& $python (Join-Path $PSScriptRoot 'installer\version.py') --check
if ($LASTEXITCODE -ne 0) {
    throw "installer\version_info.txt does not match APP_VERSION $version (see above); fix it before building."
}

if (-not $SkipTests) {
    Write-Host 'Running self-tests...' -ForegroundColor Cyan
    & $python selftest.py
    if ($LASTEXITCODE -ne 0) { throw "Self-tests failed (exit $LASTEXITCODE)" }
}

Write-Host 'Installing build dependencies...' -ForegroundColor Cyan
& $python -m pip install --disable-pip-version-check --quiet pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

Write-Host 'Building the app folder (this takes a few minutes)...' -ForegroundColor Cyan
& $python -m PyInstaller --noconfirm --clean FreebuffDictate.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

$appExe = Join-Path $PSScriptRoot 'dist\Freebuff Dictate\Freebuff Dictate.exe'
if (-not (Test-Path $appExe)) { throw "PyInstaller did not produce $appExe" }
Write-Host "  $appExe" -ForegroundColor Green

# A package hook can copy compiled caches into the app folder, and a .pyc
# records the absolute path it was built from - the builder's own user folder.
# They are never needed at runtime, so nothing built here ships one.
Get-ChildItem -LiteralPath (Split-Path $appExe) -Directory -Filter '__pycache__' -Recurse |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$signing = [bool]($PfxPath -or $CertThumbprint)
if ($signing) {
    Write-Host 'Signing the app...' -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot 'sign.ps1') -Path $appExe -PfxPath $PfxPath -PfxPassword $PfxPassword `
        -Thumbprint $CertThumbprint -TimestampServer $TimestampServer
} elseif ($RequireSignature) {
    throw 'No signing certificate. Pass -PfxPath / -CertThumbprint or set FREEBUFF_SIGN_PFX / FREEBUFF_SIGN_THUMBPRINT.'
} else {
    Write-Warning 'Building an unsigned installer: Windows will show it as an unknown publisher.'
}

if ($ExeOnly) { return }

$candidates = @(
    (Join-Path ${env:ProgramFiles(x86)} 'NSIS\makensis.exe'),
    (Join-Path $env:ProgramFiles 'NSIS\makensis.exe'),
    (Join-Path $PSScriptRoot 'tools\nsis-3.11\makensis.exe')
)
$nsis = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $nsis) {
    $nsisDir = Join-Path $PSScriptRoot 'tools\nsis-3.11'
    $zip = Join-Path $PSScriptRoot 'tools\nsis-3.11.zip'
    Write-Host 'Downloading NSIS 3.11 (once, ~2 MB)...' -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot 'tools') | Out-Null
    $nsisUrl = 'https://downloads.sourceforge.net/project/nsis/NSIS%203/3.11/nsis-3.11.zip'
    # SourceForge hands Invoke-WebRequest an HTML mirror page instead of the
    # archive, so use curl.exe (part of Windows 10+, and on CI runners) first.
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($curl) {
        & $curl.Source -L --fail --silent --show-error -o $zip $nsisUrl
        if ($LASTEXITCODE -ne 0) { throw "Downloading NSIS failed (curl exit $LASTEXITCODE)." }
    } else {
        Invoke-WebRequest -Uri $nsisUrl -OutFile $zip
    }
    $magic = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($zip), 0, 2)
    if ($magic -ne 'PK') {
        throw "The NSIS download is not a zip (starts with '$magic'). Download $nsisUrl in a browser and unpack it into tools\nsis-3.11."
    }
    Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $PSScriptRoot 'tools') -Force
    $nsis = Join-Path $nsisDir 'makensis.exe'
    if (-not (Test-Path $nsis)) { throw 'NSIS download did not produce makensis.exe' }
}

Write-Host 'Building the installer...' -ForegroundColor Cyan
& $nsis "/DAPP_VERSION=$version" "/DAPP_FILEVERSION=$fileVersion" "/DPROJECT_ROOT=$PSScriptRoot" (Join-Path $PSScriptRoot 'installer\FreebuffDictate.nsi')
if ($LASTEXITCODE -ne 0) { throw "makensis failed (exit $LASTEXITCODE)" }

$setup = Join-Path $PSScriptRoot "dist\Freebuff-Dictate-Setup-$version.exe"
if (-not (Test-Path $setup)) { throw "The installer was not produced at $setup" }

if ($signing) {
    Write-Host 'Signing the installer...' -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot 'sign.ps1') -Path $setup -PfxPath $PfxPath -PfxPassword $PfxPassword `
        -Thumbprint $CertThumbprint -TimestampServer $TimestampServer
}

Write-Host ''
Write-Host "Installer: $setup" -ForegroundColor Green
Write-Host 'It installs per user in %LOCALAPPDATA%\Programs\Freebuff Dictate.' -ForegroundColor Green
