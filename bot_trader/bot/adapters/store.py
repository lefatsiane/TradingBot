from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from bot.core.types import ExitReason

IntentStatus = Literal["PENDING", "SENT", "CONFIRMED", "FAILED", "UNKNOWN"]
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"SENT", "FAILED", "UNKNOWN"},
    "SENT": {"CONFIRMED", "FAILED", "UNKNOWN"},
    "UNKNOWN": {"CONFIRMED", "FAILED"},
    "CONFIRMED": set(),
    "FAILED": set(),
}
_FLAGS = (
    "stop_new",
    "close_all_pending",
    "halted_daily",
    "halted_monthly",
    "halted_error",
)


def _validate_utc_timestamp(value: str, field_name: str) -> None:
    if not value:
        raise ValueError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO 8601 UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be a UTC timestamp")


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    time_utc: str
    symbol: str
    timeframe: str
    reason_code: str
    strategy: str
    code_version: str
    settings_version: str


@dataclass(frozen=True, slots=True)
class IntentRecord:
    intent_id: int
    intent_key: str
    symbol: str
    side: str
    lots: float
    entry: float
    stop_loss: float
    take_profit: float
    status: IntentStatus
    created_at_utc: str
    ticket: str | None


@dataclass(frozen=True, slots=True)
class AlertPayload:
    created_at_utc: str
    severity: str
    message: str


@dataclass(frozen=True, slots=True)
class AlertRecord:
    alert_id: int
    created_at_utc: str
    severity: str
    message: str


@dataclass(frozen=True, slots=True)
class TradeRecord:
    ticket: str
    intent_id: int
    symbol: str
    strategy: str
    timeframe: str
    side: str
    lots: float
    entry: float
    stop_loss: float
    take_profit: float
    actual_risk: float
    opened_at_utc: str
    code_version: str
    settings_version: str
    close_reason: str | None = None
    closed_at_utc: str | None = None
    close_price: float | None = None
    commission: float | None = None
    result_r: float | None = None
    candles_held: int | None = None


