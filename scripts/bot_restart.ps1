# Restart the Telegram bot. Never touches the browser.
#
# Only ONE getUpdates consumer may exist per bot token, so the old instance is
# stopped first — selected by its exact command line, never by process name
# (stopping "python" or "node" by name has taken down unrelated tools before).
# Secrets are read from the Windows *User* environment and never printed.

$old = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
       Where-Object { $_.CommandLine -like '*-m ainl.bot*' }
foreach ($p in $old) { Stop-Process -Id $p.ProcessId -Force -Confirm:$false }
if ($old) { Start-Sleep -Seconds 2 }

foreach ($n in 'CANVAS_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'GEMINI_API_KEY',
               'CANVAS_BASE_URL', 'AINL_KEEPALIVE_MIN') {
    $v = [Environment]::GetEnvironmentVariable($n, 'User')
    if ($v) { Set-Item -Path "Env:$n" -Value $v }
}

$ainl = Join-Path $env:USERPROFILE '.ainl'
New-Item -ItemType Directory -Force $ainl | Out-Null
$repo = Split-Path -Parent $PSScriptRoot
# The owner's PRIVATE homework bot. The public one is a separate bot and a
# separate process: scripts\public_bot_restart.ps1.
Start-Process -FilePath python -ArgumentList '-m', 'ainl.bot' -WorkingDirectory $repo `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $ainl 'bot.log') `
    -RedirectStandardError  (Join-Path $ainl 'bot.log.err')

Start-Sleep -Seconds 6
$now = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
         Where-Object { $_.CommandLine -like '*-m ainl.bot*' })
"bot processes: $($now.Count)"
Get-Content (Join-Path $ainl 'bot.log.err') -Tail 2
