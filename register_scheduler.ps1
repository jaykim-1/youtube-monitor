# Register YouTube Monitor worker in Windows Task Scheduler.
# Usage:
#   1) Open PowerShell as Administrator
#   2) cd to this directory
#   3) .\register_scheduler.ps1
#
# Default: run worker.py every 1 hour.

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$WorkerPath = Join-Path $ProjectDir "worker.py"

if (Test-Path $VenvPython) {
    $PythonExe = $VenvPython
} else {
    $PythonExe = (Get-Command python).Source
}

if (-not (Test-Path $WorkerPath)) {
    Write-Error "worker.py not found at $WorkerPath"
    exit 1
}

$TaskName = "YouTubeMonitor_Worker"

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$WorkerPath`"" -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)

$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "YouTube Monitor Worker (hourly)" -User $env:USERNAME

Write-Host "Task registered: $TaskName" -ForegroundColor Green
Write-Host "Verify in Task Scheduler (taskschd.msc)" -ForegroundColor Yellow
Write-Host "Run manually: Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Yellow
