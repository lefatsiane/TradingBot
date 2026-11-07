[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$TaskName = "TradingBot-Demo",
    [string]$ProjectDirectory = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe")
)

$ErrorActionPreference = "Stop"
$project = (Resolve-Path -LiteralPath $ProjectDirectory).Path
$python = (Resolve-Path -LiteralPath $PythonPath).Path
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m bot.main run" `
    -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal `
    -UserId $user `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Starts the demo-only TradingBot runtime at user logon."

if ($PSCmdlet.ShouldProcess($TaskName, "Register Windows scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    Write-Output "Registered $TaskName for $user using $python in $project."
    Write-Warning "Verify Task Scheduler settings and demo-only runtime behavior before enabling unattended use."
}
