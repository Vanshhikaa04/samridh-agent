# One-time setup: run the agent automatically whenever you log in to Windows.
# Undo any time with:  Unregister-ScheduledTask -TaskName JobAgent -Confirm:$false
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root "start_agent.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "JobAgent" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Starts the job application agent and its dashboard at logon" -Force | Out-Null
Get-ScheduledTask -TaskName "JobAgent" | Select-Object TaskName, State | Format-Table -AutoSize | Out-String
