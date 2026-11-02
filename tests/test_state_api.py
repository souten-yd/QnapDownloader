"""v0.1.33: GET /api/state (unified client state), single-flight caches."""
import threading
import time
import unittest

import ttlcache


class SingleFlightTests(unittest.TestCase):
    def test_concurrent_callers_share_one_load_and_ttl_applies(self):
        cache = ttlcache.SingleFlightCache()
        calls = []
        gate = threading.Event()

        def loader():
            calls.append(1)
            gate.wait(2)
            return len(calls)

        results = []
        threads = [threading.Thread(target=lambda: results.append(cache.get("k", 5, loader))) for _ in range(6)]
        for t in threads:
            t.start()
        time.sleep(0.1)
        gate.set()
        for t in threads:
            t.join()
        self.assertEqual(calls, [1])
        self.assertEqual(results, [1] * 6)
        self.assertEqual(cache.get("k", 5, loader), 1)         # within the TTL: no new load
        cache.invalidate()
        self.assertEqual(cache.get("k", 5, loader), 2)

    def test_errors_are_shared_but_never_cached(self):
        cache = ttlcache.SingleFlightCache()
        with self.assertRaises(ValueError):
            cache.get("k", 5, lambda: (_ for _ in ()).throw(ValueError("x")))
        self.assertEqual(cache.get("k", 5, lambda: 7), 7)

    def test_a_load_that_started_before_invalidate_is_not_stored(self):
        cache = ttlcache.SingleFlightCache()
        started, release = threading.Event(), threading.Event()

        def slow():
            started.set()
            release.wait(2)
            return "old"

        out = []
        t = threading.Thread(target=lambda: out.append(cache.get("k", 5, slow)))
        t.start()
        started.wait(2)
        cache.invalidate()
        release.set()
        t.join()
        self.assertEqual(out, ["old"])
        self.assertEqual(cache.get("k", 5, lambda: "new"), "new")


if __name__ == "__main__":
    unittest.main()
