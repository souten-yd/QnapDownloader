"""v0.1.65: the long-press analysis queue (rb_probe_queue.py) with fakes: no browser, no network."""
import threading
import time
import unittest

import rb_probe_queue as q


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeWork:
    """Stands in for RemoteBrowser._probe_job_execute: records the order and the overlap of the runs."""

    def __init__(self):
        self.runs = []
        self.active = 0
        self.max_active = 0
        self.fail = {}
        self.result = {}
        self.hold = None
        self.lock = threading.Lock()

    def __call__(self, tab, href):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.runs.append((tab, href))
        try:
            if self.hold is not None:
                self.hold.wait(10)
            if href in self.fail:
                raise self.fail[href]
            return self.result.get(href) or {"links": ["https://rapidgator.net/file/" + "a" * 32], "final_url": href,
                                             "redirects": 1}
        finally:
            with self.lock:
                self.active -= 1


def make(work=None, **extra):
    clock = extra.pop("clock", None) or Clock()
    work = work or FakeWork()
    changes, done = [], []
    queue = q.ProbeQueue(work, clock=clock, wall=lambda: 1_700_000_000.0, start=extra.pop("start", False),
                         on_change=changes.append, on_done=done.append, **extra)
    return queue, work, clock, changes, done


def href(n):
    return "https://example.org/go/%d" % n


class EnqueueTests(unittest.TestCase):
    def test_enqueue_returns_at_once_without_running_anything(self):
        queue, work, _, changes, _ = make()
        job, existing = queue.enqueue("b1", href(1), "リンク1")
        self.assertFalse(existing)
        self.assertEqual((job["state"], job["position"], job["label"], job["host"]), ("queued", 1, "リンク1", "example.org"))
        self.assertEqual(work.runs, [])
        self.assertEqual(changes, ["b1"])
        second, _ = queue.enqueue("b1", href(2))
        self.assertEqual((second["position"], second["label"]), (2, "example.org"))     # no text: the host names the job
        self.assertEqual(queue.summary("b1")["queued"], 2)

    def test_the_same_link_of_the_same_tab_is_one_job(self):
        queue, work, clock, _, _ = make()
        first, _ = queue.enqueue("b1", href(1))
        again, existing = queue.enqueue("b1", href(1))
        self.assertTrue(existing)
        self.assertEqual(again["id"], first["id"])
        other_tab, existing = queue.enqueue("b2", href(1))
        self.assertFalse(existing)
        self.assertNotEqual(other_tab["id"], first["id"])
        queue.run_pending()
        self.assertEqual(len(work.runs), 2)
        # finished a moment ago: still the same job; a minute later a new one
        self.assertTrue(queue.enqueue("b1", href(1))[1])
        clock.now += q.DONE_DEDUPE + 1
        self.assertFalse(queue.enqueue("b1", href(1))[1])

    def test_a_failed_job_is_not_reused_by_a_new_press(self):
        queue, work, _, _, _ = make()
        work.fail[href(1)] = RuntimeError("boom")
        queue.enqueue("b1", href(1))
        queue.run_pending()
        job, existing = queue.enqueue("b1", href(1))
        self.assertFalse(existing)
        self.assertEqual(job["state"], "queued")

    def test_capacity_refuses_only_when_everything_is_still_active(self):
        queue, _, _, _, _ = make()
        for n in range(q.QUEUE_CAP):
            queue.enqueue("b1", href(n))
        with self.assertRaises(q.QueueFull):
            queue.enqueue("b1", href(q.QUEUE_CAP))
        self.assertEqual(queue.summary("b1")["queued"], q.QUEUE_CAP)
        # a finished job makes room (the oldest finished one goes)
        queue2, work2, _, _, _ = make()
        for n in range(q.QUEUE_CAP):
            queue2.enqueue("b1", href(n))
        queue2.cancel("b1", queue2.jobs("b1")[0]["id"])
        job, _ = queue2.enqueue("b1", href(10_000))
        self.assertEqual(job["state"], "queued")
        self.assertEqual(len(queue2.jobs("b1")), q.QUEUE_CAP)

    def test_label_is_cleaned_and_cut(self):
        self.assertEqual(q.clean_label("  a\n\tb\x00c  ", "https://h.example/"), "a b c")
        self.assertEqual(len(q.clean_label("x" * 500, "https://h.example/")), q.LABEL_MAX)
        self.assertEqual(q.clean_label(None, "https://h.example/x"), "h.example")
        self.assertEqual(q.clean_label(7, "https://h.example/x"), "h.example")


