# Make autostart.ps1 run at every logon — or undo that with -Remove.
#
# A shortcut in the user's own Startup folder: no admin rights, no scheduled
# task, no registry edit, and it is visible and deletable in Explorer
# (Win+R -> shell:startup). It runs at LOGON, not at boot: the secrets live in
# the *User* environment and the browser profile belongs to this user, so
# there is nothing useful to start before someone is signed in.
param([switch]$Remove)

$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'ainl-autostart.lnk'
if ($Remove) {
    if (Test-Path $lnk) { Remove-Item $lnk -Confirm:$false; "removed $lnk" } else { "nothing to remove" }
    return
}
$script = Join-Path $PSScriptRoot 'autostart.ps1'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = (Get-Command powershell.exe).Source
$s.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$s.WorkingDirectory = Split-Path -Parent $PSScriptRoot
$s.WindowStyle = 7            # minimised: no console flashing over the desktop
$s.Description = 'ainl: headless browser + Telegram bot'
$s.Save()
"installed $lnk"
