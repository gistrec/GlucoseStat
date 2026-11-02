"""Источник Libre: опрос LibreLinkUp, запись, отметки, пауза после сбоя.

Снимок, тревога и метка свежести — у publisher.py (tests/test_publisher.py):
здесь только то, что источник обязан сделать сам.
"""

from datetime import datetime, timedelta, timezone

from conftest import BASE
from libre import BACKOFF_MAX, BACKOFF_MIN, RATE_LIMIT_MIN, run_once
from librelinkup import RateLimited


class FakeCollector:
    """poll() отдаёт заготовку или бросает её, если это исключение."""

    def __init__(self, result, sensor=None):
        self._result = result
        # Как у настоящего сборщика: дата установки сенсора лежит на объекте
        # и остаётся None, пока Abbott её не назвал.
        self.sensor = sensor

    def poll(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class TestRunOnce:
    def quiet(self, monkeypatch, stored=None, marks=None, received=None):
        monkeypatch.setattr(
            "libre.store_readings",
            lambda readings, sensor, received_at: (stored if stored is not None else []).append(
                (list(readings), sensor, received_at)
            )
            or len(readings),
        )
        monkeypatch.setattr(
            "libre.store_last_success",
            lambda when: (marks if marks is not None else []).append(when),
        )
        monkeypatch.setattr(
            "libre.mark_received",
            lambda sensor, when: (received if received is not None else []).append(sensor),
        )

    def test_readings_are_stored_under_libre_with_a_receipt_time(self, monkeypatch):
        stored, received = [], []
        self.quiet(monkeypatch, stored=stored, received=received)

        run_once(FakeCollector([(BASE, 120.0)]), 300, BACKOFF_MIN, None)

        readings, sensor, received_at = stored[0]
        assert readings == [(BASE, 120.0)]
        assert sensor == "libre"
        assert received_at is not None and received_at.tzinfo is None
        assert received == ["libre"]

    def test_a_successful_poll_stores_the_mark_as_naive_utc(self, monkeypatch):
        marks = []
        self.quiet(monkeypatch, marks=marks)

        run_once(FakeCollector([(BASE, 120.0)]), 300, BACKOFF_MIN, None)

        assert len(marks) == 1
        assert marks[0].tzinfo is None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert abs((now - marks[0]).total_seconds()) < 5

    def test_success_resets_the_backoff(self, monkeypatch):
        self.quiet(monkeypatch)

        delay, backoff, last_success = run_once(
            FakeCollector([(BASE, 120.0)]), 300, BACKOFF_MIN * 8, None
        )

        assert delay == 300
        assert backoff == BACKOFF_MIN
        assert last_success is not None

    def test_a_failed_poll_backs_off_and_stores_no_mark(self, monkeypatch):
        marks = []
        self.quiet(monkeypatch, marks=marks)

        delay, backoff, last_success = run_once(
            FakeCollector(RuntimeError("network down")), 300, BACKOFF_MIN, None
        )

        assert delay == BACKOFF_MIN
        assert backoff == BACKOFF_MIN * 2
        assert last_success is None
        assert marks == []

    def test_the_backoff_is_capped(self, monkeypatch):
        self.quiet(monkeypatch)

        _, backoff, _ = run_once(FakeCollector(RuntimeError("down")), 300, BACKOFF_MAX, None)

        assert backoff == BACKOFF_MAX

    def test_a_rate_limit_waits_without_growing_the_backoff(self, monkeypatch):
        self.quiet(monkeypatch)

        delay, backoff, _ = run_once(FakeCollector(RateLimited(None)), 300, BACKOFF_MIN, None)

        assert delay == RATE_LIMIT_MIN
        assert backoff == BACKOFF_MIN

    def test_a_failed_write_is_a_failed_poll(self, monkeypatch):
        # MySQL недоступна: показания не записаны, отметка успеха не ставится,
        # и источник ждёт, а не опрашивает Abbott каждую минуту.
        self.quiet(monkeypatch)

        def down(*args, **kwargs):
            raise RuntimeError("mysql is down")

        monkeypatch.setattr("libre.store_readings", down)

        delay, backoff, last_success = run_once(
            FakeCollector([(BASE, 120.0)]), 300, BACKOFF_MIN, None
        )

        assert last_success is None
        assert delay == BACKOFF_MIN

    def test_a_previous_success_is_carried_through_a_failure(self, monkeypatch):
        marks = []
        self.quiet(monkeypatch, marks=marks)
        earlier = (BASE - timedelta(minutes=5)).replace(tzinfo=timezone.utc).timestamp()

        _, _, last_success = run_once(FakeCollector(RuntimeError("down")), 300, BACKOFF_MIN, earlier)

        # Отметка не сдвигается вперёд, но и не теряется: страница по ней
        # скажет, когда LibreLinkUp отвечал в последний раз.
        assert last_success == earlier
        assert len(marks) == 1
