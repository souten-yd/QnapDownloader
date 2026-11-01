#!/bin/sh
set -eu
name=QnapDownloader
config=/etc/config/qpkg.conf
if [ -x /sbin/getcfg ] && [ -f "$config" ]; then
    root=$(/sbin/getcfg "$name" Install_Path -d "" -f "$config")
else
    root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
fi
[ -n "$root" ] || { echo 'QPKG installation path missing' >&2; exit 1; }
pidfile="$root/downloader.pid"
# Persistent sibling of the QPKG installation, NOT a directory replaced
# when QDK updates the package. Requires no Container Station/Docker share.
# Never auto-switch data directories based on shares appearing at boot.
data_dir=${DOWNLOADER_DATA_DIR:-"$(dirname "$root")/QnapDownloader-data"}
logfile="$data_dir/logs/downloader.log"

resolve_python() {
    /bin/sh "$root/python3-path"
}
check_owned() {
    "$python" -B "$root/downloader_pid.py" check "$root" "$data_dir" "$pidfile"
}

case "${1:-}" in
    start)
        if [ -x /sbin/getcfg ] && [ -f "$config" ]; then
            enabled=$(/sbin/getcfg "$name" Enable -u -d FALSE -f "$config")
            [ "$enabled" = TRUE ] || { echo "$name is disabled" >&2; exit 1; }
        fi
        umask 077
        # QTS may start QPKGs before their volume and Python3 are ready.
        # The default data dir shares the QPKG volume but is outside the
        # replaceable installation root. Do not silently fall back on errors.
        startup_wait=${DOWNLOADER_STARTUP_WAIT_SECONDS:-90}
        case "$startup_wait" in *[!0-9]*|'') startup_wait=90;; esac
        [ "$startup_wait" -le 180 ] || startup_wait=180
        elapsed=0
        python=
        while :; do
            ready=yes
            [ -d "$(dirname "$data_dir")" ] || ready=no
            case "$data_dir" in
                /share/Container|/share/Container/*) [ -d /share/Container ] || ready=no ;;
            esac
            if [ "$ready" = yes ]; then
                mkdir -p "$data_dir/logs" "$data_dir/data" "$data_dir/config"
                if python=$(/bin/sh "$root/python3-path" 2>/dev/null); then
                    break
                fi
            fi
            if [ "$elapsed" -ge "$startup_wait" ]; then
                echo "QnapDownloader startup timed out after ${elapsed}s waiting for data volume and Python; check QTS volume, free space and Python3 QPKG" >&2
                exit 1
            fi
            sleep 2
            elapsed=$((elapsed + 2))
        done
        # Reconcile only verified old QPKG servers, including QTS deleted cwd.
        # Exit code 10 means the verified port is free for a new server.
        if "$python" -B "$root/downloader_pid.py" prepare "$root" "$data_dir" "$pidfile"; then
            exit 0
        else
            result=$?
            [ "$result" -eq 10 ] || exit 1
        fi
        rm -f "$root/admin-token"
        # -B avoids .pyc filesystem writes. A fixed cwd avoids QTS temp dirs.
        # Secure default: loopback-only. QTS HTTPS reverse proxy/VPN is preferred.
        # Explicit LAN HTTP exposure requires DOWNLOADER_LAN_ENABLED=1.
        if [ "${DOWNLOADER_LAN_ENABLED:-0}" = "1" ]; then
            (cd "$root" && exec "$python" -B "$root/webapp.py" --lan --port 17864 --data-dir "$data_dir") < /dev/null > "$logfile" 2>&1 &
        else
            (cd "$root" && exec "$python" -B "$root/webapp.py" --secure-cookie --port 17864 --data-dir "$data_dir") < /dev/null > "$logfile" 2>&1 &
        fi
        echo $! > "$pidfile"
        sleep 1
        check_owned || { rm -f "$pidfile"; echo "Web UI failed to start with verified PID; check $logfile" >&2; exit 1; }
        ;;
    stop)
        python=$(resolve_python) || exit 1
        "$python" -B "$root/downloader_pid.py" stop "$root" "$data_dir" "$pidfile"
        ;;
    status)
        python=$(resolve_python) || exit 1
        "$python" -B "$root/downloader_pid.py" status "$root" "$data_dir" "$pidfile"
        ;;
    restart)
        "$0" stop
        "$0" start
        ;;
    *) echo "Usage: $0 {start|stop|status|restart}" >&2; exit 2;;
esac
