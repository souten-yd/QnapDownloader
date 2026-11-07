"""v0.1.65: server-side queue of long-press link analyses ("probes").

Before v0.1.65 every long press held one HTTP request open until its hidden browser page had finished
(up to 4 pages at once, the rest waiting in the request, 30 s for the memory guard, then an error). On a
phone that is the wrong shape: a held request occupies one of the browser's ~6 connections per origin (the
Hub's long polls need two of them), a slow or memory-starved analysis turns into an error, and a phone that
goes to the background loses the answer.

Now a long press only *adds a job* and returns at once. One background worker takes the jobs one by one
(CONCURRENCY = 1: a hidden Chromium page costs far more than the queue entry), waits for the memory guard
instead of failing, and keeps the finished jobs in RAM for RESULT_TTL so the results survive a phone that was
away. Nothing here touches the network or the browser itself: the caller injects `execute` (runs one probe),
`memory_reason` (the guard), `tab_alive`, and the callbacks; tests drive the same code with fakes.

RAM only. Job record: {id, tab, href, label, host, state, wait, position, links, final_url, redirects, error,
note, attempts, created, started, finished, sent}; state is queued | running | done | failed | cancelled and
`wait` is "memory" while a queued job waits for the guard (shown as "メモリ待ち").
"""
import secrets
import threading
import time
from urllib.parse import urlsplit

CONCURRENCY = 1                 # hidden pages analysed at once
QUEUE_CAP = 200                 # jobs kept in total (waiting, running and finished)
RESULT_TTL = 1800.0             # finished jobs are kept this long (the phone may have been in the background)
DONE_DEDUPE = 60.0              # the same link of the same tab, finished this recently, is not analysed again
MEMORY_WAIT_MAX = 600.0         # a job waits this long for the memory guard, then fails with the guard's text
MEMORY_POLL = 1.0               # how often the guard is looked at while waiting
LABEL_MAX = 80
LINK_MAX = 50

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
ACTIVE = (QUEUED, RUNNING)
FINISHED = (DONE, FAILED, CANCELLED)


class QueueFull(RuntimeError):
    """Every slot holds a job that is still waiting or running."""


def clean_label(value, href):
    """The text of the pressed link (control characters out, <= LABEL_MAX), else its host."""
    text = ""
    if isinstance(value, str):
        text = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())[:LABEL_MAX]
    if text:
        return text
    return host_of(href)


def host_of(href):
    try:
        return (urlsplit(href).hostname or "")[:LABEL_MAX]
    except ValueError:
        return ""


