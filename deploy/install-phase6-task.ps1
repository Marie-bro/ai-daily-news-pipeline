param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$taskName = 'MarieSpace Tech Daily Daily'
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root 'deploy\run-phase6-task.cmd'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed $taskName"
    exit 0
}

if (-not (Test-Path -LiteralPath $runner)) { throw 'Phase 6 runner is missing.' }
$action = New-ScheduledTaskAction -Execute $env:ComSpec -Argument "/d /c `"`"$runner`"`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At 08:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Description 'MarieSpace Tech Daily. Runs daily at 08:00 Asia/Shanghai; sends only after formal URL verification.'
Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
Write-Output "Installed $taskName for 08:00 Asia/Shanghai"
