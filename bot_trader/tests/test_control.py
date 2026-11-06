from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.adapters.store import StateStore
from bot.app.control import ControlHandler
from bot.core.clock import SimClock


def test_stop_new_and_resume_only_clear_manual_entry_block(tmp_path) -> None:
    store = StateStore(tmp_path / "control.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    control = ControlHandler(store, clock)

    assert control.handle("/stop_new") == "New entries stopped."
    assert store.get_flag("stop_new")
    assert control.handle("/resume") == "Entries resumed."
    assert not store.get_flag("stop_new")


def test_resume_refuses_while_any_safety_halt_is_set(tmp_path) -> None:
    store = StateStore(tmp_path / "control.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    control = ControlHandler(store, clock)
    store.set_flag("stop_new", True)
    store.set_flag("halted_daily", True)

    reply = control.handle("/resume")

    assert "cannot resume" in reply.lower()
    assert store.get_flag("stop_new")
    assert store.get_flag("halted_daily")


def test_close_all_requires_yes_within_30_seconds(tmp_path) -> None:
    store = StateStore(tmp_path / "control.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    control = ControlHandler(store, clock)

    assert control.handle("/close_all") == "Reply YES within 30 seconds to confirm."
    assert not store.get_flag("close_all_pending")
    clock.advance(timedelta(seconds=29))
    assert control.handle("YES") == "Close-all queued; entries stopped."
    assert store.get_flag("stop_new")
    assert store.get_flag("close_all_pending")


def test_close_all_confirmation_expires(tmp_path) -> None:
    store = StateStore(tmp_path / "control.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    control = ControlHandler(store, clock)

    control.handle("/close_all")
    clock.advance(timedelta(seconds=30))

    assert control.handle("YES") == "Confirmation expired."
    assert not store.get_flag("close_all_pending")
    assert not store.get_flag("stop_new")


def test_status_reports_flags_and_unknown_commands_are_rejected(tmp_path) -> None:
    store = StateStore(tmp_path / "control.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    control = ControlHandler(store, clock)
    store.set_flag("stop_new", True)

    assert "stop_new=on" in control.handle("/status")
    assert control.handle("/unknown") == "Unsupported command."
