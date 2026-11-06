from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.adapters.store import StateStore
from bot.app.runtime import CrashLoopGuard
from bot.core.clock import SimClock


def test_third_unplanned_start_sets_stop_new_and_alerts(tmp_path) -> None:
    store = StateStore(tmp_path / "runtime.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    guard = CrashLoopGuard(store, clock)

    assert not guard.record_start(planned=False)
    clock.advance(timedelta(minutes=1))
    assert not guard.record_start(planned=False)
    clock.advance(timedelta(minutes=1))

    assert guard.record_start(planned=False)
    assert store.get_flag("stop_new")
    alerts = store.pending_alerts()
    assert len(alerts) == 1
    assert alerts[0].severity == "CRITICAL"


def test_old_and_planned_starts_do_not_count_toward_crash_loop(tmp_path) -> None:
    store = StateStore(tmp_path / "runtime.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    guard = CrashLoopGuard(store, clock)

    assert not guard.record_start(planned=False)
    clock.advance(timedelta(hours=2))
    assert not guard.record_start(planned=True)
    clock.advance(timedelta(seconds=1))
    assert not guard.record_start(planned=False)
