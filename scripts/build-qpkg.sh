#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 2 || "$1" != x86_64 ]]; then
  echo "Usage: scripts/build-qpkg.sh x86_64 <output-dir>" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT="$2"
command -v qbuild >/dev/null || { echo 'QDK qbuild is required' >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/shared" "$OUTPUT"
cp "$ROOT/qpkg/qpkg.cfg" "$WORK/qpkg.cfg"
cp "$ROOT/qpkg/package_routines" "$WORK/package_routines"
cp "$ROOT"/{webapp.py,storage.py,updater.py,self_update.py,durability.py,jd2_policy.py,jd2_control.py,jd2_myjd.py,README.md} "$WORK/shared/"
cp "$ROOT"/qpkg/shared/{downloader.sh,downloader_pid.py,python3-path} "$WORK/shared/"
cp -R "$ROOT/web" "$WORK/shared/web"
# Native x86_64 dependencies must be prepared on the build machine.
# No pip/npm operation happens at NAS app startup.
if [ -d "$ROOT/vendor" ]; then
  cp -R "$ROOT/vendor" "$WORK/shared/vendor"
fi
chmod 755 "$WORK/shared/downloader.sh" "$WORK/shared/python3-path"
. "$WORK/qpkg.cfg"
qbuild --root "$WORK" --build-arch x86_64 --build-dir "$WORK/build"
count=$(find "$WORK/build" -maxdepth 1 -type f -name '*.qpkg' | wc -l)
[[ $count -eq 1 ]] || { echo "Expected one QPKG" >&2; exit 1; }
pkg=$(find "$WORK/build" -maxdepth 1 -type f -name '*.qpkg' -print -quit)
qbuild --query info "$pkg"
cp "$pkg" "$OUTPUT/QnapDownloader_$QPKG_VER"_x86_64.qpkg
sha256sum "$OUTPUT/QnapDownloader_$QPKG_VER"_x86_64.qpkg
