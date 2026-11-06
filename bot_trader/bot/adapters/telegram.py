from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol

import requests

from bot.adapters.store import AlertPayload, StateStore
from bot.app.control import ControlHandler
from bot.core.clock import Clock

_REQUEST_TIMEOUT_S = 35.0
_LONG_POLL_TIMEOUT_S = 30
_MAX_BACKOFF_S = 30.0
_WARNING_MERGE_WINDOW = timedelta(minutes=10)


class TelegramError(RuntimeError):
    pass


class TelegramConflictError(TelegramError):
    pass


class _Response(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> object: ...

    def raise_for_status(self) -> None: ...


class _Session(Protocol):
    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
    ) -> _Response: ...


class TelegramAdapter:
    def __init__(
        self,
        token: str,
        chat_id: int,
        command_max_age_s: int,
        store: StateStore,
        clock: Clock,
        control: ControlHandler,
        *,
        session: _Session | None = None,
        on_alert: Callable[[str, str], None] | None = None,
    ) -> None:
        if not token:
            raise ValueError("Telegram token is required")
        if command_max_age_s < 60:
            raise ValueError("command_max_age_s must be at least 60 seconds")
        self._token = token
        self._chat_id = chat_id
        self._command_max_age_s = command_max_age_s
        self._store = store
        self._clock = clock
        self._control = control
        self._session = session or requests.Session()
        self._on_alert = on_alert or (lambda _severity, _text: None)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_clock_alert: datetime | None = None

    def start(self) -> None:
        self._request("deleteWebhook", {"drop_pending_updates": False})
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.run,
            name="telegram-poller",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("Telegram polling thread did not stop in time")
            self._thread = None

    def poll_once(self) -> None:
        updates = self._request(
            "getUpdates",
            {
                "offset": self._store.last_telegram_update_id() + 1,
                "timeout": _LONG_POLL_TIMEOUT_S,
                "allowed_updates": ["message"],
            },
        )
        if not isinstance(updates, list):
            raise TelegramError("Telegram returned an invalid update list")
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if not isinstance(update_id, int):
                continue
            if update_id <= self._store.last_telegram_update_id():
                continue
            self._process_update(update)
            self._store.set_telegram_update_id(update_id)

    def run(self) -> None:
        delay_s = 1.0
        while not self._stop_event.is_set():
            failed = False
            try:
                self.poll_once()
                self.flush_alerts()
                delay_s = 1.0
            except TelegramConflictError:
                failed = True
            except TelegramError:
                failed = True
            if self._stop_event.wait(delay_s):
                break
            if failed:
                delay_s = min(delay_s * 2.0, _MAX_BACKOFF_S)

    def send_alert(self, severity: str, text: str) -> bool:
        normalized = severity.upper()
        if normalized not in {"INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Unknown alert severity: {severity}")
        if not text.strip():
            raise ValueError("Alert text is required")
        now = self._clock.now_utc().astimezone(UTC)
        if normalized == "WARNING" and self._warning_was_sent_recently(text, now):
            return True
        if self._try_send(text):
            if normalized == "WARNING":
                key = self._warning_meta_key(text)
                self._store.set_meta(key, now.isoformat())
            return True
        self._store.enqueue_alert(
            AlertPayload(now.isoformat(), normalized, text)
        )
        if normalized == "WARNING":
            self._store.set_meta(self._warning_meta_key(text), now.isoformat())
        return False

    def flush_alerts(self, limit: int = 100) -> int:
        sent = 0
        for alert in self._store.pending_alerts(limit):
            if not self._try_send(f"{alert.severity}: {alert.message}"):
                break
            self._store.mark_alert_sent(
                alert.alert_id,
                self._clock.now_utc().astimezone(UTC).isoformat(),
            )
            if alert.severity == "WARNING":
                self._store.set_meta(
                    self._warning_meta_key(alert.message),
                    self._clock.now_utc().astimezone(UTC).isoformat(),
                )
            sent += 1
        return sent

    def _process_update(self, update: dict[str, object]) -> None:
        if "edited_message" in update:
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        if chat.get("type") != "private" or str(chat.get("id")) != str(self._chat_id):
            return
        text = message.get("text")
        sent_at = message.get("date")
        if not isinstance(text, str) or not isinstance(sent_at, int):
            return
        now = self._clock.now_utc().astimezone(UTC)
        sent_time = datetime.fromtimestamp(sent_at, UTC)
        if (now - sent_time).total_seconds() > self._command_max_age_s:
            self._send_message(self._chat_id, "Command expired.")
            return
        if not text.startswith("/") and text != "YES":
            return
        reply = self._control.handle(text)
        self._send_message(self._chat_id, reply)

    def _send_message(self, chat_id: int, text: str) -> None:
        self._request("sendMessage", {"chat_id": chat_id, "text": text})

    def _try_send(self, text: str) -> bool:
        try:
            self._send_message(self._chat_id, text)
        except TelegramError:
            return False
        return True

    def _request(self, method: str, payload: dict[str, object]) -> object:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        try:
            response = self._session.post(
                url,
                json=payload,
                timeout=_REQUEST_TIMEOUT_S,
            )
        except (requests.RequestException, ConnectionError, TimeoutError):
            raise TelegramError("Telegram network request failed") from None

        self._check_server_clock(response.headers)
        if response.status_code == 409:
            self._on_alert("ERROR", "Telegram polling conflict (HTTP 409)")
            raise TelegramConflictError("Telegram polling conflict")
        try:
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError):
            raise TelegramError("Telegram returned an invalid response") from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramError("Telegram API request was not successful")
        return body.get("result")

    def _check_server_clock(self, headers: Mapping[str, str]) -> None:
        date_header = headers.get("Date")
        if date_header is None:
            return
        try:
            server_time = parsedate_to_datetime(date_header)
        except (TypeError, ValueError, OverflowError):
            return
        if server_time.tzinfo is None:
            server_time = server_time.replace(tzinfo=UTC)
        now = self._clock.now_utc().astimezone(UTC)
        if abs((server_time.astimezone(UTC) - now).total_seconds()) <= 5:
            return
        if (
            self._last_clock_alert is None
            or now - self._last_clock_alert > _WARNING_MERGE_WINDOW
        ):
            self._on_alert("ERROR", "Laptop clock differs from Telegram server clock")
            self._last_clock_alert = now

    def _warning_was_sent_recently(self, text: str, now: datetime) -> bool:
        value = self._store.get_meta(self._warning_meta_key(text))
        if value is None:
            return False
        previous = datetime.fromisoformat(value)
        age = now - previous
        return timedelta(0) <= age < _WARNING_MERGE_WINDOW

    @staticmethod
    def _warning_meta_key(text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"telegram_warning:{digest}"
