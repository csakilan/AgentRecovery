"""A small heartbeat that never shares the caller's in-flight transaction."""

import math
import threading
from collections.abc import Callable

from lab.execution.types import LeaseLost


class LeaseHeartbeat:
    def __init__(self, renew: Callable[[], bool], *, interval_s: float = 10.0):
        if not math.isfinite(interval_s) or interval_s <= 0:
            raise ValueError("heartbeat interval must be finite and positive")
        self.renew = renew
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def tick_once(self) -> bool:
        if self._lost.is_set():
            return False
        try:
            alive = self.renew()
        except Exception:
            alive = False
        if not alive:
            self._lost.set()
        return alive

    def check(self) -> None:
        if self._lost.is_set():
            raise LeaseLost("heartbeat could not renew ownership")

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            if not self.tick_once():
                return

    def __enter__(self):
        self.check()
        self._thread = threading.Thread(target=self._run, daemon=True, name="lease-heartbeat")
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
