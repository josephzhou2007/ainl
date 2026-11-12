# Restart the PUBLIC bot — registration for classmates. A separate bot under
# its own BotFather token, a separate process; the owner's private homework
# bot (bot_restart.ps1) is not touched. Never touches the browser.
#
# Needs TELEGRAM_PUBLIC_BOT_TOKEN in the Windows *User* environment: a SECOND
# bot from @BotFather, never the private bot's token (one getUpdates consumer
# per token). Secrets are read from the environment and never printed.

$old = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
       Where-Object { $_.CommandLine -like '*scale-up\bot.py*' }
foreach ($p in $old) { Stop-Process -Id $p.ProcessId -Force -Confirm:$false }
if ($old) { Start-Sleep -Seconds 2 }

foreach ($n in 'TELEGRAM_PUBLIC_BOT_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'AINL_HOME') {
    $v = [Environment]::GetEnvironmentVariable($n, 'User')
    if ($v) { Set-Item -Path "Env:$n" -Value $v }
}
if (-not $env:TELEGRAM_PUBLIC_BOT_TOKEN) {
    "TELEGRAM_PUBLIC_BOT_TOKEN is not set. Create a second bot with @BotFather, then:"
    '    setx TELEGRAM_PUBLIC_BOT_TOKEN "<its token>"'
    "and run this again from a NEW terminal."
    exit 2
}

$ainl = Join-Path $env:USERPROFILE '.ainl'
New-Item -ItemType Directory -Force $ainl | Out-Null
$repo = Split-Path -Parent $PSScriptRoot
Start-Process -FilePath python -ArgumentList (Join-Path $repo 'scale-up\bot.py') -WorkingDirectory $repo `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $ainl 'public_bot.log') `
    -RedirectStandardError  (Join-Path $ainl 'public_bot.log.err')

Start-Sleep -Seconds 6
$now = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
         Where-Object { $_.CommandLine -like '*scale-up\bot.py*' })
"public bot processes: $($now.Count)"
Get-Content (Join-Path $ainl 'public_bot.log.err') -Tail 2
