"""v0.1.59: /api/state revision stays put while only volatile figures change."""
import threading
import unittest

import state_hub
import ttlcache


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeCond:
    """Stands in for threading.Condition: a wait just advances the fake clock."""

    def __init__(self, clock):
        self.clock = clock
        self.waits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def wait(self, timeout=None):
        self.waits += 1
        self.clock.now += timeout or 0

    def notify_all(self):
        pass


class Source:
    def __init__(self, fn):
        self.fn = fn
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        return self.fn(self)


class Fake:
    """A server whose volatile figures change on every read."""

    def __init__(self, clock):
        self.clock = clock
        self.phase = "running"
        self.tabs = []
        self.rows = [{"running": True}]
        self.reason = None
        self.engine_src = Source(self._engine)
        self.rb_src = Source(self._rb)
        outer = self

        class Engine:
            status = staticmethod(outer.engine_src)

        class Jd2:
            @staticmethod
            def status():
                return {"can_send": True, "uptime": outer.clock.now}

            @staticmethod
            def downloads_snapshot(page):
                return {"packages": [dict(r, bytes=outer.clock.now) for r in outer.rows]}

        class Rb:
            status = staticmethod(outer.rb_src)

        class Store:
            @staticmethod
            def revision():
                return 7

        self.engine, self.jd2, self.rb, self.store = Engine, Jd2, Rb, Store

    def _engine(self, src):
        return {"phase": self.phase, "mode": "container", "installed": True, "autostart": True,
                "preset_applied": True, "error": None, "container": {"available": True, "state": "running",
                "checked_at": self.clock.now, "uptime": src.calls}, "steps": [], "components": [],
                "native_leftovers": {"present": False, "bytes": src.calls}, "restart_count": 0,
                "heap_mb": 512, "core_revision": "r1", "now": self.clock.now}

    def _rb(self, src):
        n = src.calls
        return {"phase": "running", "image_ready": True, "tabs": list(self.tabs), "steps": [],
                "memory": {"used_mib": 300 + n, "percent": 20 + (n % 37)}, "available_mib": 900 + n * 13,
                "reason": "NASブラウザのメモリ使用量が%d%%" % (50 + n) if self.reason else None,
                "can_open": True, "container_station": {"available": True, "version": "3"},
                "resident": {"on": True, "state": "resident", "tabs": len(self.tabs), "user_stopped": False,
                             "memory": {"used_mib": 300 + n}, "hub_rss_mib": 50 + n,
                             "idle": {"latest_mib": 200 + n * 7, "samples": n, "at": self.clock.now},
                             "watchdog": {"state": "idle", "next_in_s": n, "restarts": 0, "lost_seq": 0,
                                          "restart_n": 0, "reason": "", "message": "",
                                          "events": [{"at": self.clock.now}]}}}


def make():
    clock = Clock()
    fake = Fake(clock)
    cache = ttlcache.SingleFlightCache(clock=clock)
    hub = state_hub.StateHub(fake, "0.1.59", cache, clock=clock)
    hub._cond = FakeCond(clock)
    return clock, fake, hub


class VolatileFiguresTests(unittest.TestCase):
    def test_volatile_figures_do_not_end_a_long_poll_early(self):
        clock, fake, hub = make()
        rev, _ = hub.poll(None, 0)
        start = clock.now
        again, _ = hub.poll(rev, 20)
        self.assertEqual(again, rev)
        self.assertGreaterEqual(clock.now - start, 20)         # waited the whole time
        self.assertGreater(fake.rb_src.calls, 3)               # ... while the sources did change every read

    def test_rev_is_stable_over_many_refreshes(self):
        clock, fake, hub = make()
        rev, _ = hub.poll(None, 0)
        for _ in range(30):
            clock.now += 1.5
            self.assertEqual(hub.poll(None, 0)[0], rev)

    def test_real_changes_still_return_promptly(self):
        clock, fake, hub = make()
        rev, _ = hub.poll(None, 0)
        for change in (lambda: fake.rows.append({"running": False}),
                       lambda: fake.tabs.append({"id": "t1", "url": "https://a.example.com/", "title": "A"}),
                       lambda: setattr(fake, "reason", "x"),
                       lambda: setattr(fake, "phase", "stopped")):
            change()
            clock.now += 6
            start = clock.now
            new, _ = hub.poll(rev, 20)
            self.assertNotEqual(new, rev)
            self.assertLess(clock.now - start, 2)
            rev = new

    def test_reason_text_with_changing_percent_is_not_a_change(self):
        clock, fake, hub = make()
        fake.reason = "x"
        rev, _ = hub.poll(None, 0)
        for _ in range(5):
            clock.now += 6
            self.assertEqual(hub.poll(None, 0)[0], rev)

    def test_engine_status_is_read_at_most_every_five_seconds_when_stable(self):
        clock, fake, hub = make()
        hub.poll(None, 0)
        before = fake.engine_src.calls
        hub.poll(hub._rev, 20)                                 # 20 fake seconds of waiting
        self.assertLessEqual(fake.engine_src.calls - before, 5)
        fake.phase = "starting"
        clock.now += 6
        hub.poll(None, 0)                                      # sees "starting": from now on 2 s
        before = fake.engine_src.calls
        hub.poll(hub._rev, 4)
        self.assertGreaterEqual(fake.engine_src.calls - before, 2)

    def test_bucket(self):
        self.assertEqual(state_hub._bucket(47.9, 10), 40)
        self.assertEqual(state_hub._bucket(199, 50), 150)
        self.assertIsNone(state_hub._bucket(None, 10))


if __name__ == "__main__":
    unittest.main()
