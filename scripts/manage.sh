#!/usr/bin/env bash
# Trading bot + dashboard process manager
# Usage: manage.sh [start|stop|restart|status] [bot|ui|all]

set -euo pipefail

REPO=/home/ivan8115/git/tradingBot
VENV=$REPO/.venv
PID_DIR=/tmp/tradingbot
LOG_DIR=$REPO/logs

mkdir -p "$PID_DIR" "$LOG_DIR"

BOT_PID=$PID_DIR/bot.pid
UI_PID=$PID_DIR/ui.pid
BOT_LOG=$LOG_DIR/tradingbot_$(date +%Y-%m-%d).log
UI_LOG=$LOG_DIR/dashboard.log

_is_running() {
    local pidfile=$1
    [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

_start_bot() {
    if _is_running "$BOT_PID"; then
        echo "[bot] already running (pid $(cat "$BOT_PID"))"
        return
    fi
    nohup "$VENV/bin/python" "$REPO/main.py" trade --mode paper \
        >> "$BOT_LOG" 2>&1 &
    echo $! > "$BOT_PID"
    disown $!
    echo "[bot] started (pid $!)"
}

_start_ui() {
    if _is_running "$UI_PID"; then
        echo "[ui]  already running (pid $(cat "$UI_PID"))"
        return
    fi
    nohup "$VENV/bin/python" -m dashboard.app \
        >> "$UI_LOG" 2>&1 &
    echo $! > "$UI_PID"
    disown $!
    echo "[ui]  started (pid $!)"
}

_stop_bot() {
    if _is_running "$BOT_PID"; then
        kill "$(cat "$BOT_PID")" && rm -f "$BOT_PID"
        echo "[bot] stopped"
    else
        echo "[bot] not running"
        rm -f "$BOT_PID"
    fi
}

_stop_ui() {
    if _is_running "$UI_PID"; then
        kill "$(cat "$UI_PID")" && rm -f "$UI_PID"
        echo "[ui]  stopped"
    else
        echo "[ui]  not running"
        rm -f "$UI_PID"
    fi
}

_status() {
    if _is_running "$BOT_PID"; then
        echo "[bot] running  (pid $(cat "$BOT_PID"))"
    else
        echo "[bot] stopped"
    fi
    if _is_running "$UI_PID"; then
        echo "[ui]  running  (pid $(cat "$UI_PID"))"
    else
        echo "[ui]  stopped"
    fi
}

CMD=${1:-status}
TARGET=${2:-all}

case "$CMD" in
    start)
        [[ "$TARGET" == "bot" || "$TARGET" == "all" ]] && _start_bot
        [[ "$TARGET" == "ui"  || "$TARGET" == "all" ]] && _start_ui
        ;;
    stop)
        [[ "$TARGET" == "bot" || "$TARGET" == "all" ]] && _stop_bot
        [[ "$TARGET" == "ui"  || "$TARGET" == "all" ]] && _stop_ui
        ;;
    restart)
        [[ "$TARGET" == "bot" || "$TARGET" == "all" ]] && { _stop_bot; sleep 1; _start_bot; }
        [[ "$TARGET" == "ui"  || "$TARGET" == "all" ]] && { _stop_ui;  sleep 1; _start_ui;  }
        ;;
    status)
        _status
        ;;
    *)
        echo "Usage: $0 [start|stop|restart|status] [bot|ui|all]"
        exit 1
        ;;
esac
