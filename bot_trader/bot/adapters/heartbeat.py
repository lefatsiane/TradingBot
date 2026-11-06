from __future__ import annotations

import threading
from typing import Protocol

import requests


class _Response(Protocol):
    def raise_for_status(self) -> None: ...


class _Session(Protocol):
    def get(self, url: str, *, timeout: float) -> _Response: ...


class HeartbeatPinger:
    def __init__(
        self,
        url: str,
        *,
        session: _Session | None = None,
        interval_s: float = 60.0,
        timeout_s: float = 10.0,
    ) -> None:
        if not url:
            raise ValueError("Healthcheck URL is required")
        if interval_s <= 0 or timeout_s <= 0:
            raise ValueError("Heartbeat intervals and timeouts must be positive")
        self._url = url
        self._session = session or requests.Session()
        self._interval_s = interval_s
        self._timeout_s = timeout_s
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.run,
            name="healthcheck-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("Heartbeat thread did not stop in time")
            self._thread = None

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.ping_once()
            if self._stop_event.wait(self._interval_s):
                break

    def ping_once(self) -> bool:
        try:
            response = self._session.get(self._url, timeout=self._timeout_s)
            response.raise_for_status()
        except (requests.RequestException, ConnectionError, TimeoutError):
            return False
        return True
