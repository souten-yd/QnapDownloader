"""Safe QTS-only service PID reconciler: never signal based on PID/name alone."""
import os
from pathlib import Path
import re
import signal
import socket
import sys
import time

PORT = 17864
PYTHON = re.compile(r"^python(?:3(?:\.[0-9]+)?)?(?: \(deleted\))?$")


def owned(pid, root, data):
    try:
        base = Path("/proc") / str(pid)
        stat = (base / "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        birth = int(stat[19])
        raw = (base / "cmdline").read_bytes()
        if not raw or len(raw) > 16384:
            return None
        args = [os.fsdecode(x) for x in raw.split(b"\0") if x]
        # Only exact approved CLI layouts are recognized. QTS may use the
        # /share/Container alias or its canonical cache-volume path.
        exact_data_paths = {str(data), os.path.realpath(data)}
        if not args or not PYTHON.fullmatch(Path(args[0]).name):
            return None
        command = args[2:] if len(args) > 1 and args[1] == "-B" else args[1:]
        approved_prefixes = [
            [str(root / "webapp.py"), "--secure-cookie", "--port", str(PORT), "--data-dir"],
            [str(root / "webapp.py"), "--lan", "--port", str(PORT), "--data-dir"],
        ]
        if (len(command) != 6 or command[:5] not in approved_prefixes or
                command[5] not in exact_data_paths):
            return None
        if not PYTHON.fullmatch(Path(os.readlink(base / "exe")).name):
            return None
        cwd = os.readlink(base / "cwd")
        safe = {str(root), str(root) + " (deleted)",
                str(root.parent / ".tmp-QnapDownloader"),
                str(root.parent / ".tmp-QnapDownloader") + " (deleted)"}
        if cwd not in safe:
            return None
        return birth, cwd
    except (OSError, ValueError, IndexError):
        return None


def scan(root, data):
    result = {}
    for name in os.listdir("/proc"):
        if name.isdecimal():
            pid = int(name)
            info = owned(pid, root, data)
            if info is not None:
                result[pid] = info
    return result


def recorded_pid(path):
    try:
        value = path.read_text().strip()
        return int(value) if value.isdecimal() else None
    except OSError:
        return None


def port_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", PORT))
            return True
        except OSError:
            return False


def terminate(matches, root, data):
    for pid, (birth, _) in matches.items():
        now = owned(pid, root, data)
        if now is None or now[0] != birth:
            print("QnapDownloader ownership changed; no signal sent to", pid,
                  file=sys.stderr)
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except OSError as error:
            print("QnapDownloader SIGTERM failed:", error, file=sys.stderr)
            return False
    deadline = time.monotonic() + 14.0
    while time.monotonic() < deadline:
        if not any((info := owned(pid, root, data)) is not None and
                   info[0] == birth for pid, (birth, _) in matches.items()):
            return True
        time.sleep(0.1)
    print("QnapDownloader owned server remains; refusing overlap", file=sys.stderr)
    return False


def main(args):
    if len(args) != 5 or args[1] not in ("prepare", "stop", "check", "status"):
        print("Usage: downloader_pid.py {prepare|stop|check|status} ROOT DATA PIDFILE",
              file=sys.stderr)
        return 2
    action, root, data, pidfile = args[1], Path(args[2]), Path(args[3]), Path(args[4])
    root = Path(os.path.abspath(root))
    data = Path(os.path.abspath(data))
    matches = scan(root, data)
    recorded = recorded_pid(pidfile)
    if action in ("check", "status"):
        return 0 if recorded in matches else 1
    if action == "prepare" and len(matches) == 1 and recorded in matches:
        if matches[recorded][1] == str(root):
            return 0
    if matches and not terminate(matches, root, data):
        return 1
    try:
        pidfile.unlink(missing_ok=True)
    except OSError as error:
        print("Stale PID file cleanup failed:", error, file=sys.stderr)
        return 1
    if action == "stop":
        return 0
    if not port_free():
        print("Port 17864 held by unrelated process; refusing takeover", file=sys.stderr)
        return 1
    return 10


if __name__ == "__main__":
    sys.exit(main(sys.argv))
