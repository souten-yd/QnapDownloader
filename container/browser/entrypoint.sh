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

mkdir -p -m 1777 /tmp/.X11-unix
Xvfb :99 -screen 0 1280x2000x24 -nolisten tcp &
XVFB=$!
i=0
while [ ! -S /tmp/.X11-unix/X99 ]; do
  i=$((i + 1))
  [ "$i" -gt 100 ] && exit 70
  sleep 0.1
done

chromium \
  --remote-debugging-port=9222 \
  --user-data-dir=/profile \
  --disk-cache-size=1 \
  --media-cache-size=1 \
  --disable-breakpad \
  --disable-crash-reporter \
  --no-first-run \
  --no-default-browser-check \
  --disable-sync \
  --disable-background-networking \
  --lang=ja \
  --window-size=1280,2000 \
  about:blank &
CHROMIUM=$!

socat TCP-LISTEN:9223,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:9222 &
SOCAT=$!

trap 'kill "$SOCAT" "$CHROMIUM" "$XVFB" 2>/dev/null || true; exit 0' TERM INT
# Any child ending ends the container (the Hub then sees it as stopped).
while kill -0 "$XVFB" 2>/dev/null && kill -0 "$CHROMIUM" 2>/dev/null && kill -0 "$SOCAT" 2>/dev/null; do
  sleep 1 &
  wait $! || true
done
kill "$SOCAT" "$CHROMIUM" "$XVFB" 2>/dev/null || true
exit 1
