param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$taskName = 'MarieSpace Obsidian Favorites Worker'
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root 'run_obsidian_worker.py'
$config = Join-Path $PSScriptRoot 'phase6.5-worker.env'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed $taskName"
    exit 0
}

if (-not (Test-Path -LiteralPath $runner)) { throw 'Phase 6.5 worker runner is missing.' }
if (-not (Test-Path -LiteralPath $config)) { throw 'Private Phase 6.5 worker configuration is missing.' }

$action = New-ScheduledTaskAction -Execute 'py.exe' -Argument "-3 `"$runner`" --env-file `"$config`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 0) -MultipleInstances IgnoreNew
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Description 'MarieSpace Phase 6.5. Pulls approved owner favorites to Obsidian every 45 seconds; no public inbound service.'
Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
Write-Output "Installed $taskName. It starts when this Windows user signs in."
