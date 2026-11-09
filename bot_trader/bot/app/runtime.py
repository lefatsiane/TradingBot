from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from datetime import UTC, timedelta
from pathlib import Path
from types import FrameType
from typing import BinaryIO

from bot.adapters.broker import Broker
from bot.adapters.gateway import BrokerGateway
from bot.adapters.heartbeat import HeartbeatPinger
from bot.adapters.store import AlertPayload, StateStore
from bot.adapters.telegram import TelegramAdapter
from bot.app.config import (
    Settings,
    load_runtime_secrets,
    load_settings,
    schedule_validation_failures,
)
from bot.app.control import ControlHandler
from bot.app.engine import Engine
from bot.app.executor import Executor
from bot.app.logging_setup import configure_logging
from bot.app.symbol_validation import validate_symbol
from bot.app.versioning import get_version
from bot.core.clock import Clock, SystemClock

_CRASH_LOOP_WINDOW = timedelta(hours=1)
_CRASH_LOOP_LIMIT = 3


class RuntimeFileLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            handle.close()
            raise RuntimeError("Another runtime instance holds the bot lock") from error
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()
            self._handle = None


class BotRuntime:
    def __init__(
        self,
        engine: Engine,
        store: StateStore,
        clock: Clock,
        *,
        fast_tick_s: int,
        candle_poll_s: int,
        logger: logging.Logger,
        telegram: TelegramAdapter | None = None,
        heartbeat: HeartbeatPinger | None = None,
        startup_report: Callable[[], None] | None = None,
        broker_shutdown: Callable[[], None] | None = None,
    ) -> None:
        if fast_tick_s < 1 or candle_poll_s < 1:
            raise ValueError("Runtime polling intervals must be positive")
        self._engine = engine
        self._store = store
        self._clock = clock
        self._fast_tick_s = fast_tick_s
        self._candle_poll_s = candle_poll_s
        self._logger = logger
        self._telegram = telegram
        self._heartbeat = heartbeat
        self._startup_report = startup_report
        self._broker_shutdown = broker_shutdown

    def run(self, stop_event: threading.Event) -> None:
        clean_shutdown = False
        self._store.set_meta("clean_shutdown", "0")
        try:
            startup = self._engine.start()
            self._logger.info(
                "Engine started; degraded=%s actions=%s",
                startup.degraded,
                ",".join(startup.actions),
            )
            if self._startup_report is not None:
                self._startup_report()
            if self._telegram is not None:
                self._telegram.start()
            if self._heartbeat is not None:
                self._heartbeat.start()

            next_entry = time.monotonic() + self._candle_poll_s
            next_management = time.monotonic() + self._fast_tick_s
            while not stop_event.is_set():
                now = time.monotonic()
                if now >= next_management:
                    report = self._engine.run_management_lane()
                    if report.degraded:
                        self._logger.error("Management lane degraded")
                    next_management = now + self._fast_tick_s
                if now >= next_entry:
                    self._engine.run_entry_lane()
                    next_entry = now + self._candle_poll_s
                wait_s = max(0.0, min(next_entry, next_management) - time.monotonic())
                stop_event.wait(wait_s)
            clean_shutdown = True
        except KeyboardInterrupt:
            clean_shutdown = True
            self._logger.info("Shutdown requested by keyboard interrupt")
        finally:
            try:
                if self._telegram is not None:
                    self._telegram.stop()
            finally:
                try:
                    if self._heartbeat is not None:
                        self._heartbeat.stop()
                finally:
                    try:
                        self._engine.stop()
                    finally:
                        if self._broker_shutdown is not None:
                            self._broker_shutdown()
            if clean_shutdown:
                self._store.set_meta("clean_shutdown", "1")
                self._logger.info("Runtime stopped cleanly; broker positions left untouched")


