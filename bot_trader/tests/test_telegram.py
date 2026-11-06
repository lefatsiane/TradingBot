from __future__ import annotations

from datetime import UTC, datetime, timedelta

import requests

from bot.adapters.store import StateStore
from bot.adapters.telegram import TelegramAdapter, TelegramConflictError
from bot.app.control import ControlHandler
from bot.core.clock import SimClock

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


class FakeResponse:
    def __init__(self, result=None, *, status_code=200, headers=None) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._result = result

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("HTTP failure")

    def json(self):
        return {"ok": True, "result": self._result}


class FakeSession:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _update(update_id: int, chat_id: int, text: str, sent_at: datetime):
    return {
        "update_id": update_id,
        "message": {
            "date": int(sent_at.timestamp()),
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def test_poller_filters_chats_and_edited_messages_and_persists_cursor(tmp_path) -> None:
    store = StateStore(tmp_path / "telegram.db")
    clock = SimClock(NOW)
    control = ControlHandler(store, clock)
    updates = [
        _update(1, 123, "/stop_new", NOW),
        {
            "update_id": 2,
            "edited_message": {
                "chat": {"id": 123, "type": "private"},
                "text": "/resume",
            },
        },
        {
            "update_id": 3,
            "message": {
                "date": int(NOW.timestamp()),
                "chat": {"id": -123, "type": "group"},
                "text": "/resume",
            },
        },
    ]
    session = FakeSession(
        [
            FakeResponse(updates, headers={"Date": "Mon, 26 Oct 2026 12:00:00 GMT"}),
            FakeResponse([], headers={"Date": "Mon, 26 Oct 2026 12:00:00 GMT"}),
        ]
    )
    adapter = TelegramAdapter(
        "fake-token",
        123,
        60,
        store,
        clock,
        control,
        session=session,
    )

    adapter.poll_once()

    assert store.get_flag("stop_new")
    assert store.last_telegram_update_id() == 3
    assert sum(call[1].get("text") == "New entries stopped." for call in session.calls) == 1
    assert len(session.calls) == 2


def test_stale_private_command_gets_expiry_reply(tmp_path) -> None:
    store = StateStore(tmp_path / "telegram.db")
    clock = SimClock(NOW)
    control = ControlHandler(store, clock)
    session = FakeSession(
        [
            FakeResponse(
                [_update(1, 123, "/stop_new", NOW - timedelta(seconds=61))],
                headers={"Date": "Mon, 26 Oct 2026 12:00:00 GMT"},
            ),
            FakeResponse([], headers={"Date": "Mon, 26 Oct 2026 12:00:00 GMT"}),
        ]
    )
    adapter = TelegramAdapter(
        "fake-token", 123, 60, store, clock, control, session=session
    )

    adapter.poll_once()

    assert not store.get_flag("stop_new")
    assert session.calls[1][1]["text"] == "Command expired."


def test_update_cursor_prevents_command_replay_after_restart(tmp_path) -> None:
    store = StateStore(tmp_path / "telegram-restart.db")
    clock = SimClock(NOW)
    control = ControlHandler(store, clock)
    old_update = _update(7, 123, "/stop_new", NOW)
    first = TelegramAdapter(
        "fake-token",
        123,
        60,
        store,
        clock,
        control,
        session=FakeSession([FakeResponse([old_update]), FakeResponse([])]),
    )
    first.poll_once()
    assert store.get_flag("stop_new")

    restarted_session = FakeSession([FakeResponse([old_update])])
    restarted = TelegramAdapter(
        "fake-token",
        123,
        60,
        store,
        clock,
        control,
        session=restarted_session,
    )
    restarted.poll_once()

    assert len(restarted_session.calls) == 1
    assert restarted_session.calls[0][1]["offset"] == 8
    assert store.last_telegram_update_id() == 7


def test_conflict_and_server_clock_drift_are_reported(tmp_path) -> None:
    store = StateStore(tmp_path / "telegram.db")
    clock = SimClock(NOW)
    alerts = []
    session = FakeSession(
        [
            FakeResponse(
                status_code=409,
                headers={"Date": "Mon, 26 Oct 2026 12:00:10 GMT"},
            )
        ]
    )
    adapter = TelegramAdapter(
        "fake-token",
        123,
        60,
        store,
        clock,
        ControlHandler(store, clock),
        session=session,
        on_alert=lambda severity, text: alerts.append((severity, text)),
    )

    try:
        adapter.poll_once()
    except TelegramConflictError:
        pass
    else:
        raise AssertionError("A getUpdates conflict must be surfaced")

    assert any("conflict" in text.lower() for _, text in alerts)
    assert any("clock" in text.lower() for _, text in alerts)


def test_alert_sender_merges_warnings_but_never_suppresses_critical(tmp_path) -> None:
    store = StateStore(tmp_path / "telegram-alerts.db")
    clock = SimClock(NOW)
    session = FakeSession(
        [
            FakeResponse([]),
            FakeResponse([]),
        ]
    )
    adapter = TelegramAdapter(
        "fake-token",
        123,
        60,
        store,
        clock,
        ControlHandler(store, clock),
        session=session,
    )

    assert adapter.send_alert("WARNING", "same warning")
    assert adapter.send_alert("WARNING", "same warning")
    assert adapter.send_alert("CRITICAL", "same warning")

    assert len(session.calls) == 2


def test_failed_alert_is_queued_and_flushed_later(tmp_path) -> None:
    store = StateStore(tmp_path / "telegram-outbox.db")
    clock = SimClock(NOW)
    session = FakeSession(
        [
            requests.Timeout(),
            FakeResponse([]),
        ]
    )
    adapter = TelegramAdapter(
        "fake-token",
        123,
        60,
        store,
        clock,
        ControlHandler(store, clock),
        session=session,
    )

    assert not adapter.send_alert("CRITICAL", "critical test")
    assert len(store.pending_alerts()) == 1
    assert adapter.flush_alerts() == 1
    assert store.pending_alerts() == ()
