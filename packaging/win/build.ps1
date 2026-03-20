<#
.SYNOPSIS
    Build the NativMix Windows installer (PyInstaller + Inno Setup).

.DESCRIPTION
    1. Creates a build venv under packaging/win/.venv (reused on subsequent runs).
    2. Installs NativMix with all Windows dependencies (pycaw, comtypes, psutil, ...).
    3. Runs PyInstaller -> dist/NativMix/ directory bundle.
    4. Runs Inno Setup -> packaging/win/output/NativMix-<version>-Setup.exe

.PARAMETER Version
    Override the version string (default: read from pyproject.toml).

.PARAMETER SkipInnoSetup
    Stop after PyInstaller - useful for CI or when Inno Setup is not installed.

.EXAMPLE
    # Normal build from repo root:
    powershell -ExecutionPolicy Bypass -File packaging\win\build.ps1

    # Specific version, skip installer:
    powershell -ExecutionPolicy Bypass -File packaging\win\build.ps1 -Version 1.0.7 -SkipInnoSetup
#>

[CmdletBinding()]
param(
    [string] $Version       = "",
    [switch] $SkipInnoSetup
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# -- Paths --------------------------------------------------------------------
$WIN_DIR = $PSScriptRoot                        # packaging/win/
$ROOT    = (Resolve-Path "$WIN_DIR\..\..")      # repo root
$VENV    = "$WIN_DIR\.venv"
$PY      = "$VENV\Scripts\python.exe"
$PIP     = "$VENV\Scripts\pip.exe"
$PYINST  = "$VENV\Scripts\pyinstaller.exe"

# -- 1. Version ---------------------------------------------------------------
if (-not $Version) {
    $toml = Get-Content "$ROOT\pyproject.toml" -Raw
    if ($toml -match '(?m)^version\s*=\s*"([^"]+)"') {
        $Version = $Matches[1]
    } else {
        Write-Error "Could not read version from pyproject.toml"
    }
}
Write-Host ""
Write-Host "  NativMix Windows Build  --  v$Version" -ForegroundColor Cyan
Write-Host ""

# -- 2. Virtual environment ---------------------------------------------------
if (-not (Test-Path $PY)) {
    Write-Host "[1/4] Creating build venv..." -ForegroundColor Yellow
    python -m venv $VENV
} else {
    Write-Host "[1/4] Build venv already exists - reusing." -ForegroundColor DarkGray
}

# -- 3. Install dependencies --------------------------------------------------
Write-Host "[2/4] Installing dependencies..." -ForegroundColor Yellow
& $PY -m pip install --quiet --upgrade pip
& $PIP install --quiet pyinstaller
# Install NativMix itself (editable) plus Windows extras.
# --upgrade ensures changed dependencies in pyproject.toml are picked up
# even when reusing an existing venv.
& $PIP install --quiet --upgrade -e "$ROOT[windows]"

# -- 4. PyInstaller -----------------------------------------------------------
Write-Host "[3/4] Running PyInstaller..." -ForegroundColor Yellow
Push-Location $ROOT
try {
    & $PYINST "$WIN_DIR\nativmix.spec" --noconfirm
} finally {
    Pop-Location
}

$bundle = "$ROOT\dist\NativMix\nativmix.exe"
if (-not (Test-Path $bundle)) {
    Write-Error "PyInstaller output not found at $bundle - build failed."
}
Write-Host "      Bundle: $ROOT\dist\NativMix\" -ForegroundColor DarkGray

# -- 5. Inno Setup ------------------------------------------------------------
if ($SkipInnoSetup) {
    Write-Host "[4/4] Inno Setup skipped (-SkipInnoSetup)." -ForegroundColor DarkGray
} else {
    Write-Host "[4/4] Running Inno Setup..." -ForegroundColor Yellow

    # Locate ISCC.exe (Inno Setup compiler)
    $iscc = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty Source
    if (-not $iscc) {
        $candidates = @(
            "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
            "C:\Program Files\Inno Setup 6\ISCC.exe"
        )
        $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    }

    if (-not $iscc) {
        Write-Warning "Inno Setup 6 not found - skipping installer creation."
        Write-Warning "Download from https://jrsoftware.org/isinfo.php and re-run."
    } else {
        New-Item -ItemType Directory -Force "$WIN_DIR\output" | Out-Null
        & $iscc /DMyAppVersion="$Version" "$WIN_DIR\nativmix.iss"

        $installer = Get-ChildItem "$WIN_DIR\output\NativMix-*.exe" |
                     Sort-Object LastWriteTime | Select-Object -Last 1
        if ($installer) {
            Write-Host ""
            Write-Host "  Installer ready: $($installer.FullName)" -ForegroundColor Green
        } else {
            Write-Error "Inno Setup finished but no output .exe found in $WIN_DIR\output\"
        }
    }
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