def run_bot(
    settings_path: str | Path,
    *,
    broker: Broker | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    settings_file = Path(settings_path).resolve()
    project_dir = settings_file.parent
    logger = configure_logging(project_dir / "logs")
    settings, settings_version = load_settings(settings_file)
    failures = schedule_validation_failures(settings)
    if failures:
        raise RuntimeError(
            "Runtime requires reviewed schedules for all enabled symbols: "
            + "; ".join(failures)
        )

    runtime_lock = RuntimeFileLock(project_dir / "runtime" / "bot.lock")
    runtime_lock.acquire()
    store: StateStore | None = None
    created_broker: Broker | None = None
    runtime_started = False
    try:
        store = StateStore(project_dir / "state" / "bot.sqlite3")
        clock = SystemClock()
        planned_start = store.get_meta("clean_shutdown") != "0"
        CrashLoopGuard(store, clock).record_start(planned=planned_start)
        store.set_meta("clean_shutdown", "0")

        secrets = load_runtime_secrets()
        if secrets.mt5_login is None:
            raise RuntimeError("MT5_LOGIN must be set in the environment")
        settings = settings.model_copy(
            update={
                "account": settings.account.model_copy(
                    update={"expected_login": secrets.mt5_login}
                )
            }
        )
        if broker is None:
            if not secrets.mt5_password:
                raise RuntimeError("MT5_PASSWORD must be set in the environment")
            from bot.adapters.mt5_broker import MT5Broker

            live_broker = MT5Broker(
                deviation_points=settings.execution.deviation_points,
                on_server_clock_mismatch=lambda message: _block_new_entries_for_clock_mismatch(
                    store,
                    clock,
                    message,
                ),
            )
            live_broker.connect(
                login=secrets.mt5_login,
                server=settings.account.expected_server,
                password=secrets.mt5_password,
            )
            created_broker = live_broker
            active_broker: Broker = live_broker
        else:
            active_broker = broker

        gateway = BrokerGateway(active_broker)
        version = get_version(project_dir)
        code_version = version.commit + ("-dirty" if not version.clean else "")
        executor = Executor(
            gateway,
            store,
            clock,
            magic_number=settings.account.magic_number,
            max_send_retries=settings.execution.max_send_retries,
            unknown_outcome_wait_s=settings.execution.unknown_outcome_wait_s,
            code_version=code_version,
            settings_version=settings_version,
            commission_per_lot_side={
                symbol: symbol_settings.commission_per_lot_side
                for symbol, symbol_settings in settings.symbols.items()
            },
        )
        engine = Engine(
            settings,
            gateway,
            store,
            executor,
            clock,
            code_version=code_version,
            settings_version=settings_version,
        )
        control = ControlHandler(
            store,
            clock,
            gateway=gateway,
            executor=executor,
            magic_number=settings.account.magic_number,
        )
        telegram = None
        if secrets.telegram_token:
            if settings.control.telegram_chat_id <= 0:
                raise RuntimeError(
                    "A positive telegram_chat_id is required when TELEGRAM_TOKEN is set"
                )
            telegram = TelegramAdapter(
                secrets.telegram_token,
                settings.control.telegram_chat_id,
                settings.control.command_max_age_s,
                store,
                clock,
                control,
                on_alert=lambda severity, message: store.enqueue_alert(
                    AlertPayload(
                        clock.now_utc().astimezone(UTC).isoformat(),
                        severity,
                        message,
                    )
                ),
            )
        heartbeat = (
            HeartbeatPinger(secrets.healthcheck_url)
            if secrets.healthcheck_url
            else None
        )
        runtime = BotRuntime(
            engine,
            store,
            clock,
            fast_tick_s=settings.timing.fast_tick_s,
            candle_poll_s=settings.timing.candle_poll_s,
            logger=logger,
            telegram=telegram,
            heartbeat=heartbeat,
            startup_report=lambda: _log_minimum_lot_startup_report(
                settings, gateway, clock, logger
            ),
            broker_shutdown=(
                created_broker.shutdown
                if created_broker is not None
                else None
            ),
        )
        runtime_started = True
        runtime_stop_event = stop_event or threading.Event()
        if threading.current_thread() is threading.main_thread():
            previous_handlers = {
                signum: signal.getsignal(signum)
                for signum in (signal.SIGINT, signal.SIGTERM)
            }

            def request_stop(signum: int, frame: FrameType | None) -> None:
                frame_name = frame.f_code.co_name if frame is not None else "unknown"
                logger.info(
                    "Shutdown requested by %s while executing %s",
                    signal.Signals(signum).name,
                    frame_name,
                )
                runtime_stop_event.set()

            try:
                for signum in previous_handlers:
                    signal.signal(signum, request_stop)
                runtime.run(runtime_stop_event)
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
        else:
            runtime.run(runtime_stop_event)
    finally:
        if created_broker is not None and not runtime_started:
            created_broker.shutdown()
        if store is not None:
            store.close_thread_connection()
        runtime_lock.release()


def _log_minimum_lot_startup_report(
    settings: Settings,
    gateway: BrokerGateway,
    clock: Clock,
    logger: logging.Logger,
) -> None:
    account = gateway.account()
    max_stop_atr = max(
        settings.strategy_a.max_stop_atr,
        settings.strategy_b.max_stop_atr,
    )
    for symbol, symbol_settings in settings.symbols.items():
        if not symbol_settings.enabled:
            continue
        report = validate_symbol(
            gateway,
            symbol,
            symbol_settings,
            equity=account.equity,
            risk_per_trade_pct=settings.risk.risk_per_trade_pct,
            cost_gate_pct_of_risk=settings.risk.cost_gate_pct_of_risk,
            max_stop_atr=max_stop_atr,
            now_utc=clock.now_utc().astimezone(UTC),
            server_session_hours=settings.broker_session_hours_server_time,
        )
        if "BELOW_MIN_LOT" in report.issues:
            logger.warning(
                "Startup sizing: %s is below minimum lot at the configured maximum "
                "stop distance and current equity",
                symbol,
            )
        elif any(
            issue.startswith(
                (
                    "tick read failed:",
                    "no fresh, valid tick",
                    "H1 history",
                    "cannot calculate a positive H1 ATR",
                )
            )
            for issue in report.issues
        ):
            logger.warning(
                "Startup sizing diagnostic incomplete for %s: %s",
                symbol,
                "; ".join(report.issues),
            )


def _block_new_entries_for_clock_mismatch(
    store: StateStore,
    clock: Clock,
    message: str,
) -> None:
    store.set_flag("stop_new", True)
    store.enqueue_alert(
        AlertPayload(
            clock.now_utc().astimezone(UTC).isoformat(),
            "CRITICAL",
            f"Broker clock verification failed: {message}",
        )
    )


class CrashLoopGuard:
    def __init__(self, store: StateStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def record_start(self, *, planned: bool) -> bool:
        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock must return a timezone-aware timestamp")
        now = now.astimezone(UTC)
        self._store.record_start(now.isoformat(), planned=int(planned))
        if planned:
            return False

        cutoff = (now - _CRASH_LOOP_WINDOW).isoformat()
        if self._store.unplanned_starts_since(cutoff) < _CRASH_LOOP_LIMIT:
            return False
        self._store.set_flag("stop_new", True)
        self._store.enqueue_alert(
            AlertPayload(
                now.isoformat(),
                "CRITICAL",
                "Three unplanned starts occurred within one hour; new entries stopped",
            )
        )
        return True
