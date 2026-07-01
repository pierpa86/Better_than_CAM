param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Icon = Join-Path $Root "btcam.ico"
$IconData = "$Icon;."

function Test-VenvPython {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return $false
    }

    try {
        & $Path -c "import sys" *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if (-not (Test-VenvPython $Python)) {
    if (Test-Path -LiteralPath $Venv) {
        Remove-Item -LiteralPath $Venv -Recurse -Force
    }
    py -m venv $Venv
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r (Join-Path $Root "requirements-dev.txt")
& $Python -m pip install -e $Root

if ($Clean) {
    $buildDir = Join-Path $Root "build"
    $distDir = Join-Path $Root "dist"
    $specFile = Join-Path $Root "BTCAM.spec"
    foreach ($target in @($buildDir, $distDir, $specFile)) {
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }
}

if (-not (Test-Path -LiteralPath $Icon)) {
    throw "Icona non trovata: $Icon"
}

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --noupx `
    --onefile `
    --windowed `
    --uac-admin `
    --icon $Icon `
    --add-data $IconData `
    --name BTCAM `
    --specpath (Join-Path $Root "build") `
    --paths (Join-Path $Root "src") `
    --collect-all HardwareMonitor `
    --collect-submodules liquidctl `
    --collect-submodules winusbcdc `
    --collect-binaries libusb_package `
    (Join-Path $Root "src\BTCAM\standalone.py")

$Exe = Join-Path $Root "dist\BTCAM.exe"
if (-not (Test-Path $Exe)) {
    throw "Build completata senza trovare $Exe"
}

Write-Host "Exe creato: $Exe"
