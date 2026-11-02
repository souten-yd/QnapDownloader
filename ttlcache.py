"""Short-lived RAM caches with single-flight loading (v0.1.33).

Concurrent callers asking for the same key share ONE in-flight load: the first caller runs the
loader, the others wait for its result (or its exception). A successful result is reused for
`ttl` seconds. Nothing is written to disk and failures are never cached.
"""
import threading
import time


class _Flight:
    __slots__ = ("done", "value", "error")

    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.error = None


class SingleFlightCache:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._values = {}      # key -> (at, value)
        self._flights = {}     # key -> _Flight
        self._epoch = 0        # bumped by invalidate(): a load started before it is not stored

    def get(self, key, ttl, loader):
        while True:
            with self._lock:
                hit = self._values.get(key)
                if hit is not None and self._clock() - hit[0] < ttl:
                    return hit[1]
                flight = self._flights.get(key)
                leader = flight is None
                if leader:
                    flight = self._flights[key] = _Flight()
                    epoch = self._epoch
            if not leader:
                flight.done.wait()
                if flight.error is not None:
                    raise flight.error
                return flight.value
            try:
                value = loader()
            except BaseException as error:
                with self._lock:
                    self._flights.pop(key, None)
                flight.error = error
                flight.done.set()
                raise
            with self._lock:
                self._flights.pop(key, None)
                if epoch == self._epoch:
                    self._values[key] = (self._clock(), value)
            flight.value = value
            flight.done.set()
            return value

    def invalidate(self, prefix=None):
        """Forget cached values (all, or keys starting with `prefix`)."""
        with self._lock:
            self._epoch += 1
            if prefix is None:
                self._values.clear()
            else:
                for key in [k for k in self._values if k.startswith(prefix)]:
                    del self._values[key]