class ProbeQueue:
    def __init__(self, execute, memory_reason=None, tab_alive=None, on_change=None, on_done=None,
                 clock=time.monotonic, wall=time.time, concurrency=CONCURRENCY, start=True):
        self._execute = execute
        self._memory_reason = memory_reason or (lambda: None)
        self._tab_alive = tab_alive or (lambda tab: True)
        self._on_change = on_change
        self._on_done = on_done
        self._clock = clock
        self._wall = wall
        self._concurrency = max(1, int(concurrency))
        self._start = start
        self._cv = threading.Condition()
        self._jobs = {}            # id -> job (insertion order = creation order)
        self._rev = {}             # tab -> revision (bumped by every change of one of its jobs)
        self._running = 0
        self._closed = False
        self._threads = []
        self.peak_waiting = 0      # most jobs waiting at once (for the self-test / numbers in the docs)

    # ------------------------------------------------------------------ public API
    def enqueue(self, tab, href, label=None):
        """Add a job; returns (snapshot, existing). The same href of the same tab that is waiting, running or was
        finished less than DONE_DEDUPE ago is not queued again: the existing job comes back (existing=True)."""
        now = self._clock()
        with self._cv:
            if self._closed:
                raise QueueFull("解析キューは停止しています")
            for job in self._jobs.values():
                if job["tab"] != tab or job["href"] != href:
                    continue
                if job["state"] in ACTIVE or (job["state"] == DONE and now - job["_finished"] < DONE_DEDUPE):
                    return self._snapshot_locked(job), True
            self._expire_locked(now)
            if len(self._jobs) >= QUEUE_CAP:
                # make room: the oldest finished job goes; when everything is still waiting or running, refuse
                for key, job in self._jobs.items():
                    if job["state"] in FINISHED:
                        del self._jobs[key]
                        break
                else:
                    raise QueueFull("解析待ちが多すぎます（%d件）。しばらく待ってから追加してください" % QUEUE_CAP)
            job = {"id": secrets.token_urlsafe(9), "tab": tab, "href": href, "label": clean_label(label, href),
                   "host": host_of(href), "state": QUEUED, "wait": None, "links": [], "final_url": None,
                   "redirects": 0, "error": None, "note": None, "attempts": 0, "sent": None,
                   "created": self._wall(), "started": None, "finished": None,
                   "_created": now, "_finished": None}
            self._jobs[job["id"]] = job
            waiting = sum(1 for j in self._jobs.values() if j["state"] == QUEUED)
            self.peak_waiting = max(self.peak_waiting, waiting)
            self._bump_locked(tab)
            snap = self._snapshot_locked(job)
            self._ensure_worker_locked()
            self._cv.notify_all()
        self._changed(tab)
        return snap, False

    def jobs(self, tab):
        """Snapshots of the tab's jobs, oldest first (finished jobs older than RESULT_TTL are dropped first)."""
        with self._cv:
            self._expire_locked(self._clock())
            return [self._snapshot_locked(j) for j in self._jobs.values() if j["tab"] == tab and not j.get("_orphan")]

    def get(self, tab, job_id):
        with self._cv:
            job = self._jobs.get(job_id)
            return self._snapshot_locked(job) if job is not None and job["tab"] == tab else None

    def summary(self, tab):
        """Compact counts + revision for the tab's meta long-poll: {rev, queued, running, done, failed, total, review}."""
        with self._cv:
            out = {"rev": self._rev.get(tab, 0), "queued": 0, "running": 0, "done": 0, "failed": 0, "total": 0,
                   "review": 0}
            for job in self._jobs.values():
                if job["tab"] != tab or job["state"] == CANCELLED:
                    continue
                out[job["state"]] += 1
                out["total"] += 1
                if job["state"] == DONE and job["links"] and not (job["sent"] and not job["sent"].get("error")):
                    out["review"] += 1          # links found that nobody has sent to JD2 yet: the phone shows them
            return out

    def revision(self, tab):
        with self._cv:
            return self._rev.get(tab, 0)

    def cancel(self, tab, job_id=None):
        """Cancel the tab's waiting jobs (one, or all when job_id is None). A running job is not interrupted.
        Returns how many were cancelled."""
        count = 0
        with self._cv:
            for job in self._jobs.values():
                if job["tab"] != tab or job["state"] != QUEUED or (job_id is not None and job["id"] != job_id):
                    continue
                self._finish_locked(job, CANCELLED, note="取り消しました")
                count += 1
            if count:
                self._bump_locked(tab)
                self._cv.notify_all()
        if count:
            self._changed(tab)
        return count

    def retry(self, tab, job_id=None):
        """Put failed jobs (one, or all of the tab) back at the end of the queue. Returns how many."""
        count = 0
        with self._cv:
            if self._closed:
                return 0
            for job in [j for j in self._jobs.values()
                        if j["tab"] == tab and j["state"] == FAILED and (job_id is None or j["id"] == job_id)]:
                job.update(state=QUEUED, wait=None, error=None, note=None, links=[], final_url=None, redirects=0,
                           sent=None, started=None, finished=None, _finished=None, _claimed=False)
                # a retried job goes behind the others: re-insert it
                self._jobs[job["id"]] = self._jobs.pop(job["id"])
                count += 1
            if count:
                self._bump_locked(tab)
                self._ensure_worker_locked()
                self._cv.notify_all()
        if count:
            self._changed(tab)
        return count

    def dismiss(self, tab, job_id=None):
        """Forget the tab's finished jobs (one, or all). Returns how many."""
        count = 0
        with self._cv:
            for key in [k for k, j in self._jobs.items() if j["tab"] == tab and j["state"] in FINISHED and
                        (job_id is None or j["id"] == job_id)]:
                del self._jobs[key]
                count += 1
            if count:
                self._bump_locked(tab)
        if count:
            self._changed(tab)
        return count

    def tab_closed(self, tab):
        """The tab is gone: its waiting jobs never run, its results are dropped (nobody can ask for them any more)."""
        with self._cv:
            keys = [k for k, j in self._jobs.items() if j["tab"] == tab]
            for key in keys:
                job = self._jobs[key]
                if job["state"] == RUNNING:
                    job["_orphan"] = True      # the worker finishes it and drops the result
                else:
                    job["state"] = CANCELLED
                    del self._jobs[key]
            self._rev.pop(tab, None)
            self._cv.notify_all()
        return len(keys)

    def mark_sent(self, tab, job_id, sent):
        """Record the outcome of sending a job's links to JD2 (a small dict) so no one sends them twice."""
        with self._cv:
            job = self._jobs.get(job_id)
            if job is None or job["tab"] != tab:
                return False
            job["sent"] = dict(sent)
            self._bump_locked(tab)
            self._cv.notify_all()
        self._changed(tab)
        return True

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        for thread in list(self._threads):
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)

    def idle(self):
        """True when nothing is waiting or running (tests)."""
        with self._cv:
            return self._running == 0 and not any(j["state"] in ACTIVE for j in self._jobs.values())

    def wait_idle(self, timeout=10.0):
        end = time.monotonic() + timeout
        with self._cv:
            while not (self._running == 0 and not any(j["state"] in ACTIVE for j in self._jobs.values())):
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(min(remaining, 0.2))
        return True

    # ------------------------------------------------------------------ worker
    def run_pending(self):
        """Run the waiting jobs on the calling thread until none is left (tests with start=False)."""
        while self._step():
            pass

    def _ensure_worker_locked(self):
        if not self._start or self._closed:
            return
        self._threads = [t for t in self._threads if t.is_alive()]
        if len(self._threads) < self._concurrency:
            thread = threading.Thread(target=self._loop, name="rb-probe-queue", daemon=True)
            self._threads.append(thread)
            thread.start()

    def _loop(self):
        while True:
            with self._cv:
                if self._closed:
                    return
                if not self._unclaimed_locked():
                    self._cv.wait(5.0)
                    if self._closed:
                        return
                    if not self._unclaimed_locked():
                        # nothing for a while: the thread ends (a new job starts another one)
                        self._threads = [t for t in self._threads if t is not threading.current_thread()]
                        return
                    continue
            self._step()

    def _unclaimed_locked(self):
        return next((j for j in self._jobs.values() if j["state"] == QUEUED and not j.get("_claimed")), None)

    def _step(self):
        """Take the oldest waiting job and run it. False when no job is waiting."""
        with self._cv:
            if self._running >= self._concurrency:
                self._cv.wait(0.2)
                return True
            job = self._unclaimed_locked()
            if job is None:
                return False
            job["_claimed"] = True
            tab = job["tab"]
            self._running += 1
        try:
            self._run_job(job, tab)
        finally:
            with self._cv:
                self._running -= 1
                self._cv.notify_all()
        return True

    def _run_job(self, job, tab):
        # the job stays "queued" (wait: memory) while the guard is not met
        started_wait = self._clock()
        while True:
            with self._cv:
                if job["state"] != QUEUED or self._jobs.get(job["id"]) is not job or self._closed:
                    return              # cancelled (or its tab closed) meanwhile
            if not self._tab_alive(tab):
                self._finish(job, CANCELLED, note="タブが閉じられたため解析しませんでした")
                return
            try:
                reason = self._memory_reason()
            except Exception as error:      # the guard itself failing is not the link's fault: keep waiting
                reason = "メモリの状態を確認できません（%s）" % type(error).__name__
            if not reason:
                break
            if self._clock() - started_wait >= MEMORY_WAIT_MAX:
                self._finish(job, FAILED, error=reason, note="メモリが空かないまま%d分待ったため中止しました" % (MEMORY_WAIT_MAX // 60))
                return
            with self._cv:
                if job["state"] != QUEUED or self._jobs.get(job["id"]) is not job or self._closed:
                    return
                if job["wait"] != "memory" or job["note"] != reason:
                    job["wait"], job["note"] = "memory", reason
                    self._bump_locked(tab)
                    changed = True
                else:
                    changed = False
                self._cv.wait(MEMORY_POLL)
            if changed:
                self._changed(tab)
        with self._cv:
            if job["state"] != QUEUED or self._jobs.get(job["id"]) is not job:
                return
            job.update(state=RUNNING, wait=None, note=None, started=self._wall(), attempts=job["attempts"] + 1)
            self._bump_locked(tab)
        self._changed(tab)
        try:
            result = self._execute(tab, job["href"])
        except Exception as error:
            if type(error).__name__ == "TabGone":
                self._finish(job, CANCELLED, note="タブが閉じられたため中止しました")
            else:
                self._finish(job, FAILED, error=_reason(error))
            return
        links = [u for u in (result.get("links") or []) if isinstance(u, str)][:LINK_MAX]
        self._finish(job, DONE, result={"links": links, "final_url": result.get("final_url"),
                                        "redirects": int(result.get("redirects") or 0)},
                     note=(None if links else _no_link_note(result)))

    def _finish(self, job, state, error=None, note=None, result=None):
        tab = job["tab"]
        with self._cv:
            if job["state"] in FINISHED:
                return
            if job.get("_orphan") or self._jobs.get(job["id"]) is not job:
                self._jobs.pop(job["id"], None)     # the tab closed while this ran: nothing to keep
                self._cv.notify_all()
                return
            self._finish_locked(job, state, error=error, note=note, result=result)
            self._bump_locked(tab)
            snap = self._snapshot_locked(job)
            self._cv.notify_all()
        self._changed(tab)
        if state == DONE and self._on_done is not None:
            try:
                self._on_done(snap)
            except Exception:
                pass

    def _finish_locked(self, job, state, error=None, note=None, result=None):
        job["state"], job["wait"] = state, None
        job["error"], job["note"] = error, note
        if result:
            job["links"], job["final_url"], job["redirects"] = result["links"], result["final_url"], result["redirects"]
        job["finished"], job["_finished"] = self._wall(), self._clock()

    # ------------------------------------------------------------------ helpers
    def _bump_locked(self, tab):
        self._rev[tab] = self._rev.get(tab, 0) + 1

    def _changed(self, tab):
        if self._on_change is not None:
            try:
                self._on_change(tab)
            except Exception:
                pass

    def _expire_locked(self, now):
        for key in [k for k, j in self._jobs.items()
                    if j["state"] in FINISHED and j["_finished"] is not None and now - j["_finished"] > RESULT_TTL]:
            del self._jobs[key]

    def _snapshot_locked(self, job):
        position = 0
        if job["state"] in ACTIVE:
            # ordinal among the waiting and running jobs of all tabs (running ones come first): "N件目"
            position = 1
            for other in self._jobs.values():
                if other is job:
                    break
                if other["state"] in ACTIVE:
                    position += 1
        out = {k: v for k, v in job.items() if not k.startswith("_")}
        out["links"] = list(job["links"])
        out["position"] = position
        return out


def _reason(error):
    text = str(error).strip()
    return (text or type(error).__name__)[:300]


def _no_link_note(result):
    ended = result.get("ended")
    if ended == "timeout":
        return "時間内にホスターのリンクへ到達しませんでした"
    return "ホスターのリンクは見つかりませんでした"
