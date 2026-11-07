"""One unified state for the web client: GET /api/state?rev=N (v0.1.33).

The client used to poll the engine, the JD2 download list, the NAS browser status and the settings
revision separately. The hub computes ONE compact state from cached sources (engine status 2 s,
JD2 counts 5 s, NAS browser status 2 s), bumps `rev` whenever the content changes, and lets a
caller wait (long-poll, at most WAIT_MAX seconds) until `rev` differs from the one it holds.
Nothing is persisted and nothing is polled in the background: sources are read only while a
client is actually asking.
"""
import hashlib
import json
import re
import threading
import time

import debuglog

ENGINE_TTL = 2.0
ENGINE_TTL_STABLE = 5.0   # v0.1.59: phase running/stopped/not_installed: docker inspect at most every 5 s
STABLE_PHASES = ("running", "stopped", "not_installed")
COUNTS_TTL = 5.0
RB_TTL = 2.0
WAIT_MAX = 25.0
VOLATILE_RB_KEYS = ('mem', 'idle_mib', 'cur_mib')
TICK = 1.0          # a waiting poller re-reads the (cached) sources at most this often


def _digest(value):
    text = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _bucket(value, step):
    """v0.1.59: coarse bucket for a volatile figure so it does not bump the revision every tick."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return int(value // step * step)


def engine_signature(status):
    """v0.1.59: digest of the parts of the engine status the UI reacts to (no counters or timestamps)."""
    container = status.get("container") or {}
    leftovers = status.get("native_leftovers") or {}
    return _digest({
        "phase": status.get("phase"), "mode": status.get("mode"), "preferred": status.get("preferred_mode"),
        "installed": status.get("installed"), "autostart": status.get("autostart"),
        "preset": status.get("preset_applied"), "error": status.get("error"),
        "setup": status.get("setup_running"), "migrate": status.get("can_migrate"),
        "container": [container.get("available"), container.get("version"), container.get("state"),
                      container.get("mounted_dir"), container.get("image")],
        "leftovers": leftovers.get("present"),
        "steps": [[x.get("id"), x.get("status"), x.get("detail"), x.get("progress")]
                  for x in status.get("steps") or []],
        "components": [[x.get("name"), x.get("installed"), x.get("version")]
                      for x in status.get("components") or []],
        "heap": status.get("heap_mb"), "core": status.get("core_revision"),
        "restarts": status.get("restart_count")})


def rb_signature(status):
    """v0.1.59: digest of the NAS-browser status without memory figures, countdowns and event times."""
    res = status.get("resident") or {}
    wd = res.get("watchdog") or {}
    reason = status.get("reason")
    return _digest({
        "phase": status.get("phase"), "setup": status.get("setup_running"),
        "ready": status.get("image_ready"), "update": status.get("update_needed"),
        "station": status.get("container_station"), "error": status.get("error"),
        "steps": [[x.get("id"), x.get("status"), x.get("detail")] for x in status.get("steps") or []],
        "tabs": [[t.get("id"), t.get("url"), t.get("title")] for t in status.get("tabs") or []],
        "can_open": status.get("can_open"),
        "reason": None if not reason else re.sub(r"\d+", "#", str(reason)),   # memory % inside the text
        "res": [res.get("on"), res.get("state"), res.get("user_stopped"), res.get("tabs")],
        "wd": [wd.get("state"), wd.get("restart_n"), wd.get("restarts"), wd.get("lost_seq"),
               wd.get("reason"), wd.get("message")]})


def _item_state(item):
    if item.get("finished") is True:
        return "finished"
    return "running" if item.get("running") is True else "waiting"


def download_counts(snapshot):
    """running / waiting / finished over the rows of page 0 (packages, else loose links)."""
    rows = snapshot.get("packages") or snapshot.get("links") or []
    counts = {"running": 0, "waiting": 0, "finished": 0}
    for row in rows:
        counts[_item_state(row)] += 1
    return counts


class StateHub:
    def __init__(self, server, version, cache, clock=time.monotonic):
        self._server = server
        self._version = version
        self._cache = cache
        self._clock = clock
        self._cond = threading.Condition()
        self._rev = int(time.time()) & 0x3fffffff
        self._sig = None
        self._state = None
        self._computed = None
        self._engine_phase = None

    # ---- sources (all read through the shared single-flight cache) ----------------------------
    def _engine(self):
        server = self._server
        ttl = ENGINE_TTL_STABLE if self._engine_phase in STABLE_PHASES else ENGINE_TTL
        status = self._cache.get("engine:status", ttl, server.engine.status)
        self._engine_phase = status.get("phase")
        running = status.get("phase") == "running"
        can_send = False
        if running:
            try:
                jd2 = self._cache.get("jd2:status", COUNTS_TTL, server.jd2.status)
                can_send = jd2.get("can_send") is True
            except Exception:
                can_send = False
        return {"state": status.get("phase"), "mode": status.get("mode"), "running": running,
                "can_send": can_send, "preset_applied": status.get("preset_applied") is True,
                "installed": status.get("installed") is True, "autostart": status.get("autostart") is True,
                "error": status.get("error"),
                "cs": (status.get("container") or {}).get("available") is True,
                "sig": engine_signature(status)}, running

    def _downloads(self, running):
        if not running:
            return None
        try:
            snap = self._cache.get("jd2:downloads:0", COUNTS_TTL,
                                   lambda: self._server.jd2.downloads_snapshot(0))
        except Exception:
            return None
        return download_counts(snap)

    def _rb(self):
        try:
            status = self._cache.get("rb:status", RB_TTL, self._server.rb.status)
        except Exception:
            return {"state": "unknown", "tabs": 0, "mem": None, "ready": None, "sig": ""}
        memory = status.get("memory") or {}
        res = status.get("resident") or {}
        wd = res.get("watchdog") or {}
        idle = res.get("idle") or {}
        used = (res.get("memory") or {}).get("used_mib")
        # v0.1.59: memory figures are bucketed coarsely (10 % / 50 MiB) and the signature ignores them, so
        # a changing RSS or percentage never wakes a waiting client; /api/rb/status has the exact numbers.
        return {"state": status.get("phase"), "tabs": len(status.get("tabs") or []),
                "mem": _bucket(memory.get("percent"), 10), "ready": status.get("image_ready") is True,
                "res": res.get("on") is True, "rs": res.get("state"),
                "idle_mib": _bucket(idle.get("latest_mib"), 50),
                "cur_mib": _bucket(used, 50),
                "restarts": wd.get("restarts", 0), "lost": wd.get("lost_seq", 0),
                "sig": rb_signature(status)}

    def _build(self):
        engine, running = self._engine()
        return {"engine": engine, "downloads": self._downloads(running), "rb": self._rb(),
                "settings_rev": self._server.store.revision(), "version": self._version,
                "debug": debuglog.LOG.client_status()}

    # ---- revision handling -------------------------------------------------------------------
    def _refresh(self, force=False):
        """Recompute (at most once per TICK unless forced); bump rev when the content changed."""
        with self._cond:
            now = self._clock()
            if not force and self._computed is not None and now - self._computed < TICK * 0.9:
                return self._rev, self._state
        state = self._build()
        # v0.1.59: the bucketed memory figures ride along in the answer but never decide the revision
        # (a value hovering at a bucket edge would otherwise wake every waiting client).
        core = dict(state, rb={k: v for k, v in state["rb"].items() if k not in VOLATILE_RB_KEYS})
        sig = _digest(core)
        with self._cond:
            self._computed = self._clock()
            if sig != self._sig:
                self._sig = sig
                self._state = state
                self._rev += 1
                self._cond.notify_all()
            return self._rev, self._state

    def poke(self):
        """A write happened: waiting pollers re-read the sources now."""
        with self._cond:
            self._computed = None
            self._cond.notify_all()

    def poll(self, since, wait):
        """(rev, state). With `since`, waits up to `wait` s while rev equals it."""
        wait = max(0.0, min(float(wait), WAIT_MAX))
        deadline = self._clock() + wait
        rev, state = self._refresh()
        while since is not None and rev == since:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            with self._cond:
                self._cond.wait(min(remaining, TICK))
            rev, state = self._refresh()
        return rev, state
