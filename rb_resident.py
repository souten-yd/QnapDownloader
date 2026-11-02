"""NAS browser resident mode support (v0.1.38): watchdog state machine, memory figures.

Pure logic with injected clocks, so it is testable without docker or threads:
  * Watchdog        restart schedule after an unexpected stop (5 s -> 15 s -> 60 s, at most 5 restarts
                    per 15 minutes, then it gives up until the user presses "retry"). It never acts by
                    itself; RemoteBrowser asks it what to do. Docker's own `--restart` policy is NOT
                    used (the Hub must keep control and the memory guard), see docs/container-runtime.md.
  * IdleBaseline    the measured memory of the resident browser with no tabs (min / latest / time).
  * cgroup memory   reading memory.current (v2) or usage_in_bytes minus inactive file (v1).
Everything is kept in RAM only.
"""
import os
import re
import time
from collections import deque

BACKOFF = (5.0, 15.0, 60.0)       # delay before restart 1, 2, 3 and later
MAX_RESTARTS = 5                  # restarts allowed ...
WINDOW = 900.0                    # ... per this many seconds
MEMORY_RECHECK = 5.0              # while memory is short, look again this often
EVENT_CAP = 20
IDLE_SETTLE = 60.0                # no tabs for this long (after the trim) -> record the idle memory
IDLE_RESAMPLE = 300.0             # while idle, refresh the figure this often
MEM_TTL = 5.0

STATE_IDLE = "idle"               # nothing pending
STATE_SCHEDULED = "scheduled"     # a (re)start is due at next_at
STATE_MEMORY = "memory_wait"      # due, but the memory guard says wait
STATE_STARTING = "starting"       # a start is running now
STATE_GAVE_UP = "gave_up"         # too many stops: waits for the user's retry
STATE_BLOCKED = "blocked"         # e.g. the image is gone: waits for setup / toggling

MSG_MEMORY_WAIT = "メモリ不足のため待機中"
MSG_GAVE_UP = "NASブラウザが繰り返し停止したため自動再起動を止めました（理由: %s）"
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def reason_text(info):
    """Japanese reason for a stop from docker's State: 'メモリ不足で強制終了 / 終了コード 137'."""
    info = info or {}
    code = info.get("exit_code")
    error = CONTROL.sub(" ", str(info.get("error") or "")).strip()[:80]
    if info.get("oom") is True:
        cause = "メモリ不足で強制終了"
    elif info.get("lost") is True:
        cause = "ブラウザとの接続が切れました"
    elif error:
        cause = error
    elif code == 137:
        cause = "強制終了（SIGKILL）"
    elif code == 143:
        cause = "終了要求を受けて停止"
    elif info.get("reason"):
        cause = CONTROL.sub(" ", str(info["reason"])).strip()[:120]
    else:
        cause = "予期しない停止"
    if isinstance(code, int) and not isinstance(code, bool):
        return "%s / 終了コード %d" % (cause, code)
    return cause


