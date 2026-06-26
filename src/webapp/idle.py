"""Idle auto-stop: after N idle minutes, set the ECS service desiredCount=0.

The app can stop itself (it is up when the timer fires) but cannot wake itself
(once at 0 there is no task to serve the wake) — wake is out-of-band via
deploy/wake.sh. Default: disabled (minutes=None)."""
from __future__ import annotations

import threading
import time


class IdleMonitor:
    def __init__(self, ecs_client, cluster: str, service: str, *, now=time.time):
        self._ecs = ecs_client
        self._cluster = cluster
        self._service = service
        self._now = now
        self._minutes: float | None = None
        self._last = now()
        self._lock = threading.Lock()

    def touch(self) -> None:
        with self._lock:
            self._last = self._now()

    def set_minutes(self, minutes: float | None) -> None:
        with self._lock:
            self._minutes = minutes
            self._last = self._now()

    def get_minutes(self) -> float | None:
        return self._minutes

    def should_stop(self, now: float | None = None) -> bool:
        if self._minutes is None:
            return False
        now = self._now() if now is None else now
        return (now - self._last) >= self._minutes * 60.0

    def maybe_stop(self) -> bool:
        if not self.should_stop():
            return False
        self._ecs.update_service(cluster=self._cluster, service=self._service,
                                 desiredCount=0)
        return True
