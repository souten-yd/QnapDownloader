#!/bin/sh
# Xvfb + headful Chromium (CDP on 127.0.0.1:9222) + socat relay on 0.0.0.0:9223.
# 9223 is reachable only on the container's private network; nothing is published.
set -eu

# Exit 79: the process has effective capabilities. The container is created with
# `--cap-drop ALL --cap-add SYS_ADMIN` only so Docker's default seccomp profile
# lets Chromium create *unprivileged* user namespaces; as uid 1000 with
# no-new-privileges the effective set must stay empty. Refuse to run anything
# (including the selftest) otherwise, so Chromium never has real privileges.
if ! grep -q '^CapEff:[[:space:]]*0000000000000000$' /proc/self/status; then
  echo "process has effective capabilities" >&2
  exit 79
fi

# Exit 78: user namespaces unavailable. Chromium's sandbox needs them and is
# never disabled, so refuse to start (the Hub shows a clear message).
if ! unshare --user --map-root-user --pid --fork --net true >/dev/null 2>&1; then
  echo "user namespaces unavailable" >&2
  exit 78
fi
if [ "${1:-}" = "selftest" ]; then
  exit 0
fi

# Output goes to tmpfs files (no disk writes in normal operation). Only when a
# component dies is the tail of its log written to stderr, so the reason reaches
# `docker logs` (the Hub reads it once, then removes the container).
# Exit codes: 70 Xvfb socket timeout, 71 Xvfb died, 72 Chromium died, 73 socat died.
XVFB_LOG=/tmp/xvfb.log
CHROMIUM_LOG=/tmp/chromium.log
export HOME="${HOME:-/tmp}"
mkdir -p "$HOME" /tmp/.config /tmp/.cache /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix 2>/dev/null || true

XVFB=""
CHROMIUM=""
SOCAT=""

fail() {
  # fail <component> <logfile or ""> <exit code>
  echo "qnapdl-browser: $1 exited" >&2
  if [ -n "$2" ] && [ -f "$2" ]; then
    tail -n 60 "$2" >&2 || true
  fi
  kill $SOCAT $CHROMIUM $XVFB 2>/dev/null || true
  exit "$3"
}

Xvfb :99 -screen 0 1280x2000x24 -nolisten tcp >"$XVFB_LOG" 2>&1 &
XVFB=$!
i=0
while [ ! -S /tmp/.X11-unix/X99 ]; do
  kill -0 "$XVFB" 2>/dev/null || fail xvfb "$XVFB_LOG" 71
  i=$((i + 1))
  [ "$i" -gt 100 ] && fail xvfb "$XVFB_LOG" 70
  sleep 0.1
done

chromium \
  --remote-debugging-port=9222 \
  --user-data-dir=/profile \
  --disk-cache-size=1 \
  --media-cache-size=1 \
  --disable-breakpad \
  --disable-crash-reporter \
  --disable-gpu \
  --password-store=basic \
  --no-first-run \
  --no-default-browser-check \
  --disable-sync \
  --disable-background-networking \
  --lang=ja \
  --window-size=1280,2000 \
  about:blank >"$CHROMIUM_LOG" 2>&1 &
CHROMIUM=$!

socat TCP-LISTEN:9223,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:9222 &
SOCAT=$!

trap 'kill "$SOCAT" "$CHROMIUM" "$XVFB" 2>/dev/null || true; exit 0' TERM INT
# Any child ending ends the container (the Hub then sees it as stopped).
while :; do
  kill -0 "$XVFB" 2>/dev/null || fail xvfb "$XVFB_LOG" 71
  kill -0 "$CHROMIUM" 2>/dev/null || fail chromium "$CHROMIUM_LOG" 72
  kill -0 "$SOCAT" 2>/dev/null || fail socat "" 73
  sleep 1 &
  wait $! || true
done
