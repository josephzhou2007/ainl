#!/bin/zsh
# The macOS counterpart of scripts/*.ps1: install the pieces as launchd user
# agents (start at login, restart on failure), and restart / stop / inspect them.
#
#   scripts/macos/service.sh install [browser bot public-bot]   default: browser bot
#   scripts/macos/service.sh remove  [targets]
#   scripts/macos/service.sh restart bot        # like bot_restart.ps1 — never touches the browser
#   scripts/macos/service.sh restart browser    # like browser_start.py --restart
#   scripts/macos/service.sh stop|start <target>
#   scripts/macos/service.sh status
#   scripts/macos/service.sh logs bot           # tail the service's stderr (the real log)
#
# Only ONE getUpdates consumer may exist per bot token, and launchd guarantees
# one instance per label — so "restart" here is safe by construction, where
# bot_restart.ps1 had to hunt the old process by command line.
# Secrets live in ~/.ainl/env (chmod 600), never in the plist. Nothing here
# prints one.
set -eu
cmd="${1:-status}"; shift || true
repo="$(cd "$(dirname "${(%):-%x}")/../.." && pwd)"
ainl="${AINL_HOME:-$HOME/.ainl}"
agents="$HOME/Library/LaunchAgents"
domain="gui/$(id -u)"
all=(browser bot public-bot)
targets=("$@")

label() { echo "com.ainl.$1"; }
plist() { echo "$agents/$(label "$1").plist"; }
logname() { [[ "$1" == "public-bot" ]] && echo public_bot || echo "$1"; }

need_targets() { (( ${#targets} )) || targets=("${all[@]}"); }

check_target() {
    case "$1" in browser|bot|public-bot) ;; *) echo "unknown target: $1" >&2; exit 2 ;; esac
}

render() {
    sed -e "s|__TARGET__|$1|g" -e "s|__REPO__|$repo|g" -e "s|__HOME__|$HOME|g" \
        -e "s|__LOG__|$(logname "$1")|g" "$repo/scripts/macos/com.ainl.template.plist"
}

loaded() { launchctl print "$domain/$(label "$1")" >/dev/null 2>&1; }

do_install() {
    (( ${#targets} )) || targets=(browser bot)
    mkdir -p "$ainl" "$agents"
    if [[ ! -f "$ainl/env" ]]; then
        cp "$repo/scripts/macos/env.example" "$ainl/env"; chmod 600 "$ainl/env"
        echo "created $ainl/env — fill in the tokens, then: service.sh restart bot"
    fi
    [[ -x "$repo/.venv/bin/python" ]] || echo "warning: no $repo/.venv — see docs/macos-hosting.md" >&2
    for t in "${targets[@]}"; do
        check_target "$t"
        render "$t" > "$(plist "$t")"
        loaded "$t" && launchctl bootout "$domain/$(label "$t")" 2>/dev/null || true
        launchctl bootstrap "$domain" "$(plist "$t")"
        launchctl enable "$domain/$(label "$t")"
        echo "installed $(label "$t")  ->  $(plist "$t")"
    done
}

do_remove() {
    need_targets
    for t in "${targets[@]}"; do
        check_target "$t"
        loaded "$t" && launchctl bootout "$domain/$(label "$t")" 2>/dev/null || true
        rm -f "$(plist "$t")"
        echo "removed $(label "$t")"
    done
}

do_restart() {
    need_targets
    for t in "${targets[@]}"; do
        check_target "$t"
        if loaded "$t"; then
            launchctl kickstart -k "$domain/$(label "$t")"; echo "restarted $(label "$t")"
        else
            echo "$(label "$t") is not installed — run: service.sh install $t" >&2; exit 1
        fi
    done
}

do_stop()  { need_targets; for t in "${targets[@]}"; do check_target "$t"; launchctl kill SIGTERM "$domain/$(label "$t")" 2>/dev/null && echo "stopped $(label "$t")" || echo "$(label "$t") not running"; done; }
do_start() { need_targets; for t in "${targets[@]}"; do check_target "$t"; launchctl kickstart "$domain/$(label "$t")" && echo "started $(label "$t")"; done; }

do_status() {
    local pid
    for t in "${all[@]}"; do
        if loaded "$t"; then
            pid="$(launchctl print "$domain/$(label "$t")" 2>/dev/null | awk '/^\tpid = /{print $3; exit}')"
            if [[ -n "$pid" ]]; then printf "%-12s installed  running pid %s\n" "$t" "$pid"
            else printf "%-12s installed  not running\n" "$t"; fi
        else
            printf "%-12s not installed\n" "$t"
        fi
    done
    curl -s -m 2 http://127.0.0.1:9222/json/version >/dev/null && echo "browser answering on 9222" || echo "nothing on 9222"
}

do_logs() { local t="${targets[1]:-bot}"; check_target "$t"; tail -n 50 -F "$ainl/$(logname "$t").log.err"; }

case "$cmd" in
    install) do_install ;;
    remove)  do_remove ;;
    restart) do_restart ;;
    stop)    do_stop ;;
    start)   do_start ;;
    status)  do_status ;;
    logs)    do_logs ;;
    *) sed -n '2,15p' "${(%):-%x}"; exit 2 ;;
esac