class Watchdog:
    def __init__(self, clock=time.monotonic, wall=time.time):
        self._clock = clock
        self._wall = wall
        self.state = STATE_IDLE
        self.next_at = 0.0
        self.attempts = deque()       # monotonic times of counted restart attempts
        self.reason = ""              # reason text of the last unexpected stop
        self.restarts = 0             # restarts that succeeded after an unexpected stop
        self.lost_seq = 0             # unexpected stops that took tabs with them
        self.events = deque(maxlen=EVENT_CAP)
        self._after_exit = False      # the pending start follows an unexpected stop

    # --------------------------------------------------------------- events
    def event(self, kind, text):
        item = {"at": int(self._wall()), "kind": kind, "text": str(text)[:200]}
        self.events.append(item)
        return item

    def event_list(self):
        return [dict(e) for e in self.events]

    # ----------------------------------------------------------- bookkeeping
    def _prune(self):
        now = self._clock()
        while self.attempts and now - self.attempts[0] >= WINDOW:
            self.attempts.popleft()

    def restart_n(self):
        """Which restart the user sees: the one waiting or running (1-based)."""
        self._prune()
        if self.state == STATE_STARTING:
            return max(1, len(self.attempts))
        if self.state in (STATE_SCHEDULED, STATE_MEMORY):
            return len(self.attempts) + 1 if self._after_exit else 0
        return 0

    # ------------------------------------------------------------ transitions
    def exited(self, info, tabs_lost=False):
        """An unexpected stop. Returns the new state (scheduled or gave_up)."""
        self.reason = reason_text(info)
        if tabs_lost:
            self.lost_seq += 1
        self._after_exit = True
        self._prune()
        if len(self.attempts) >= MAX_RESTARTS:
            self.state = STATE_GAVE_UP
            self.event("gave_up", MSG_GAVE_UP % self.reason)
            return self.state
        delay = BACKOFF[min(len(self.attempts), len(BACKOFF) - 1)]
        self.next_at = self._clock() + delay
        self.state = STATE_SCHEDULED
        self.event("exit", "停止を検出（%s）。%d秒後に再起動します" % (self.reason, int(delay)))
        return self.state

    def schedule_initial(self):
        """Resident mode wants the container up (boot / switch on): start now, not counted."""
        if self.state in (STATE_IDLE, STATE_BLOCKED):
            self.state = STATE_SCHEDULED
            self.next_at = self._clock()
            self._after_exit = False
        return self.state

    def due(self):
        return self.state in (STATE_SCHEDULED, STATE_MEMORY) and self._clock() >= self.next_at

    def memory_blocked(self):
        """The memory guard said wait: no attempt is used; look again soon."""
        first = self.state != STATE_MEMORY
        self.state = STATE_MEMORY
        self.next_at = self._clock() + MEMORY_RECHECK
        if first:
            self.event("memory_wait", MSG_MEMORY_WAIT)

    def begin_attempt(self):
        self._prune()
        if self._after_exit:
            self.attempts.append(self._clock())
        self.state = STATE_STARTING
        if self._after_exit:
            self.event("restart", "自動再起動を試みます（%d回目）" % len(self.attempts))

    def succeeded(self):
        """The container is up again (the watchdog's own start or the user's tab open)."""
        toast = self._after_exit and self.state != STATE_IDLE
        if toast:
            self.restarts += 1
            self.event("restarted", "NASブラウザを再起動しました")
        self.state = STATE_IDLE
        self._after_exit = False
        return toast

    def failed(self, text):
        """A start did not work: handled like another unexpected stop."""
        self.event("restart_failed", "起動に失敗: %s" % CONTROL.sub(" ", str(text))[:120])
        was = self._after_exit
        self.state = STATE_IDLE
        self.exited({"reason": text})
        self._after_exit = True
        return was

    def disarm(self, text):
        """Cannot start for a reason that waiting will not fix (e.g. image missing)."""
        self.state = STATE_BLOCKED
        self._after_exit = False
        self.event("blocked", text)

    def unblock(self):
        if self.state == STATE_BLOCKED:
            self.state = STATE_IDLE

    def cancel(self):
        """The user/Hub stopped the container on purpose: nothing may restart it."""
        if self.state in (STATE_SCHEDULED, STATE_MEMORY, STATE_STARTING):
            self.state = STATE_IDLE
            self._after_exit = False
            self.event("cancel", "手動の停止のため自動再起動を取り消しました")

    def retry(self):
        """The user's retry button: forget the history and try now."""
        self.attempts.clear()
        self.state = STATE_SCHEDULED
        self.next_at = self._clock()
        self._after_exit = True
        self.event("retry", "再試行を受け付けました")

    def reset(self):
        self.attempts.clear()
        self.state = STATE_IDLE
        self._after_exit = False

    # --------------------------------------------------------------- public
    def message(self):
        if self.state == STATE_GAVE_UP:
            return MSG_GAVE_UP % self.reason
        if self.state == STATE_MEMORY:
            return MSG_MEMORY_WAIT
        return ""

    def snapshot(self):
        self._prune()
        wait = None
        if self.state in (STATE_SCHEDULED, STATE_MEMORY):
            wait = max(0, int(round(self.next_at - self._clock())))
        return {"state": self.state, "restart_n": self.restart_n(), "max_restarts": MAX_RESTARTS,
                "window_s": int(WINDOW), "next_in_s": wait, "reason": self.reason,
                "message": self.message(), "restarts": self.restarts, "lost_seq": self.lost_seq,
                "events": self.event_list()}


