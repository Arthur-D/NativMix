<#
.SYNOPSIS
    Copy repo to a local temp folder and run the build from there.
    Workaround for running build.ps1 via VM shared folder (Z:).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File Z:\packaging\win\build_local.ps1
#>

$ErrorActionPreference = "Stop"

$REPO_SRC  = (Resolve-Path "$PSScriptRoot\..\..")   # Z:\  (repo root on shared drive)
$BUILD_DIR = "C:\nativmix_build"

# -- 1. Kill any running NativMix instances (they lock DLLs in dist/) --------
foreach ($name in @("nativmix", "python", "pythonw")) {
    Get-Process -Name $name -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "*nativmix*" } |
        Stop-Process -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2

# -- 2. Clean temp folder -----------------------------------------------------
if (Test-Path $BUILD_DIR) {
    Write-Host "Cleaning $BUILD_DIR ..." -ForegroundColor Yellow
    # Use cmd rd instead of Remove-Item -- handles locked DLLs more reliably
    & cmd /c rd /s /q $BUILD_DIR
    if (Test-Path $BUILD_DIR) {
        Write-Error "Could not delete $BUILD_DIR - close any programs using it and retry."
        exit 1
    }
}
New-Item -ItemType Directory -Force $BUILD_DIR | Out-Null
Write-Host "Created $BUILD_DIR" -ForegroundColor DarkGray

# -- 3. Copy repo -------------------------------------------------------------
Write-Host "Copying repo from $REPO_SRC ..." -ForegroundColor Yellow
Copy-Item "$REPO_SRC\*" $BUILD_DIR -Recurse -Force -Exclude ".git",".venv","__pycache__","*.pyc"
Write-Host "Copy done." -ForegroundColor DarkGray

# -- 4. Run build -------------------------------------------------------------
$script = "$BUILD_DIR\packaging\win\build.ps1"
Write-Host "Starting build: $script" -ForegroundColor Cyan
powershell -ExecutionPolicy Bypass -File $script
