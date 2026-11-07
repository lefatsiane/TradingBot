from __future__ import annotations

import requests

from bot.adapters.heartbeat import HeartbeatPinger


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError("failed")


class FakeSession:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, *, timeout):
        self.calls.append((url, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_heartbeat_ping_handles_http_and_network_failures() -> None:
    session = FakeSession(
        [
            requests.Timeout(),
            FakeResponse(503),
            FakeResponse(200),
        ]
    )
    heartbeat = HeartbeatPinger("https://example.invalid/check", session=session)

    assert heartbeat.ping_once() is False
    assert heartbeat.ping_once() is False
    assert heartbeat.ping_once() is True
    assert len(session.calls) == 3