class IdleBaseline:
    """Memory of the resident browser with no tabs: minimum, latest and when (RAM only)."""

    def __init__(self, wall=time.time):
        self._wall = wall
        self.min_bytes = None
        self.latest_bytes = None
        self.anon_bytes = None
        self.at = None
        self.samples = 0

    def record(self, used_bytes, anon_bytes=None):
        if not isinstance(used_bytes, int) or used_bytes <= 0:
            return False
        self.latest_bytes = used_bytes
        self.anon_bytes = anon_bytes if isinstance(anon_bytes, int) else None
        self.min_bytes = used_bytes if self.min_bytes is None else min(self.min_bytes, used_bytes)
        self.at = int(self._wall())
        self.samples += 1
        return True

    def snapshot(self, total_kib=None):
        if self.latest_bytes is None:
            return None
        mib = 1024 * 1024
        out = {"latest_mib": round(self.latest_bytes / mib), "min_mib": round(self.min_bytes / mib),
               "anon_mib": None if self.anon_bytes is None else round(self.anon_bytes / mib),
               "at": self.at, "samples": self.samples, "percent_of_total": None}
        if isinstance(total_kib, int) and total_kib > 0:
            out["percent_of_total"] = round(self.latest_bytes / (total_kib * 1024) * 100, 1)
        return out


# ------------------------------------------------------------------ cgroup memory
def parse_flat_keys(text):
    """'key value' lines (memory.stat) -> {key: int}; bad lines are skipped."""
    values = {}
    for line in str(text).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            values[parts[0]] = int(parts[1])
    return values


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read(1 << 20)


def _read_int(path):
    text = _read(path).strip()
    if not text.isdigit():
        raise ValueError("number")
    return int(text)


def _read_stat(directory):
    try:
        return parse_flat_keys(_read(os.path.join(directory, "memory.stat")))
    except OSError:
        return {}


def read_cgroup_memory(paths):
    """{used_bytes, anon_bytes|None, file_bytes|None, source} from the first readable cgroup, or None.

    v2: memory.current, with anon / file from memory.stat. v1: memory/usage_in_bytes minus
    total_inactive_file (what `docker stats` reports as usage), anon = total_rss.
    """
    for version, directory in paths:
        try:
            if version == 2:
                used = _read_int(os.path.join(directory, "memory.current"))
                stat = _read_stat(directory)
                return {"used_bytes": used, "anon_bytes": stat.get("anon"),
                        "file_bytes": stat.get("file"), "source": "cgroup v2"}
            for base in (directory, directory.replace("/blkio/", "/memory/", 1)):
                try:
                    usage = _read_int(os.path.join(base, "memory.usage_in_bytes"))
                except (OSError, ValueError):
                    continue
                stat = _read_stat(base)
                inactive = stat.get("total_inactive_file", stat.get("inactive_file", 0))
                return {"used_bytes": max(0, usage - inactive),
                        "anon_bytes": stat.get("total_rss", stat.get("rss")),
                        "file_bytes": stat.get("total_cache", stat.get("cache")),
                        "source": "cgroup v1"}
        except (OSError, ValueError):
            continue
    return None


def hub_rss_bytes(path="/proc/self/status"):
    """VmRSS of the Hub process in bytes, or None."""
    try:
        for line in _read(path).splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                return int(parts[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def mem_total_kib(path="/proc/meminfo"):
    try:
        for line in _read(path).splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None
