<#
.SYNOPSIS
    Build the svn2gitlab Windows installer.

.DESCRIPTION
    Creates a clean virtual environment, freezes the application with PyInstaller and
    (if Inno Setup is present) produces svn2gitlab-setup-<version>.exe in dist\.

    Run this on the same Windows major version you intend to deploy to. A binary
    frozen on Windows 11 generally runs on Server 2019, but the reverse is not
    guaranteed, and the client's platform is Server 2019.

.PARAMETER Vendor
    Optional path to a folder containing `git\` and `svn\` subfolders to embed in the
    installer for offline deployment. Tool discovery looks in <bundle>\tools\{git,svn}.

.EXAMPLE
    .\installer\build.ps1
    .\installer\build.ps1 -Vendor C:\offline-tools
#>
[CmdletBinding()]
param(
    [string]$Vendor = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Warn($message) { Write-Host "!!  $message" -ForegroundColor Yellow }

try {
    Write-Step "Checking Python"
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { throw "Python 3.9+ is required and was not found on PATH." }
    $version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
    Write-Host "    Python $version"
    if ([version]$version -lt [version]"3.9") { throw "Python 3.9 or newer is required (found $version)." }

    Write-Step "Creating a clean build environment"
    if (Test-Path .buildenv) { Remove-Item -Recurse -Force .buildenv }
    & python -m venv .buildenv
    $venvPython = Join-Path $Root ".buildenv\Scripts\python.exe"

    Write-Step "Installing dependencies"
    & $venvPython -m pip install --upgrade pip --quiet
    & $venvPython -m pip install -e ".[dev]" --quiet

    if (-not $SkipTests) {
        Write-Step "Running tests"
        & $venvPython -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw "Tests failed; not building an installer from a failing tree." }
    }

    if ($Vendor) {
        Write-Step "Staging vendored tools from $Vendor"
        $target = Join-Path $PSScriptRoot "vendor"
        if (Test-Path $target) { Remove-Item -Recurse -Force $target }
        New-Item -ItemType Directory -Path $target | Out-Null
        Copy-Item -Recurse -Path (Join-Path $Vendor "*") -Destination $target
    }

    Write-Step "Freezing with PyInstaller"
    if (Test-Path dist\svn2gitlab) { Remove-Item -Recurse -Force dist\svn2gitlab }
    if (Test-Path build) { Remove-Item -Recurse -Force build }
    & $venvPython -m PyInstaller installer\svn2gitlab.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    Write-Step "Smoke-testing the frozen binary"
    $exe = Join-Path $Root "dist\svn2gitlab\svn2gitlab.exe"
    & $exe version
    if ($LASTEXITCODE -ne 0) { throw "The frozen binary does not start." }

    Write-Step "Building the installer"
    $iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if (-not $iscc) {
        $candidates = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
        )
        $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($found) { $iscc = $found }
    }
    if ($iscc) {
        & $iscc installer\svn2gitlab.iss
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
        Write-Host ""
        Write-Host "Installer written to dist\" -ForegroundColor Green
        Get-ChildItem dist\svn2gitlab-setup-*.exe | ForEach-Object { Write-Host "    $($_.FullName)" }
    } else {
        Write-Warn "Inno Setup 6 was not found, so no setup.exe was produced."
        Write-Warn "Install it from https://jrsoftware.org/isdl.php and re-run, or ship"
        Write-Warn "dist\svn2gitlab\ as a portable folder - it is fully self-contained."
    }

    Write-Host ""
    Write-Host "Build complete." -ForegroundColor Green
}
finally {
    Pop-Location
}
