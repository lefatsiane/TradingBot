from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from bot.adapters.store import AlertPayload, DecisionRecord, StateStore


def test_store_uses_durable_database_pragmas_and_starts_safe(tmp_path) -> None:
    path = tmp_path / "state.db"
    store = StateStore(path)

    assert store.get_flag("stop_new") is False
    store.set_flag("stop_new", True)
    assert store.get_flag("stop_new") is True
    store.close_thread_connection()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    store = StateStore(path)
    store.close_thread_connection()


def test_intent_must_resolve_without_invalid_status_transitions(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    intent = store.create_intent(
        intent_key="701",
        symbol="EURUSD",
        side="long",
        lots=0.01,
        entry=1.1,
        stop_loss=1.099,
        take_profit=1.102,
        actual_risk=1.25,
        created_at_utc="2026-10-26T12:00:00+00:00",
        code_version="abc123",
        settings_version="def456",
    )
    assert intent.status == "PENDING"
    assert [item.intent_id for item in store.unresolved_intents()] == [intent.intent_id]
    with pytest.raises(ValueError, match="confirmed intent"):
        store.create_trade_from_intent(
            intent_id=intent.intent_id,
            ticket="ticket-1",
            strategy="A",
            timeframe="H1",
            opened_at_utc="2026-10-26T12:00:00+00:00",
        )

    sent = store.transition_intent(intent.intent_id, "SENT")
    assert sent.status == "SENT"
    unknown = store.transition_intent(intent.intent_id, "UNKNOWN")
    assert unknown.status == "UNKNOWN"
    confirmed = store.transition_intent(intent.intent_id, "CONFIRMED", ticket="ticket-1")
    assert confirmed.status == "CONFIRMED"
    trade = store.create_trade_from_intent(
        intent_id=intent.intent_id,
        ticket="ticket-1",
        strategy="A",
        timeframe="H1",
        opened_at_utc="2026-10-26T12:00:00+00:00",
    )
    assert trade.code_version == "abc123"
    assert trade.settings_version == "def456"
    assert trade.actual_risk == pytest.approx(1.25)
    assert store.unresolved_intents() == ()
    with pytest.raises(ValueError, match="Invalid intent transition"):
        store.transition_intent(intent.intent_id, "FAILED")


def test_candle_decisions_and_progress_commit_once(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    decision = DecisionRecord(
        time_utc="2026-10-26T12:00:00+00:00",
        symbol="EURUSD",
        timeframe="H1",
        reason_code="NO_TRIGGER",
        strategy="A",
        code_version="abc123",
        settings_version="def456",
    )
    assert store.commit_candle_decisions(
        symbol="EURUSD",
        timeframe="H1",
        bar_open_utc="2026-10-26T11:00:00+00:00",
        decisions=[decision],
        alert=AlertPayload(
            "2026-10-26T12:00:00+00:00",
            "WARNING",
            "Test alert",
        ),
    )
    assert not store.commit_candle_decisions(
        symbol="EURUSD",
        timeframe="H1",
        bar_open_utc="2026-10-26T11:00:00+00:00",
        decisions=[decision],
        alert=AlertPayload(
            "2026-10-26T12:00:01+00:00",
            "WARNING",
            "Duplicate should not be saved",
        ),
    )
    assert not store.commit_candle_decisions(
        symbol="EURUSD",
        timeframe="H1",
        bar_open_utc="2026-10-26T10:00:00+00:00",
        decisions=[decision],
    )
    row = store._connection().execute("SELECT COUNT(*) AS n FROM decisions").fetchone()
    assert row["n"] == 1
    alerts = store.pending_alerts()
    assert len(alerts) == 1
    assert alerts[0].message == "Test alert"
    store.mark_alert_sent(alerts[0].alert_id, "2026-10-26T12:00:02+00:00")
    assert store.pending_alerts() == ()


def test_store_migrates_v1_database_to_outbox_and_audit_columns(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE trades (ticket TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 1")

    store = StateStore(path)
    store.close_thread_connection()
    store = StateStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(trades)").fetchall()
        }
        assert {"code_version", "settings_version"} <= columns
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'alert_outbox'"
        ).fetchone()


def test_state_store_tracks_trade_closes_and_runtime_meta(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    intent = store.create_intent(
        intent_key="meta-1",
        symbol="EURUSD",
        side="long",
        lots=0.02,
        entry=1.1,
        stop_loss=1.09,
        take_profit=1.12,
        actual_risk=1.0,
        created_at_utc="2026-10-26T12:00:00+00:00",
        code_version="c1",
        settings_version="s1",
    )
    store.transition_intent(intent.intent_id, "SENT")
    store.transition_intent(intent.intent_id, "UNKNOWN")
    store.transition_intent(intent.intent_id, "CONFIRMED", ticket="T-100")
    trade = store.create_trade_from_intent(
        intent_id=intent.intent_id,
        ticket="T-100",
        strategy="A",
        timeframe="H1",
        opened_at_utc="2026-10-26T12:10:00+00:00",
    )
    assert trade.close_reason is None
    assert store.get_bar_progress("EURUSD", "H1") is None
    store.record_start("2026-10-26T12:15:00+00:00", 1)
    store.set_meta("clean_shutdown", "0")
    assert store.get_meta("clean_shutdown") == "0"
    assert store.unplanned_starts_since("2026-10-26T00:00:00+00:00") == 0
    closed = store.close_trade(
        "T-100",
        "2026-10-26T13:00:00+00:00",
        1.11,
        0.50,
        0.05,
        "TAKE_PROFIT",
    )
    assert closed.close_reason == "TAKE_PROFIT"
    assert store.open_trades() == ()
    assert store.realized_pnl_between("2026-10-26T12:00:00+00:00", "2026-10-26T13:00:00+00:00") == pytest.approx(0.45)
    assert store.last_stop_out_utc("EURUSD", "H1") is None


def test_baseline_is_written_once(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.new_baseline("day", "2026-10-26T00:00:00+00:00", 3_000)
    store.new_baseline("day", "2026-10-26T01:00:00+00:00", 2_990)
    assert store.get_baseline("day") == ("2026-10-26T00:00:00+00:00", 3_000)


def test_baseline_rolls_forward_once_at_the_next_period_boundary(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.new_baseline("day", "2026-10-26T00:00:00+00:00", 3_000)
    store.new_baseline("day", "2026-10-26T00:00:00+00:00", 2_900)
    assert store.get_baseline("day") == ("2026-10-26T00:00:00+00:00", 3_000)

    store.new_baseline("day", "2026-10-27T00:00:00+00:00", 2_950)

    assert store.get_baseline("day") == ("2026-10-27T00:00:00+00:00", 2_950)


def test_store_supports_concurrent_thread_writes(tmp_path) -> None:
    store = StateStore(tmp_path / "concurrent.db")
    start = datetime(2026, 10, 26, tzinfo=UTC)

    def write_symbol(symbol: str) -> None:
        try:
            for index in range(10):
                time_utc = (start + timedelta(hours=index)).isoformat()
                decision = DecisionRecord(
                    time_utc=time_utc,
                    symbol=symbol,
                    timeframe="H1",
                    reason_code="NO_TRIGGER",
                    strategy="A",
                    code_version="code",
                    settings_version="settings",
                )
                assert store.commit_candle_decisions(
                    symbol=symbol,
                    timeframe="H1",
                    bar_open_utc=time_utc,
                    decisions=[decision],
                )
        finally:
            store.close_thread_connection()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(write_symbol, ("EURUSD", "GBPUSD")))

    count = store._connection().execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert count == 20
    store.close_thread_connection()


def test_store_rejects_non_utc_timestamps(tmp_path) -> None:
    store = StateStore(tmp_path / "utc-only.db")
    non_utc = "2026-10-26T14:00:00+02:00"

    with pytest.raises(ValueError, match="UTC"):
        store.new_baseline("day", non_utc, 3_000.0)
    with pytest.raises(ValueError, match="UTC"):
        store.record_equity(non_utc, 3_000.0, 3_000.0)
    with pytest.raises(ValueError, match="UTC"):
        store.enqueue_alert(AlertPayload(non_utc, "WARNING", "Local time rejected"))

    store.close_thread_connection()