class RunTests(unittest.TestCase):
    def test_jobs_run_one_after_another_in_order_and_keep_their_results(self):
        queue, work, _, _, done = make()
        for n in range(1, 13):
            queue.enqueue("b1", href(n), "リンク%d" % n)
        queue.run_pending()
        self.assertEqual([r[1] for r in work.runs], [href(n) for n in range(1, 13)])
        self.assertEqual(work.max_active, 1)
        jobs = queue.jobs("b1")
        self.assertEqual([j["state"] for j in jobs], ["done"] * 12)
        self.assertTrue(all(j["links"] and j["started"] and j["finished"] for j in jobs))
        self.assertEqual(queue.summary("b1")["done"], 12)
        self.assertEqual(len(done), 12)                         # the on_done hook ran for every finished job
        self.assertTrue(queue.idle())

    def test_the_threaded_worker_is_sequential_too(self):
        queue, work, _, _, _ = make(start=True)
        work.hold = threading.Event()
        for n in range(1, 7):
            queue.enqueue("b1", href(n))
        time.sleep(0.3)
        self.assertEqual(work.active, 1)                        # one is running, five wait
        self.assertEqual(queue.summary("b1")["running"], 1)
        work.hold.set()
        self.assertTrue(queue.wait_idle(10))
        self.assertEqual(work.max_active, 1)
        self.assertEqual(queue.summary("b1")["done"], 6)
        queue.close()

    def test_concurrency_can_be_raised_but_defaults_to_one(self):
        self.assertEqual(q.CONCURRENCY, 1)
        queue, work, _, _, _ = make(start=True, concurrency=2)
        work.hold = threading.Event()
        for n in range(1, 5):
            queue.enqueue("b1", href(n))
        deadline = time.time() + 5
        while work.active < 2 and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)
        self.assertEqual(work.max_active, 2)
        work.hold.set()
        self.assertTrue(queue.wait_idle(10))
        self.assertEqual(sorted(r[1] for r in work.runs), [href(n) for n in range(1, 5)])      # nothing ran twice
        queue.close()

    def test_a_failure_keeps_its_reason_and_does_not_stop_the_others(self):
        queue, work, _, _, done = make()
        work.fail[href(2)] = RuntimeError("リンク先の確認用ページを作成できません")
        for n in (1, 2, 3):
            queue.enqueue("b1", href(n))
        queue.run_pending()
        states = {j["href"]: j for j in queue.jobs("b1")}
        self.assertEqual(states[href(2)]["state"], "failed")
        self.assertEqual(states[href(2)]["error"], "リンク先の確認用ページを作成できません")
        self.assertEqual((states[href(1)]["state"], states[href(3)]["state"]), ("done", "done"))
        self.assertEqual(len(done), 2)                          # on_done is for results only
        self.assertEqual(queue.summary("b1")["failed"], 1)

    def test_no_link_found_is_a_finished_job_with_a_note(self):
        queue, work, _, _, _ = make()
        work.result[href(1)] = {"links": [], "final_url": "https://x.example/end", "redirects": 2, "ended": "timeout"}
        queue.enqueue("b1", href(1))
        queue.run_pending()
        job = queue.jobs("b1")[0]
        self.assertEqual((job["state"], job["links"], job["final_url"]), ("done", [], "https://x.example/end"))
        self.assertIn("時間内", job["note"])

    def test_retry_requeues_failed_jobs_behind_the_others(self):
        queue, work, _, _, _ = make()
        work.fail[href(1)] = RuntimeError("boom")
        for n in (1, 2):
            queue.enqueue("b1", href(n))
        queue.run_pending()
        del work.fail[href(1)]
        self.assertEqual(queue.retry("b1"), 1)
        job = [j for j in queue.jobs("b1") if j["href"] == href(1)][0]
        self.assertEqual((job["state"], job["error"], job["attempts"]), ("queued", None, 1))
        queue.enqueue("b1", href(3))
        self.assertEqual([j["href"] for j in queue.jobs("b1")][-2:], [href(1), href(3)])
        queue.run_pending()
        self.assertEqual([j["state"] for j in queue.jobs("b1")], ["done", "done", "done"])
        self.assertEqual(queue.retry("b1"), 0)                  # nothing failed any more
        self.assertEqual([r[1] for r in work.runs], [href(1), href(2), href(1), href(3)])

    def test_cancel_removes_waiting_jobs_only(self):
        queue, work, _, changes, _ = make(start=True)
        work.hold = threading.Event()
        for n in range(1, 5):
            queue.enqueue("b1", href(n))
        time.sleep(0.2)                                         # job 1 is running
        self.assertEqual(queue.cancel("b1"), 3)
        self.assertEqual(queue.summary("b1")["running"], 1)
        work.hold.set()
        self.assertTrue(queue.wait_idle(10))
        self.assertEqual([r[1] for r in work.runs], [href(1)])
        states = [j["state"] for j in queue.jobs("b1")]
        self.assertEqual(states, ["done", "cancelled", "cancelled", "cancelled"])
        self.assertEqual(queue.summary("b1")["total"], 1)       # cancelled jobs are not counted
        queue.close()

    def test_cancel_one_job(self):
        queue, work, _, _, _ = make()
        first, _ = queue.enqueue("b1", href(1))
        second, _ = queue.enqueue("b1", href(2))
        self.assertEqual(queue.cancel("b1", second["id"]), 1)
        self.assertEqual(queue.cancel("b1", second["id"]), 0)
        queue.run_pending()
        self.assertEqual([r[1] for r in work.runs], [href(1)])

    def test_closing_a_tab_cancels_its_jobs_and_drops_its_results(self):
        queue, work, _, _, _ = make()
        queue.enqueue("b1", href(1))
        queue.enqueue("b2", href(2))
        queue.run_pending()
        queue.enqueue("b1", href(3))
        self.assertEqual(queue.tab_closed("b1"), 2)
        self.assertEqual(queue.jobs("b1"), [])
        queue.run_pending()
        self.assertEqual([r[1] for r in work.runs], [href(1), href(2)])        # href(3) never ran
        self.assertEqual(len(queue.jobs("b2")), 1)

    def test_a_tab_that_is_gone_when_its_turn_comes_is_not_probed(self):
        alive = {"b1": True}
        queue, work, _, _, _ = make(tab_alive=lambda tab: alive.get(tab, False))
        queue.enqueue("b1", href(1))
        alive["b1"] = False
        queue.run_pending()
        self.assertEqual(work.runs, [])
        self.assertEqual(queue.jobs("b1")[0]["state"], "cancelled")

    def test_a_running_job_of_a_closed_tab_leaves_nothing_behind(self):
        queue, work, _, _, done = make(start=True)
        work.hold = threading.Event()
        queue.enqueue("b1", href(1))
        time.sleep(0.2)
        queue.tab_closed("b1")
        work.hold.set()
        self.assertTrue(queue.wait_idle(10))
        self.assertEqual(queue.jobs("b1"), [])
        self.assertEqual(done, [])
        queue.close()

    def test_tab_gone_error_while_running_is_a_cancel_not_a_failure(self):
        class TabGone(Exception):
            pass
        queue, work, _, _, _ = make()
        work.fail[href(1)] = TabGone("このタブはすでに閉じられています")
        queue.enqueue("b1", href(1))
        queue.run_pending()
        self.assertEqual(queue.jobs("b1")[0]["state"], "cancelled")

    def test_dismiss_forgets_finished_jobs(self):
        queue, work, _, _, _ = make()
        work.fail[href(2)] = RuntimeError("x")
        for n in (1, 2, 3):
            queue.enqueue("b1", href(n))
        queue.run_pending()
        queue.enqueue("b1", href(4))
        self.assertEqual(queue.dismiss("b1"), 3)
        self.assertEqual([j["href"] for j in queue.jobs("b1")], [href(4)])

    def test_finished_jobs_expire_after_the_ttl(self):
        queue, _, clock, _, _ = make()
        queue.enqueue("b1", href(1))
        queue.run_pending()
        clock.now += q.RESULT_TTL - 1
        self.assertEqual(len(queue.jobs("b1")), 1)
        clock.now += 2
        self.assertEqual(queue.jobs("b1"), [])

    def test_revision_changes_with_every_change_and_summary_counts_review(self):
        queue, work, _, _, _ = make()
        r0 = queue.revision("b1")
        job, _ = queue.enqueue("b1", href(1))
        r1 = queue.revision("b1")
        queue.run_pending()
        r2 = queue.revision("b1")
        self.assertTrue(r0 < r1 < r2)
        summary = queue.summary("b1")
        self.assertEqual((summary["done"], summary["review"], summary["rev"]), (1, 1, r2))      # links nobody sent yet
        queue.mark_sent("b1", job["id"], {"sent": 1, "skipped": 0, "total": 1, "error": None})
        self.assertEqual(queue.summary("b1")["review"], 0)
        self.assertGreater(queue.revision("b1"), r2)
        queue.mark_sent("b1", job["id"], {"sent": 0, "skipped": 0, "total": 1, "error": "JD2が使えません"})
        self.assertEqual(queue.summary("b1")["review"], 1)      # a failed send needs the user again


