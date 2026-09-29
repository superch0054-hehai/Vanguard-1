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
#   ./service.sh web       打开网页控制台（本机浏览器）
#
# ★ 一次 start 会同时起两个东西：
#     ① 采集服务（collector.py）—— 从测试机取数
#     ② 网页控制台（console.py）—— 给人看的界面
#   所以「起来之后就不用再敲任何命令了」—— 打开浏览器就行。
#
# 可通过环境变量覆盖：
#   BTS_HOST=10.201.47.169 CHANNELS=27-188-10-1 ./service.sh start
#   CONSOLE_HOST=127.0.0.1 CONSOLE_PORT=8090 ./service.sh start

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/collector.pid"
LOGFILE="$DIR/logs/collector.log"
DATADIR="$DIR/data"

CONSOLE_PIDFILE="$DIR/console.pid"
CONSOLE_LOG="$DIR/logs/console.log"
CONSOLE_PORT="${CONSOLE_PORT:-8090}"
# 默认绑 0.0.0.0 —— 否则同网段的电脑打不开，这个界面就没用了。
# ⚠️ 它**没有登录**，同网段的人都能看（只读，不会操作设备）。
#    要收紧就 CONSOLE_HOST=127.0.0.1 ./service.sh restart
CONSOLE_HOST="${CONSOLE_HOST:-0.0.0.0}"

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

console_running() {
    [ -f "$CONSOLE_PIDFILE" ] || return 1
    local pid
    pid="$(cat "$CONSOLE_PIDFILE" 2>/dev/null)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

start_console() {
    if console_running; then
        echo "网页控制台已经在跑了（PID $(cat "$CONSOLE_PIDFILE")）"
        return 0
    fi
    if [ ! -f "$DIR/console.py" ]; then
        echo "（没有 console.py，跳过控制台）"
        return 0
    fi
    echo "启动网页控制台…"
    echo "  访问地址  : http://$(hostname -I 2>/dev/null | awk '{print $1}'):$CONSOLE_PORT"
    cd "$DIR"
    setsid nohup "$PY" console.py \
        --data-dir "$DATADIR" \
        --host "$CONSOLE_HOST" \
        --port "$CONSOLE_PORT" \
        --share-dir "${SHARE_DIR:-$DIR/../batterylab-data}" \
        > "$CONSOLE_LOG" 2>&1 < /dev/null &
    echo $! > "$CONSOLE_PIDFILE"
    sleep 3
    if console_running; then
        echo "  ✅ 控制台已启动，PID $(cat "$CONSOLE_PIDFILE")"
    else
        echo "  ⚠️ 控制台没起来，看日志：$CONSOLE_LOG"
        tail -n 10 "$CONSOLE_LOG" 2>/dev/null | sed 's/^/     /'
        rm -f "$CONSOLE_PIDFILE"
    fi
}

stop_console() {
    if ! console_running; then
        rm -f "$CONSOLE_PIDFILE"
        return 0
    fi
    local pid
    pid="$(cat "$CONSOLE_PIDFILE")"
    echo "停止控制台 PID $pid …"
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 8); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    kill -9 "$pid" 2>/dev/null
    rm -f "$CONSOLE_PIDFILE"
}

start() {
    mkdir -p "$DIR/logs" "$DATADIR"

    # ⚠️ 这里**不能**因为采集器在跑就整个 return —— 否则控制台永远起不来。
    #    采集器是常驻的（演示当天它一直在跑），而控制台会被人关掉/盒子重启后消失，
    #    于是"双击桌面图标"这条最容易走的路会静默失败：什么都不报，网页就是打不开。
    if is_running; then
        echo "采集服务已经在跑了（PID $(cat "$PIDFILE")），不动它"
    else
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
            echo "✅ 采集服务已启动，PID $(cat "$PIDFILE")"
            echo
            echo "--- 启动日志 ---"
            tail -n 15 "$LOGFILE"
        else
            echo "❌ 采集服务启动失败，日志如下："
            tail -n 25 "$LOGFILE"
            rm -f "$PIDFILE"
            return 1
        fi
    fi
    echo
    start_console
}

stop() {
    # 同理不能提前 return：采集器可能已经没了，但控制台还活着。
    # 那样 stop 会"成功"却留下一个孤儿进程继续占着端口。
    if ! is_running; then
        echo "采集服务没有在跑"
        rm -f "$PIDFILE"
    else
        local pid
        pid="$(cat "$PIDFILE")"
        echo "停止采集服务 PID $pid …"
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
    fi
    stop_console
    echo "✅ 已停止（采集 + 控制台）"
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
        echo "❌ 采集服务没有在跑"
        [ -f "$PIDFILE" ] && echo "   （PID 文件还在，可能是异常退出）"
    fi
    echo
    echo "--- 网页控制台 ---"
    if console_running; then
        echo "✅ 运行中，PID $(cat "$CONSOLE_PIDFILE")"
        echo "   本机    http://127.0.0.1:$CONSOLE_PORT"
        echo "   同网段  http://$(hostname -I 2>/dev/null | awk '{print $1}'):$CONSOLE_PORT"
    else
        echo "❌ 没有在跑"
    fi
}

web() {
    local url="http://127.0.0.1:$CONSOLE_PORT"
    if ! console_running; then
        echo "控制台没在跑，先 ./service.sh start"
        return 1
    fi
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" >/dev/null 2>&1 &
        echo "已在浏览器打开 $url"
    else
        echo "没有 xdg-open。请手动打开：$url"
    fi
}

case "${1:-}" in
    start)  start ;;
    stop)   stop ;;
    restart) stop; sleep 2; start ;;
    status) status ;;
    log)    tail -f "$LOGFILE" ;;
    tail)   tail -n 40 "$LOGFILE" ;;
    web)    web ;;
    *)
        echo "用法：$0 {start|stop|restart|status|log|tail|web}"
        echo
        echo "只动本目录（$DIR），不用 sudo、不碰系统"
        exit 2
        ;;
esac