class StateStore:
    """Thread-local SQLite connections with durable intent and state writes."""

    SCHEMA_VERSION = 3

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connections = threading.local()
        connection = self._connection()
        self._migrate(connection)

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._connections, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            self._connections.connection = connection
        return connection

    def _migrate(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > self.SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema {version} is newer than supported {self.SCHEMA_VERSION}"
            )

        if version == 0:
            connection.executescript(
                """
                CREATE TABLE flags (
                    name TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
                );
                CREATE TABLE baselines (
                    period TEXT PRIMARY KEY CHECK (period IN ('day', 'month')),
                    starts_at_utc TEXT NOT NULL,
                    equity REAL NOT NULL CHECK (equity > 0)
                );
                CREATE TABLE bar_progress (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    last_open_utc TEXT NOT NULL,
                    PRIMARY KEY (symbol, timeframe)
                );
                CREATE TABLE intents (
                    intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_key TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('long', 'short')),
                    lots REAL NOT NULL CHECK (lots > 0),
                    entry REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    actual_risk REAL NOT NULL CHECK (actual_risk >= 0),
                    status TEXT NOT NULL CHECK (
                        status IN ('PENDING', 'SENT', 'CONFIRMED', 'FAILED', 'UNKNOWN')
                    ),
                    ticket TEXT,
                    created_at_utc TEXT NOT NULL,
                    code_version TEXT NOT NULL,
                    settings_version TEXT NOT NULL
                );
                CREATE TABLE trades (
                    ticket TEXT PRIMARY KEY,
                    intent_id INTEGER NOT NULL UNIQUE REFERENCES intents(intent_id),
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('long', 'short')),
                    lots REAL NOT NULL CHECK (lots > 0),
                    entry REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    actual_risk REAL NOT NULL CHECK (actual_risk >= 0),
                    opened_at_utc TEXT NOT NULL,
                    closed_at_utc TEXT,
                    close_price REAL,
                    close_reason TEXT,
                    commission REAL,
                    result_r REAL,
                    candles_held INTEGER,
                    code_version TEXT NOT NULL,
                    settings_version TEXT NOT NULL
                );
                CREATE TABLE decisions (
                    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time_utc TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    code_version TEXT NOT NULL,
                    settings_version TEXT NOT NULL
                );
                CREATE TABLE equity_log (
                    time_utc TEXT PRIMARY KEY,
                    balance REAL NOT NULL,
                    equity REAL NOT NULL
                );
                CREATE TABLE telegram (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    last_update_id INTEGER NOT NULL
                );
                CREATE TABLE alert_outbox (
                    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_utc TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK (
                        severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')
                    ),
                    message TEXT NOT NULL,
                    sent_at_utc TEXT
                );
                CREATE TABLE starts (
                    started_at_utc TEXT PRIMARY KEY,
                    planned INTEGER NOT NULL CHECK (planned IN (0, 1))
                );
                CREATE TABLE meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO flags(name, enabled) VALUES (?, 0)",
                ((flag,) for flag in _FLAGS),
            )
            connection.execute("INSERT INTO telegram(singleton, last_update_id) VALUES (1, 0)")
            version = 0

        if version < 2:
            trade_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(trades)")
            }
            if "code_version" not in trade_columns:
                connection.execute(
                    "ALTER TABLE trades ADD COLUMN code_version TEXT NOT NULL DEFAULT ''"
                )
            if "settings_version" not in trade_columns:
                connection.execute(
                    "ALTER TABLE trades ADD COLUMN settings_version TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_outbox (
                    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_utc TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK (
                        severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')
                    ),
                    message TEXT NOT NULL,
                    sent_at_utc TEXT
                )
                """
            )
            version = 2

        if version < 3:
            trade_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(trades)")
            }
            if "close_reason" not in trade_columns:
                connection.execute("ALTER TABLE trades ADD COLUMN close_reason TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS starts (
                    started_at_utc TEXT PRIMARY KEY,
                    planned INTEGER NOT NULL CHECK (planned IN (0, 1))
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            version = 3

        connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        connection.commit()

    def close_thread_connection(self) -> None:
        connection = getattr(self._connections, "connection", None)
        if connection is not None:
            connection.close()
            del self._connections.connection

    def get_flag(self, name: str) -> bool:
        if name not in _FLAGS:
            raise ValueError(f"Unknown safety flag: {name}")
        row = (
            self._connection()
            .execute("SELECT enabled FROM flags WHERE name = ?", (name,))
            .fetchone()
        )
        if row is None:
            raise RuntimeError(f"Missing safety flag in state store: {name}")
        return bool(row["enabled"])

    def set_flag(self, name: str, enabled: bool) -> None:
        if name not in _FLAGS:
            raise ValueError(f"Unknown safety flag: {name}")
        with self._connection() as connection:
            connection.execute(
                "UPDATE flags SET enabled = ? WHERE name = ?",
                (int(enabled), name),
            )

    def new_baseline(
        self, period: Literal["day", "month"], starts_at_utc: str, equity: float
    ) -> None:
        _validate_utc_timestamp(starts_at_utc, "starts_at_utc")
        if equity <= 0:
            raise ValueError("Baseline equity must be positive")
        with self._connection() as connection:
            current = connection.execute(
                "SELECT starts_at_utc FROM baselines WHERE period = ?",
                (period,),
            ).fetchone()
            candidate = datetime.fromisoformat(starts_at_utc)
            if current is None:
                connection.execute(
                    "INSERT INTO baselines(period, starts_at_utc, equity) VALUES (?, ?, ?)",
                    (period, starts_at_utc, equity),
                )
                return
            previous = datetime.fromisoformat(current["starts_at_utc"])
            is_new_period = (
                candidate.date() > previous.date()
                if period == "day"
                else (candidate.year, candidate.month)
                > (previous.year, previous.month)
            )
            if is_new_period:
                connection.execute(
                    """
                    UPDATE baselines
                    SET starts_at_utc = ?, equity = ?
                    WHERE period = ?
                    """,
                    (starts_at_utc, equity, period),
                )

    def get_baseline(self, period: Literal["day", "month"]) -> tuple[str, float] | None:
        row = (
            self._connection()
            .execute(
                "SELECT starts_at_utc, equity FROM baselines WHERE period = ?",
                (period,),
            )
            .fetchone()
        )
        return None if row is None else (row["starts_at_utc"], row["equity"])

    def open_trades(self) -> tuple[TradeRecord, ...]:
        rows = self._connection().execute(
            """
            SELECT *
            FROM trades
            WHERE closed_at_utc IS NULL
            ORDER BY opened_at_utc, ticket
            """
        ).fetchall()
        return tuple(self._trade_from_row(row) for row in rows)

    def get_bar_progress(self, symbol: str, timeframe: str) -> str | None:
        row = self._connection().execute(
            "SELECT last_open_utc FROM bar_progress WHERE symbol = ? AND timeframe = ?",
            (symbol, timeframe),
        ).fetchone()
        return None if row is None else row["last_open_utc"]

    def realized_pnl_between(self, start_utc: str, end_utc: str) -> float:
        _validate_utc_timestamp(start_utc, "start_utc")
        _validate_utc_timestamp(end_utc, "end_utc")
        if start_utc > end_utc:
            raise ValueError("start_utc must be earlier than or equal to end_utc")
        row = self._connection().execute(
            """
            SELECT COALESCE(SUM(CASE
                WHEN closed_at_utc IS NOT NULL
                 AND closed_at_utc >= ?
                 AND closed_at_utc <= ?
                THEN COALESCE(result_r, 0.0) - COALESCE(commission, 0.0)
                ELSE 0.0
            END), 0.0) AS pnl
            FROM trades
            """,
            (start_utc, end_utc),
        ).fetchone()
        return float(row["pnl"])

    def last_stop_out_utc(self, symbol: str, timeframe: str) -> str | None:
        row = self._connection().execute(
            """
            SELECT MAX(closed_at_utc) AS last_stop_out_utc
            FROM trades
            WHERE symbol = ?
              AND timeframe = ?
              AND close_reason IN ('STOP_LOSS', 'TIME_STOP')
            """,
            (symbol, timeframe),
        ).fetchone()
        return None if row is None or row["last_stop_out_utc"] is None else row["last_stop_out_utc"]

    def get_intent(self, intent_id: int) -> IntentRecord:
        row = self._connection().execute(
            "SELECT * FROM intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Intent {intent_id} does not exist")
        return self._intent_from_row(row)

    def find_intent_by_key(self, intent_key: str) -> IntentRecord | None:
        row = self._connection().execute(
            "SELECT * FROM intents WHERE intent_key = ?",
            (intent_key,),
        ).fetchone()
        return None if row is None else self._intent_from_row(row)

    def record_start(self, at_utc: str, planned: int) -> None:
        _validate_utc_timestamp(at_utc, "at_utc")
        if planned not in (0, 1):
            raise ValueError("planned must be 0 or 1")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO starts(started_at_utc, planned) VALUES (?, ?)",
                (at_utc, planned),
            )

    def unplanned_starts_since(self, since_utc: str) -> int:
        _validate_utc_timestamp(since_utc, "since_utc")
        row = self._connection().execute(
            "SELECT COUNT(*) AS n FROM starts WHERE started_at_utc >= ? AND planned = 0",
            (since_utc,),
        ).fetchone()
        return int(row["n"])

    def set_meta(self, key: str, value: str) -> None:
        if not key:
            raise ValueError("Meta key is required")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        if not key:
            raise ValueError("Meta key is required")
        row = self._connection().execute(
            "SELECT value FROM meta WHERE key = ?",
            (key,),
        ).fetchone()
        return None if row is None else row["value"]

    def commit_candle_decisions(
        self,
        *,
        symbol: str,
        timeframe: str,
        bar_open_utc: str,
        decisions: list[DecisionRecord],
        alert: AlertPayload | None = None,
    ) -> bool:
        """Persist candle decisions and progress atomically; return False for a duplicate."""
        _validate_utc_timestamp(bar_open_utc, "bar_open_utc")
        for decision in decisions:
            _validate_utc_timestamp(decision.time_utc, "decision.time_utc")
        connection = self._connection()
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT last_open_utc FROM bar_progress WHERE symbol = ? AND timeframe = ?",
                (symbol, timeframe),
            ).fetchone()
            if current is not None and bar_open_utc <= current["last_open_utc"]:
                return False
            connection.executemany(
                """
                INSERT INTO decisions(
                    time_utc, symbol, timeframe, reason_code, strategy,
                    code_version, settings_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.time_utc,
                        item.symbol,
                        item.timeframe,
                        item.reason_code,
                        item.strategy,
                        item.code_version,
                        item.settings_version,
                    )
                    for item in decisions
                ],
            )
            connection.execute(
                """
                INSERT INTO bar_progress(symbol, timeframe, last_open_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol, timeframe)
                DO UPDATE SET last_open_utc = excluded.last_open_utc
                """,
                (symbol, timeframe, bar_open_utc),
            )
            if alert is not None:
                self._insert_alert(connection, alert)
        return True

    def enqueue_alert(self, alert: AlertPayload) -> int:
        connection = self._connection()
        with connection:
            return self._insert_alert(connection, alert)

    @staticmethod
    def _insert_alert(connection: sqlite3.Connection, alert: AlertPayload) -> int:
        _validate_utc_timestamp(alert.created_at_utc, "alert.created_at_utc")
        if not alert.message.strip():
            raise ValueError("Alert message is required")
        if alert.severity not in {"INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Unknown alert severity: {alert.severity}")
        cursor = connection.execute(
            """
            INSERT INTO alert_outbox(created_at_utc, severity, message)
            VALUES (?, ?, ?)
            """,
            (alert.created_at_utc, alert.severity, alert.message),
        )
        return int(cursor.lastrowid)

    def pending_alerts(self, limit: int = 100) -> tuple[AlertRecord, ...]:
        if limit < 1:
            raise ValueError("Alert limit must be positive")
        rows = self._connection().execute(
            """
            SELECT alert_id, created_at_utc, severity, message
            FROM alert_outbox
            WHERE sent_at_utc IS NULL
            ORDER BY alert_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return tuple(
            AlertRecord(
                alert_id=row["alert_id"],
                created_at_utc=row["created_at_utc"],
                severity=row["severity"],
                message=row["message"],
            )
            for row in rows
        )

    def mark_alert_sent(self, alert_id: int, sent_at_utc: str) -> None:
        _validate_utc_timestamp(sent_at_utc, "sent_at_utc")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE alert_outbox
                SET sent_at_utc = ?
                WHERE alert_id = ? AND sent_at_utc IS NULL
                """,
                (sent_at_utc, alert_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Pending alert {alert_id} does not exist")

    def create_intent(
        self,
        *,
        intent_key: str,
        symbol: str,
        side: str,
        lots: float,
        entry: float,
        stop_loss: float,
        take_profit: float,
        actual_risk: float,
        created_at_utc: str,
        code_version: str,
        settings_version: str,
    ) -> IntentRecord:
        _validate_utc_timestamp(created_at_utc, "created_at_utc")
        connection = self._connection()
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO intents(
                    intent_key, symbol, side, lots, entry, stop_loss, take_profit,
                    actual_risk, status, created_at_utc, code_version, settings_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    intent_key,
                    symbol,
                    side,
                    lots,
                    entry,
                    stop_loss,
                    take_profit,
                    actual_risk,
                    created_at_utc,
                    code_version,
                    settings_version,
                ),
            )
            row = connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._intent_from_row(row)

    def transition_intent(
        self,
        intent_id: int,
        status: IntentStatus,
        *,
        ticket: str | None = None,
    ) -> IntentRecord:
        connection = self._connection()
        with connection:
            row = connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Intent {intent_id} does not exist")
            current_status = row["status"]
            if status != current_status and status not in _ALLOWED_TRANSITIONS[current_status]:
                raise ValueError(f"Invalid intent transition: {current_status} -> {status}")
            connection.execute(
                "UPDATE intents SET status = ?, ticket = COALESCE(?, ticket) WHERE intent_id = ?",
                (status, ticket, intent_id),
            )
            updated = connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        return self._intent_from_row(updated)

    def unresolved_intents(self) -> tuple[IntentRecord, ...]:
        rows = (
            self._connection()
            .execute(
                """
            SELECT * FROM intents
            WHERE status IN ('PENDING', 'SENT', 'UNKNOWN')
            ORDER BY intent_id
            """
            )
            .fetchall()
        )
        return tuple(self._intent_from_row(row) for row in rows)

    def create_trade_from_intent(
        self,
        *,
        intent_id: int,
        ticket: str,
        strategy: str,
        timeframe: str,
        opened_at_utc: str,
    ) -> TradeRecord:
        if not ticket or not strategy or not timeframe:
            raise ValueError("Trade ticket, strategy and timeframe are required")
        _validate_utc_timestamp(opened_at_utc, "opened_at_utc")
        connection = self._connection()
        with connection:
            intent = connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if intent is None:
                raise KeyError(f"Intent {intent_id} does not exist")
            if intent["status"] != "CONFIRMED":
                raise ValueError("Trade can only be recorded from a confirmed intent")
            if intent["ticket"] not in (None, ticket):
                raise ValueError("Trade ticket does not match the confirmed intent")
            connection.execute(
                """
                INSERT INTO trades(
                    ticket, intent_id, symbol, strategy, timeframe, side, lots, entry,
                    stop_loss, take_profit, actual_risk, opened_at_utc,
                    code_version, settings_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket,
                    intent_id,
                    intent["symbol"],
                    strategy,
                    timeframe,
                    intent["side"],
                    intent["lots"],
                    intent["entry"],
                    intent["stop_loss"],
                    intent["take_profit"],
                    intent["actual_risk"],
                    opened_at_utc,
                    intent["code_version"],
                    intent["settings_version"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM trades WHERE ticket = ?",
                (ticket,),
            ).fetchone()
        return TradeRecord(
            ticket=row["ticket"],
            intent_id=row["intent_id"],
            symbol=row["symbol"],
            strategy=row["strategy"],
            timeframe=row["timeframe"],
            side=row["side"],
            lots=row["lots"],
            entry=row["entry"],
            stop_loss=row["stop_loss"],
            take_profit=row["take_profit"],
            actual_risk=row["actual_risk"],
            opened_at_utc=row["opened_at_utc"],
            code_version=row["code_version"],
            settings_version=row["settings_version"],
        )

    def close_trade(
        self,
        ticket: str,
        closed_at_utc: str,
        close_price: float,
        profit: float,
        commission: float,
        exit_reason: str | ExitReason,
    ) -> TradeRecord:
        if not ticket:
            raise ValueError("Trade ticket is required")
        _validate_utc_timestamp(closed_at_utc, "closed_at_utc")
        reason_value = str(exit_reason)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM trades WHERE ticket = ?",
                (ticket,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Trade {ticket} does not exist")
            connection.execute(
                """
                UPDATE trades
                SET closed_at_utc = ?,
                    close_price = ?,
                    result_r = ?,
                    commission = ?,
                    close_reason = ?
                WHERE ticket = ?
                """,
                (closed_at_utc, close_price, profit, commission, reason_value, ticket),
            )
            updated = connection.execute(
                "SELECT * FROM trades WHERE ticket = ?",
                (ticket,),
            ).fetchone()
        return self._trade_from_row(updated)

    def record_equity(self, time_utc: str, balance: float, equity: float) -> None:
        _validate_utc_timestamp(time_utc, "time_utc")
        if balance < 0 or equity < 0:
            raise ValueError("Balance and equity cannot be negative")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO equity_log(time_utc, balance, equity) VALUES (?, ?, ?)
                ON CONFLICT(time_utc) DO UPDATE SET
                    balance = excluded.balance, equity = excluded.equity
                """,
                (time_utc, balance, equity),
            )

    def last_telegram_update_id(self) -> int:
        row = (
            self._connection()
            .execute("SELECT last_update_id FROM telegram WHERE singleton = 1")
            .fetchone()
        )
        if row is None:
            raise RuntimeError("Telegram cursor row is missing")
        return int(row["last_update_id"])

    def set_telegram_update_id(self, update_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE telegram SET last_update_id = ?
                WHERE singleton = 1 AND last_update_id <= ?
                """,
                (update_id, update_id),
            )

    @staticmethod
    def _trade_from_row(row: sqlite3.Row) -> TradeRecord:
        return TradeRecord(
            ticket=row["ticket"],
            intent_id=row["intent_id"],
            symbol=row["symbol"],
            strategy=row["strategy"],
            timeframe=row["timeframe"],
            side=row["side"],
            lots=row["lots"],
            entry=row["entry"],
            stop_loss=row["stop_loss"],
            take_profit=row["take_profit"],
            actual_risk=row["actual_risk"],
            opened_at_utc=row["opened_at_utc"],
            code_version=row["code_version"],
            settings_version=row["settings_version"],
            close_reason=row["close_reason"],
            closed_at_utc=row["closed_at_utc"],
            close_price=row["close_price"],
            commission=row["commission"],
            result_r=row["result_r"],
            candles_held=row["candles_held"],
        )

    @staticmethod
    def _intent_from_row(row: sqlite3.Row) -> IntentRecord:
        return IntentRecord(
            intent_id=row["intent_id"],
            intent_key=row["intent_key"],
            symbol=row["symbol"],
            side=row["side"],
            lots=row["lots"],
            entry=row["entry"],
            stop_loss=row["stop_loss"],
            take_profit=row["take_profit"],
            status=row["status"],
            created_at_utc=row["created_at_utc"],
            ticket=row["ticket"],
        )