class MemoryWaitTests(unittest.TestCase):
    def test_a_job_waits_for_the_memory_guard_instead_of_failing(self):
        reasons = ["空きメモリが不足しています"] * 3 + [None]
        queue, work, clock, changes, _ = make(memory_reason=lambda: reasons.pop(0) if reasons else None)
        queue.enqueue("b1", href(1))
        seen = []

        original = queue._cv.wait

        def wait(timeout=None):
            seen.append(queue.jobs("b1")[0]["wait"])
            clock.now += 5.0                                     # time passes while the job waits
            return original(0)
        queue._cv.wait = wait
        queue.run_pending()
        self.assertEqual(seen[:3], ["memory"] * 3)
        job = queue.jobs("b1")[0]
        self.assertEqual((job["state"], job["wait"], job["error"]), ("done", None, None))
        self.assertEqual(len(work.runs), 1)

    def test_memory_wait_is_shown_and_lasts_far_longer_than_the_old_30_seconds(self):
        queue, work, clock, _, _ = make(memory_reason=lambda: "メモリ使用率が高い")
        queue.enqueue("b1", href(1))
        original = queue._cv.wait
        steps = []

        def wait(timeout=None):
            clock.now += 10.0
            steps.append(clock.now)
            if len(steps) == 12:          # two minutes later the memory is back
                queue._memory_reason = lambda: None
            return original(0)
        queue._cv.wait = wait
        queue.run_pending()
        self.assertEqual(queue.jobs("b1")[0]["state"], "done")
        self.assertGreater(steps[-1] - steps[0], 100)

    def test_waiting_for_memory_too_long_fails_with_the_reason(self):
        queue, work, clock, _, _ = make(memory_reason=lambda: "NASブラウザのメモリ使用量が上限の85%に達しています（92%）")
        queue.enqueue("b1", href(1))
        original = queue._cv.wait

        def wait(timeout=None):
            clock.now += 60.0
            return original(0)
        queue._cv.wait = wait
        queue.run_pending()
        job = queue.jobs("b1")[0]
        self.assertEqual(job["state"], "failed")
        self.assertIn("85%", job["error"])
        self.assertIn("10分", job["note"])
        self.assertEqual(work.runs, [])

    def test_a_cancel_during_the_memory_wait_ends_it(self):
        queue, work, clock, _, _ = make(memory_reason=lambda: "メモリ不足")
        job, _ = queue.enqueue("b1", href(1))
        original = queue._cv.wait

        def wait(timeout=None):
            queue._cv.release()
            try:
                queue.cancel("b1", job["id"])
            finally:
                queue._cv.acquire()
            return original(0)
        queue._cv.wait = wait
        queue.run_pending()
        self.assertEqual(queue.jobs("b1")[0]["state"], "cancelled")
        self.assertEqual(work.runs, [])


if __name__ == "__main__":
    unittest.main()
