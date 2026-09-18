# =====================================================================
#  PowerUSB - install autostart
#
#  Run ONCE as Administrator:
#    right-click Start -> Terminal (Admin), then:
#    powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
#
#  Registers a Scheduled Task that starts the server AT BOOT (before anyone
#  logs in) and restarts it automatically if it stops, then VERIFIES that the
#  server is actually listening before claiming success.
#
#  No firewall rules are created: the server binds 127.0.0.1 only and is
#  reached through Tailscale Serve, so nothing needs to be opened. This script
#  never runs a tailscale command - your existing Funnel config is untouched.
#
#  Re-running it is safe.
# =====================================================================

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$TaskName = "PowerUSB Server"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}
$IsAdmin = Test-Admin

Write-Host ""
Write-Host "PowerUSB autostart installer" -ForegroundColor Cyan
Write-Host "  folder        : $Root"
Write-Host "  administrator : $IsAdmin"

# ------------------------------------------------------------------ python
#
# Do NOT trust Get-Command. On this machine python.exe / pythonw.exe / py.exe
# all resolve to C:\...\AppData\Local\Microsoft\WindowsApps\*.exe, which are
# 0-byte AppExecLink reparse points (Microsoft Store app-execution aliases).
# An interactive user can launch those; a Scheduled Task running as SYSTEM in
# session 0 CANNOT resolve them - the process dies instantly, nothing is
# written to server.log, and the strip is simply uncontrollable after a
# reboot. That is exactly how this failed the first time.
#
# The real interpreter is only discoverable via the PEP 514 registry keys.

function Test-RealPython {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    if ($Path -like '*\WindowsApps\*')       { return $false }
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $i = Get-Item -LiteralPath $Path -Force
    if ($i.Length -eq 0) { return $false }
    if ($i.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }

    # Positive probe: it must really be an interpreter AND have the one
    # dependency. A real .exe that cannot "import hid" is no use to us.
    $probe = Join-Path (Split-Path $Path) "python.exe"
    if (-not (Test-Path -LiteralPath $probe)) { $probe = $Path }
    & $probe -c "import hid" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

$candidates = @()
foreach ($hive in 'HKCU:\SOFTWARE\Python\PythonCore', 'HKLM:\SOFTWARE\Python\PythonCore') {
    Get-ChildItem $hive -ErrorAction SilentlyContinue | ForEach-Object {
        $ip = Get-ItemProperty (Join-Path $_.PSPath 'InstallPath') -ErrorAction SilentlyContinue
        if ($ip) {
            if ($ip.WindowedExecutablePath) { $candidates += $ip.WindowedExecutablePath }
            if ($ip.ExecutablePath)         { $candidates += $ip.ExecutablePath }
            $def = $ip.'(default)'
            if ($def) { $candidates += (Join-Path $def 'pythonw.exe') }
        }
    }
}
# Last-resort sweep of the standard per-user install roots, for the case where
# the PEP 514 registry keys are missing or unreadable.
foreach ($root in @("$env:LOCALAPPDATA\Python", "$env:LOCALAPPDATA\Programs\Python")) {
    if (Test-Path $root) {
        Get-ChildItem $root -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $candidates += (Join-Path $_.FullName 'pythonw.exe')
            $candidates += (Join-Path $_.FullName 'python.exe')
        }
    }
}

$PyW = $null
foreach ($c in ($candidates | Select-Object -Unique)) {
    if (Test-RealPython $c) { $PyW = $c; break }
}
if (-not $PyW) {
    throw ("No usable python found. Every candidate was either a Microsoft Store " +
           "app-execution alias (0-byte reparse point) or could not 'import hid'. " +
           "A Scheduled Task running as SYSTEM cannot resolve a Store alias. " +
           "Fix with: pip install hidapi   (or install Python from python.org)")
}
# Prefer the windowless launcher so no console flashes on screen.
$maybeW = Join-Path (Split-Path $PyW) "pythonw.exe"
if (Test-Path -LiteralPath $maybeW) { $PyW = $maybeW }
Write-Host "  python        : $PyW" -ForegroundColor Green

