# Bring the whole thing up after a logon / reboot: browser first, then the bot.
#
# Order matters. On the cookie channel the bot reads Canvas cookies out of the
# running browser at startup, and every WebAssign operation attaches to it, so
# a bot started before the browser comes up degraded. Nothing here shows a
# window, asks for a password, or prints a secret.
#
# Installed by:   scripts\autostart_install.ps1   (a shortcut in the Startup folder)
# Log:            ~\.ainl\autostart.log

$repo = Split-Path -Parent $PSScriptRoot
$ainl = Join-Path $env:USERPROFILE '.ainl'
New-Item -ItemType Directory -Force $ainl | Out-Null
$log = Join-Path $ainl 'autostart.log'
function Say($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Out-File -FilePath $log -Append -Encoding utf8 }

Say "autostart begin"

# The network is often not up yet right after logon; the bot's first call is
# to Canvas and it exits if that fails. Wait for it, but not forever.
$online = $false
foreach ($i in 1..30) {
    try { $null = [System.Net.Dns]::GetHostEntry('canvas.wisc.edu'); $online = $true; break }
    catch { Start-Sleep -Seconds 4 }
}
Say "network: $online"

# 1. the headless browser (no-op if one is already listening on 9222)
& python (Join-Path $repo 'scripts\browser_start.py') 2>&1 | ForEach-Object { Say "browser: $_" }
foreach ($i in 1..15) {
    try { $null = Invoke-RestMethod 'http://127.0.0.1:9222/json/version' -TimeoutSec 2; break }
    catch { Start-Sleep -Seconds 1 }
}

# 2. the bot (stops any old instance first — one getUpdates consumer per token)
#
# The bot exits if its very first Canvas call fails (a network that is "up" but
# not yet routing), and nothing else would start it again until the next logon.
# So: start it, look again after 20 s, and try up to three times.
foreach ($try in 1..3) {
    & (Join-Path $repo 'scripts\bot_restart.ps1') 2>&1 | ForEach-Object { Say "bot[$try]: $_" }
    Start-Sleep -Seconds 20
    $alive = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
               Where-Object { $_.CommandLine -like '*-m ainl.bot*' }).Count
    Say "bot[$try] alive after 20s: $alive"
    if ($alive -ge 1) { break }
    Start-Sleep -Seconds 30
}
Say "autostart end"
