# Starts the job agent (daemon) and the dashboard (serve) in the background, unless they are already running.
# Registered as the Windows scheduled task "JobAgent" so it comes back after every restart/logon.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
foreach ($cmd in 'serve', 'daemon') {
    $running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match "jobagent $cmd" }
    if (-not $running) {
        Start-Process -FilePath $py -ArgumentList '-W', 'ignore', '-m', 'jobagent', $cmd -WindowStyle Hidden -WorkingDirectory $root
    }
}
