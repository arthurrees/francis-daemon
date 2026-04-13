# install_autostart.ps1
#
# Registers Windows Scheduled Tasks so Daemon's processes start automatically
# at user logon. Pass -Role to declare which machine this is.
#
# Run once on each machine:
#   On the PC server:  powershell -File install_autostart.ps1 -Role pc
#   On the laptop:     powershell -File install_autostart.ps1 -Role laptop
#
# Tasks created:
#   -Role pc     -> Daemon-API, Daemon-EmbeddingWorker
#   -Role laptop -> Daemon-ScreenshotWorker, Daemon-Overlay
#
# To uninstall later:
#   Get-ScheduledTask -TaskName "Daemon-*" | Unregister-ScheduledTask -Confirm:$false
#
# To check status:
#   Get-ScheduledTask -TaskName "Daemon-*" | Format-Table TaskName,State,LastRunTime,LastTaskResult

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("pc", "laptop")]
    [string]$Role
)

$ErrorActionPreference = "Stop"

# ----- Capture env vars locally up front -----
# PowerShell's $env: provider can be flaky inside double-quoted strings
# (it greedily eats characters after the var name). Pulling them into
# plain $variables avoids every edge case.
$UserDomain   = $env:USERDOMAIN
$UserName     = $env:USERNAME
$ComputerName = $env:COMPUTERNAME
$LocalAppData = $env:LOCALAPPDATA

# For LOCAL accounts on a workgroup machine, the correct principal prefix
# is the computer name, NOT $env:USERDOMAIN (which returns "WORKGROUP" on
# non-domain-joined boxes — Task Scheduler rejects that with "No mapping
# between account names and security IDs").
$UserPrincipal = "$ComputerName\$UserName"

# ----- Locate paths -----
# $PSScriptRoot is the directory this script lives in. Tasks run from here,
# so the script has to be co-located with the python files (which it is —
# they're all in the same daemon/ folder).

$daemonDir = $PSScriptRoot

if (-not (Test-Path $daemonDir)) {
    Write-Host "ERROR: daemon directory not found at $daemonDir" -ForegroundColor Red
    exit 1
}

# Find pythonw.exe — try PATH first, fall back to substituting from python.exe
$pythonwCmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($pythonwCmd) {
    $pythonwExe = $pythonwCmd.Source
} else {
    $pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        Write-Host "ERROR: neither pythonw.exe nor python.exe is on PATH" -ForegroundColor Red
        exit 1
    }
    $pythonwExe = $pythonCmd.Source -replace 'python\.exe$', 'pythonw.exe'
    if (-not (Test-Path $pythonwExe)) {
        Write-Host "ERROR: derived pythonw path doesn't exist: $pythonwExe" -ForegroundColor Red
        exit 1
    }
}

Write-Host "daemon dir : $daemonDir"
Write-Host "pythonw    : $pythonwExe"
Write-Host "role       : $Role"
Write-Host ""

# ----- Helper: register one task -----

function Register-DaemonTask {
    param(
        [string]$TaskName,
        [string]$Script,
        [string]$Description
    )

    $action = New-ScheduledTaskAction `
        -Execute $pythonwExe `
        -Argument $Script `
        -WorkingDirectory $daemonDir

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserPrincipal

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 0)

    $principal = New-ScheduledTaskPrincipal `
        -UserId $UserPrincipal `
        -LogonType Interactive `
        -RunLevel Limited

    try {
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Principal $principal `
            -Description $Description `
            -Force | Out-Null
        Write-Host "  [OK] Registered: $TaskName" -ForegroundColor Green
    } catch {
        Write-Host "  [FAIL] $TaskName : $_" -ForegroundColor Red
    }
}

# ----- Register tasks for the declared role -----

if ($Role -eq "pc") {
    Write-Host "Registering PC tasks (API + embedding worker)." -ForegroundColor Cyan
    Write-Host ""

    Register-DaemonTask `
        -TaskName "Daemon-API" `
        -Script "api.py" `
        -Description "Daemon FastAPI backend (vault retrieval + Llama)"

    Register-DaemonTask `
        -TaskName "Daemon-EmbeddingWorker" `
        -Script "embedding_worker.py" `
        -Description "Daemon vault embedding worker (watches Obsidian vault)"

} else {
    Write-Host "Registering laptop tasks (screenshot worker + overlay)." -ForegroundColor Cyan
    Write-Host ""

    Register-DaemonTask `
        -TaskName "Daemon-ScreenshotWorker" `
        -Script "screenshot_worker.py" `
        -Description "Daemon screenshot + OCR worker (captures every 5 min)"

    Register-DaemonTask `
        -TaskName "Daemon-Overlay" `
        -Script "overlay.py" `
        -Description "Daemon Alt+Space overlay (Francis hotkey window)"
}

$logsDir = "$LocalAppData\daemon\logs"

Write-Host ""
Write-Host "Done. Tasks will run on next logon." -ForegroundColor Cyan
Write-Host "To start them now without logging out:"
Write-Host "  Get-ScheduledTask -TaskName 'Daemon-*' | Start-ScheduledTask"
Write-Host ""
Write-Host "Logs will appear in:" -ForegroundColor Cyan
Write-Host "  $logsDir"
