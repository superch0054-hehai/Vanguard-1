#!/usr/bin/env bash
# 采集服务的启停脚本
#
# ★ 设计原则：只动本目录（~/batterylab）里的东西。
#   不用 sudo、不装系统包、不写 systemd、不碰其他目录。
#   想彻底清理就 rm -rf 整个 ~/batterylab。
#
# 用法：
#   ./service.sh start     后台启动
#   ./service.sh stop      停止
#   ./service.sh status    看状态
#   ./service.sh log       跟着看日志（Ctrl+C 退出）
#   ./service.sh tail      看最后 40 行日志
#
# 可通过环境变量覆盖：
#   BTS_HOST=10.201.47.169 CHANNELS=27-188-10-1,27-188-10-2 ./service.sh start

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/collector.pid"
LOGFILE="$DIR/logs/collector.log"
DATADIR="$DIR/data"

# ---- 可配置项 ----
HOST="${BTS_HOST:-10.201.47.169}"
CHANNELS="${CHANNELS:-27-188-10-1,27-188-10-2,27-188-10-3}"
INTERVAL="${POLL_INTERVAL:-30}"

PY="${PY:-python3}"

is_running() {
    [ -f "$PIDFILE" ] || return 1
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

start() {
    if is_running; then
        echo "已经在跑了（PID $(cat "$PIDFILE")）"
        return 0
    fi
    mkdir -p "$DIR/logs" "$DATADIR"

    echo "启动采集服务…"
    echo "  测试机    : $HOST:502"
    echo "  盯的通道  : $CHANNELS"
    echo "  轮询间隔  : ${INTERVAL}s"
    echo "  数据目录  : $DATADIR"
    echo "  日志      : $LOGFILE"

    # setsid + nohup：完全脱离终端，SSH 断开也不受影响
    cd "$DIR"
    setsid nohup "$PY" collector.py \
        --host "$HOST" \
        --channels "$CHANNELS" \
        --data-dir "$DATADIR" \
        --interval "$INTERVAL" \
        --backfill \
        > "$LOGFILE" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"

    sleep 4
    if is_running; then
        echo "✅ 已启动，PID $(cat "$PIDFILE")"
        echo
        echo "--- 启动日志 ---"
        tail -n 15 "$LOGFILE"
    else
        echo "❌ 启动失败，日志如下："
        tail -n 25 "$LOGFILE"
        rm -f "$PIDFILE"
        return 1
    fi
}

stop() {
    if ! is_running; then
        echo "没有在跑"
        rm -f "$PIDFILE"
        return 0
    fi
    local pid
    pid="$(cat "$PIDFILE")"
    echo "停止 PID $pid …"
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "还没退出，强制杀"
        kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$PIDFILE"
    echo "✅ 已停止"
}

status() {
    if is_running; then
        local pid
        pid="$(cat "$PIDFILE")"
        echo "✅ 运行中，PID $pid"
        echo
        echo "--- 进程信息 ---"
        ps -o pid,etime,rss,cmd -p "$pid" 2>/dev/null | sed 's/^/  /'
        echo
        echo "--- 心跳（$DATADIR/status.json）---"
        if [ -f "$DATADIR/status.json" ]; then
            cat "$DATADIR/status.json" | sed 's/^/  /'
        else
            echo "  （还没有心跳文件）"
        fi
        echo
        echo "--- 已采数据集 ---"
        if [ -f "$DATADIR/manifest.jsonl" ]; then
            local n
            n="$(wc -l < "$DATADIR/manifest.jsonl")"
            echo "  共 $n 份"
            tail -n 3 "$DATADIR/manifest.jsonl" \
                | "$PY" -c 'import sys,json
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    r=json.loads(line)
    print(f"    {r[\"channel\"]}  测试{r[\"testid\"]}  条码 {r[\"barcode\"] or \"（空）\"}  "
          f"{r[\"detail_count\"]} 条  {r[\"collected_at\"]}")' 2>/dev/null
        else
            echo "  （还没有采集记录）"
        fi
    else
        echo "❌ 没有在跑"
        [ -f "$PIDFILE" ] && echo "   （PID 文件还在，可能是异常退出）"
    fi
}

case "${1:-}" in
    start)  start ;;
    stop)   stop ;;
    restart) stop; sleep 2; start ;;
    status) status ;;
    log)    tail -f "$LOGFILE" ;;
    tail)   tail -n 40 "$LOGFILE" ;;
    *)
        echo "用法：$0 {start|stop|restart|status|log|tail}"
        echo
        echo "只动本目录（$DIR），不用 sudo、不碰系统"
        exit 2
        ;;
esac
