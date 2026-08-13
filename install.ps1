<#
.SYNOPSIS
    Installs WebRunner on Windows and starts it automatically at boot.

.DESCRIPTION
    Creates a virtualenv under C:\ProgramData\WebRunner\app, installs
    WebRunner into it, and registers a Scheduled Task that starts the service
    at boot. Re-running upgrades in place and leaves your database alone.

    Run this from an ELEVATED PowerShell prompt (right click -> Run as
    Administrator). Without elevation it falls back to a per-user task that
    starts when you log in instead of at boot.

    Keep this file ASCII-only: Windows PowerShell 5.1 decodes a BOM-less
    UTF-8 script as Windows-1252, which mangles any non-ASCII character.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Port 9000 -Host 127.0.0.1
    .\install.ps1 -OpenFirewall
#>

[CmdletBinding()]
param(
    [int]    $Port = 8770,
    [string] $BindAddress = "0.0.0.0",
    [string] $InstallDir = "$env:ProgramData\WebRunner\app",
    [string] $DataDir = "$env:ProgramData\WebRunner",
    [switch] $OpenFirewall,
    [switch] $Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "WebRunner"

function Write-Step { param([string] $Message) Write-Host "  * $Message" -ForegroundColor Cyan }
function Write-Warn { param([string] $Message) Write-Host "  ! $Message" -ForegroundColor Yellow }
function Write-Bad  { param([string] $Message) Write-Host "error: $Message" -ForegroundColor Red }

$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$IsAdmin   = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# ------------------------------------------------------------- uninstall

if ($Uninstall) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Step "removed the '$TaskName' scheduled task"
    } catch {
        Write-Warn "no scheduled task named '$TaskName' was registered"
    }
    Get-Process -Name python, pythonw -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$InstallDir*" } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    if (Test-Path $InstallDir) {
        Remove-Item -Recurse -Force $InstallDir
        Write-Step "removed $InstallDir"
    }
    if ($OpenFirewall) {
        Remove-NetFirewallRule -DisplayName "WebRunner" -ErrorAction SilentlyContinue
    }
    Write-Host ""
    Write-Host "WebRunner has been uninstalled. Your data is still in $DataDir" -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host "Installing WebRunner" -ForegroundColor White
Write-Host ""

if (-not $IsAdmin) {
    Write-Warn "not running as Administrator"
    Write-Warn "WebRunner will start when you log in rather than at boot."
    Write-Warn "For a true boot service, re-run this from an elevated prompt."
    Write-Host ""
}

# ---------------------------------------------------------------- python

$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $found) { continue }
    try {
        $exe = $found.Source
        if ($candidate -eq "py") { $exe = (& py -3 -c "import sys; print(sys.executable)" 2>$null) }
        if (-not $exe) { continue }
        $version = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($version -and [version]$version -ge [version]"3.10") {
            $python = $exe
            break
        }
    } catch { continue }
}

if (-not $python) {
    Write-Bad "Python 3.10 or newer was not found on PATH."
    Write-Host "  Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')"
    Write-Host "  or run:  winget install Python.Python.3.12"
    exit 1
}
Write-Step "using $(& $python -V) at $python"

# --------------------------------------------------------------- install

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir -Parent) | Out-Null

$venvPython = Join-Path $InstallDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    & $python -m venv $InstallDir
    if ($LASTEXITCODE -ne 0) { Write-Bad "could not create the virtualenv"; exit 1 }
    Write-Step "created virtualenv at $InstallDir"
}

& $venvPython -m pip install --quiet --upgrade pip wheel
if ($LASTEXITCODE -ne 0) { Write-Bad "pip could not be upgraded"; exit 1 }

$source = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $source "pyproject.toml")) {
    Write-Step "installing from $source"
    & $venvPython -m pip install --quiet --upgrade $source
} else {
    Write-Step "installing from PyPI"
    & $venvPython -m pip install --quiet --upgrade webrunner
}
if ($LASTEXITCODE -ne 0) { Write-Bad "installation failed"; exit 1 }

