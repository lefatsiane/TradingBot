from __future__ import annotations

import pytest

from bot.adapters.broker import OrderRequest
from bot.adapters.gateway import BrokerGateway
from bot.core.types import Side


class FlakyBroker:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.account_calls = 0
        self.send_calls = 0

    def account(self) -> str:
        self.account_calls += 1
        if self.account_calls <= self.failures:
            raise ConnectionError("temporary transport failure")
        return "account"

    def send_market(self, request: OrderRequest) -> None:
        self.send_calls += 1
        raise TimeoutError("send outcome unknown")


def test_gateway_retries_reads_with_backoff_and_clears_degraded_flag() -> None:
    broker = FlakyBroker(failures=3)
    delays: list[float] = []
    gateway = BrokerGateway(broker, max_retries=3, sleep=delays.append)

    assert gateway.account() == "account"
    assert delays == [1.0, 2.0, 4.0]
    assert broker.account_calls == 4
    assert not gateway.degraded


def test_gateway_releases_lock_while_waiting_for_backoff() -> None:
    broker = FlakyBroker(failures=1)
    gateway: BrokerGateway
    lock_was_available: list[bool] = []

    def inspect_lock(_: float) -> None:
        acquired = gateway._lock.acquire(timeout=0.01)
        lock_was_available.append(acquired)
        if acquired:
            gateway._lock.release()

    gateway = BrokerGateway(broker, max_retries=1, sleep=inspect_lock)
    assert gateway.account() == "account"
    assert lock_was_available == [True]


def test_gateway_does_not_retry_non_idempotent_order_send() -> None:
    broker = FlakyBroker()
    gateway = BrokerGateway(broker, max_retries=4, sleep=lambda _: None)
    request = OrderRequest(
        symbol="EURUSD",
        side=Side.LONG,
        lots=0.01,
        stop_loss=1.099,
        take_profit=1.102,
        magic_number=123,
        intent_id="1",
    )

    with pytest.raises(TimeoutError, match="outcome unknown"):
        gateway.send_market(request)
    assert broker.send_calls == 1
    assert gateway.degraded


def test_gateway_marks_degraded_when_lock_times_out() -> None:
    gateway = BrokerGateway(FlakyBroker(), lock_timeout_s=0.01)
    gateway._lock.acquire()
    try:
        with pytest.raises(TimeoutError, match="broker lock"):
            gateway.account()
    finally:
        gateway._lock.release()
    assert gateway.degraded


def test_gateway_retry_budget_is_bounded_and_reports_failure() -> None:
    broker = FlakyBroker(failures=10)
    delays: list[float] = []
    gateway = BrokerGateway(broker, max_retries=6, sleep=delays.append)

    with pytest.raises(ConnectionError, match="transport failure"):
        gateway.account()
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    assert gateway.degraded
