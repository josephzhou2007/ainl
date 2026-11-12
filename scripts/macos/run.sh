#!/bin/zsh
# What launchd actually runs. One target per service:
#
#   run.sh browser       the headless Chromium on 9222, in the foreground so
#                        launchd can supervise it (KeepAlive)
#   run.sh bot           the owner's PRIVATE homework bot   (python -m ainl.bot)
#   run.sh public-bot    the PUBLIC registration bot        (scale-up/bot.py)
#
# Secrets come from ~/.ainl/env (see env.example) and are never printed.
# Usable by hand too:  scripts/macos/run.sh bot   runs the bot in this terminal.
set -u
target="${1:-}"
repo="$(cd "$(dirname "${(%):-%x}")/../.." && pwd)"
export AINL_HOME="${AINL_HOME:-$HOME/.ainl}"
mkdir -p "$AINL_HOME"

envfile="$AINL_HOME/env"
if [[ -f "$envfile" ]]; then
    set -a; source "$envfile"; set +a
fi

py="$repo/.venv/bin/python"
[[ -x "$py" ]] || py="$(command -v python3)"
cd "$repo"

# The bot's first call is to Canvas, and it exits if that fails; right after a
# boot the network is often not up yet. Wait for it, but not forever (launchd
# would restart us anyway).
wait_for_network() {
    local host="${CANVAS_BASE_URL:-https://canvas.wisc.edu}"
    host="${host#*://}"; host="${host%%/*}"
    for i in {1..30}; do
        /usr/bin/nc -z -w 2 "$host" 443 >/dev/null 2>&1 && return 0
        sleep 4
    done
    echo "network: $host not reachable after 2 min, starting anyway" >&2
}

# On the cookie channel the bot reads Canvas cookies out of the running
# browser at startup, and every WebAssign operation attaches to it, so a bot
# started before the browser comes up degraded. Browser first, then bot.
wait_for_browser() {
    for i in {1..20}; do
        /usr/bin/curl -s -m 2 http://127.0.0.1:9222/json/version >/dev/null && return 0
        sleep 1
    done
    echo "browser: not answering on 9222 after 20 s, starting anyway" >&2
}

case "$target" in
    browser)
        exec "$py" scripts/browser_start.py --foreground ;;
    bot)
        wait_for_network; wait_for_browser
        exec "$py" -m ainl.bot ;;
    public-bot)
        wait_for_network
        exec "$py" scale-up/bot.py ;;
    *)
        echo "usage: run.sh browser|bot|public-bot" >&2; exit 2 ;;
esac