$version = & $venvPython -c "import webrunner; print(webrunner.__version__)"
Write-Step "WebRunner $version installed"

$scope = "User"
if ($IsAdmin) { $scope = "Machine" }
[Environment]::SetEnvironmentVariable("WEBRUNNER_DATA_DIR", $DataDir, $scope)
Write-Step "set WEBRUNNER_DATA_DIR ($scope scope)"

# ------------------------------------------------------------------ task

$arguments = "-m webrunner serve --host $BindAddress --port $Port"
$action = New-ScheduledTaskAction -Execute $venvPython -Argument $arguments -WorkingDirectory $DataDir

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew

if ($IsAdmin) {
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principalObj = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $when = "at boot, as SYSTEM"
} else {
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
    $principalObj = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    $when = "when $($identity.Name) logs in"
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principalObj -Force `
    -Description "WebRunner scheduled job runner" | Out-Null
Write-Step "registered the '$TaskName' task ($when)"

if ($OpenFirewall) {
    if (-not $IsAdmin) {
        Write-Warn "a firewall rule needs Administrator - skipped"
    } else {
        Remove-NetFirewallRule -DisplayName "WebRunner" -ErrorAction SilentlyContinue
        New-NetFirewallRule -DisplayName "WebRunner" -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
        Write-Step "opened TCP $Port on private networks"
    }
}

Start-ScheduledTask -TaskName $TaskName
Write-Step "started WebRunner"

# ------------------------------------------------------------------ wait

$url = "http://localhost:$Port"
$ready = $false
foreach ($attempt in 1..20) {
    Start-Sleep -Milliseconds 700
    try {
        $response = Invoke-WebRequest -Uri "$url/healthz" -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
}

Write-Host ""
if ($ready) {
    Write-Host "WebRunner is running and starts automatically $when." -ForegroundColor Green
} else {
    Write-Warn "WebRunner did not answer on $url yet - check the task history in Task Scheduler."
}

Write-Host ""
Write-Host "  dashboard   $url"
Write-Host "  data        $DataDir"
Write-Host "  stop        Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  start       Start-ScheduledTask -TaskName $TaskName"
Write-Host "  uninstall   .\install.ps1 -Uninstall"
Write-Host ""

$passwordFile = Join-Path $DataDir "initial-password.txt"

# The file only appears on a genuinely first run: WebRunner generates a
# password when the user table is empty. Give it a moment in case the service
# is still starting, then say plainly what to do if it never shows up.
foreach ($attempt in 1..10) {
    if (Test-Path $passwordFile) { break }
    Start-Sleep -Milliseconds 500
}

if (Test-Path $passwordFile) {
    Write-Host "First-run login" -ForegroundColor White
    Get-Content $passwordFile | ForEach-Object { Write-Host "    $_" }
    Write-Host ""
    Write-Host "  Change it in Settings, then delete $passwordFile"
    Write-Host ""
} else {
    $webrunnerExe = Join-Path $InstallDir "Scripts\webrunner.exe"
    Write-Host "No first-run password file at" -ForegroundColor White
    Write-Host "    $passwordFile"
    Write-Host ""
    Write-Host "  That is expected when an account already exists in this data"
    Write-Host "  directory, or when WEBRUNNER_ADMIN_PASSWORD was set. Passwords are"
    Write-Host "  stored hashed, so an existing one cannot be recovered - set a new"
    Write-Host "  one instead:"
    Write-Host ""
    Write-Host "      & '$webrunnerExe' passwd admin" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  (run it in the same account the service uses; for a SYSTEM task"
    Write-Host "   an elevated prompt is enough, the database is the same file)"
    Write-Host ""
}

if ($BindAddress -eq "0.0.0.0") {
    Write-Warn "WebRunner runs commands on this machine and is listening on all interfaces."
    Write-Warn "Keep it behind a firewall or bind to 127.0.0.1 with -BindAddress 127.0.0.1"
}