# ------------------------------------------------------------------ ports
$httpPort = 8765
$bindHost = "127.0.0.1"
$cfgPath = Join-Path $Root "config.json"
if (Test-Path $cfgPath) {
    try {
        # -Raw + ConvertFrom-Json copes with a BOM; a hand-edited config must
        # not stop us installing.
        $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
        if ($cfg.http_port) { $httpPort = [int]$cfg.http_port }
        if ($cfg.host)      { $bindHost = [string]$cfg.host }
    } catch { Write-Warning "config.json unreadable; assuming defaults" }
}
Write-Host "  http port     : $httpPort  (bound to $bindHost)"

# ------------------------------------------------- tailscale (check only)
$tsExe = "C:\Program Files\Tailscale\tailscale.exe"
if (Test-Path $tsExe) {
    $serve = (& $tsExe serve status 2>&1 | Out-String)
    if ($serve -match "9443") {
        Write-Host "  tailscale     : serve on 9443 is configured" -ForegroundColor Green
    } else {
        Write-Warning "Tailscale Serve for 9443 was not found. Re-add it with:"
        Write-Warning "    tailscale serve --bg --https=9443 http://127.0.0.1:8765"
    }
} else {
    Write-Warning "Tailscale not found - the app will only be reachable on this PC."
}
Write-Host ""

# ------------------------------------------------------------------ task
if (-not $IsAdmin) {
    throw "Not elevated. Re-run this in an Administrator terminal so the task can start at boot."
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute $PyW `
    -Argument "-m powerusb.server" -WorkingDirectory $Root

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT20S"      # give USB time to enumerate before we grab the strip

function Register-As {
    param([string]$Mode)
    if ($Mode -eq "SYSTEM") {
        $p = New-ScheduledTaskPrincipal -UserId "SYSTEM" `
             -LogonType ServiceAccount -RunLevel Highest
    } else {
        # S4U: runs as the real user at boot without storing a password, which
        # matters because the Python install lives in this user's profile.
        $p = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
             -LogonType S4U -RunLevel Highest
    }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $p `
        -Description "PowerUSB web GUI and TCP control server" | Out-Null
}

function Test-Listening {
    param([int]$Seconds = 25)
    for ($i = 0; $i -lt $Seconds; $i++) {
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$httpPort/api/health" `
                 -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 1
    }
    return $false
}

# Free the port so the task's own instance is the one that ends up serving.
Get-NetTCPConnection -LocalPort $httpPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 800

$ok = $false
foreach ($mode in @("SYSTEM", "USER")) {
    Write-Host "  trying to run the task as $mode ..."
    Register-As $mode
    Start-ScheduledTask -TaskName $TaskName
    if (Test-Listening) {
        Write-Host "  [ok] task runs as $mode and the server is listening" -ForegroundColor Green
        $ok = $true
        break
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Warning ("  as {0}: not listening. LastTaskResult = 0x{1:X8}" -f $mode, $info.LastTaskResult)
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

Write-Host ""
if (-not $ok) {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    $reg = (Get-ScheduledTask -TaskName $TaskName).Actions[0].Execute
    Write-Warning "The server did not come up under either account."
    Write-Warning ("  LastTaskResult  : 0x{0:X8}" -f $info.LastTaskResult)
    Write-Warning ("  Execute path    : {0}" -f $reg)
    Write-Warning "  0x80070002 / 0xC0000135 means the Execute path did not resolve."
    Write-Warning "  If server.log has no new lines, the process never started at all -"
    Write-Warning "  there is no point reading it."
    exit 1
}

$public = ""
if (Test-Path $cfgPath) {
    try {
        $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
        if ($cfg.public_url) { $public = [string]$cfg.public_url }
    } catch { }
}
Write-Host "  Open this on your phone / iPad (Tailscale must be on):" -ForegroundColor Cyan
if ($public) {
    Write-Host "      $public" -ForegroundColor White
} else {
    Write-Host "      Set 'public_url' in config.json to your Tailscale address," -ForegroundColor Yellow
    Write-Host "      e.g. https://your-pc.your-tailnet.ts.net:9443/" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  iPhone / iPad : Safari -> Share -> Add to Home Screen"
Write-Host "  Android       : Chrome -> menu -> Install app"
Write-Host ""
Write-Host "  Logs   : $(Join-Path $Root 'server.log')"
Write-Host "  Manage : Task Scheduler -> '$TaskName'"
Write-Host ""
