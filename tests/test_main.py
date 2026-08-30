"""Tests for the freshness sentinel Netdata watches.

The alarm is only as good as the mtime under it, and the mistake worth
catching is stamping the file with the current time — which would report data
as fresh whenever the collector runs, which is precisely when it cannot be
trusted to say so.
"""

from datetime import datetime, timedelta, timezone

import pytest

from main import BACKOFF_MIN, run_once, stamp_freshness


BASE = datetime(2026, 8, 22, 12, 0, 0)


def unix(moment: datetime) -> float:
    return moment.replace(tzinfo=timezone.utc).timestamp()


class TestStampFreshness:
    def test_without_readings_no_file_appears(self, tmp_path):
        # Пустая база — не то же самое, что свежие данные.
        path = tmp_path / ".last-reading"
        stamp_freshness([], str(path))

        assert not path.exists()

    def test_the_mtime_is_the_reading_time_and_not_now(self, tmp_path):
        path = tmp_path / ".last-reading"
        stamp_freshness([(BASE, 120.0)], str(path))

        assert path.stat().st_mtime == pytest.approx(unix(BASE), abs=1)

    def test_the_newest_reading_wins(self, tmp_path):
        # last_readings отдаёт от старых к новым, метка ставится по последней.
        path = tmp_path / ".last-reading"
        readings = [(BASE, 120.0), (BASE + timedelta(minutes=5), 125.0)]
        stamp_freshness(readings, str(path))

        assert path.stat().st_mtime == pytest.approx(
            unix(BASE + timedelta(minutes=5)), abs=1
        )

    def test_a_later_poll_moves_the_mark_forward(self, tmp_path):
        path = tmp_path / ".last-reading"
        stamp_freshness([(BASE, 120.0)], str(path))
        stamp_freshness([(BASE + timedelta(hours=1), 120.0)], str(path))

        assert path.stat().st_mtime == pytest.approx(
            unix(BASE + timedelta(hours=1)), abs=1
        )

    def test_the_file_stays_empty(self, tmp_path):
        # filecheck читает только stat, содержимое ему не нужно — и лишний
        # разбор файла означал бы ещё одно место, где мониторинг молча врёт.
        path = tmp_path / ".last-reading"
        stamp_freshness([(BASE, 120.0)], str(path))
        stamp_freshness([(BASE + timedelta(minutes=5), 120.0)], str(path))

        assert path.stat().st_size == 0


class FakeCollector:
    """poll() отдаёт заготовку или бросает её, если это исключение."""

    def __init__(self, result):
        self._result = result

    def poll(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class RecordingNotifier:
    def __init__(self):
        self.seen = []

    def check(self, readings):
        self.seen.append(list(readings))


class TestRunOnce:
    """The alert path must not depend on the database.

    Показание, уже скачанное у Abbott, обязано дойти до нотификатора и тогда,
    когда MySQL лежит: гипогликемия в руках — не повод молчать из-за
    неудачной записи.
    """

    def quiet(self, monkeypatch):
        """Заглушить побочные эффекты, которых тест не проверяет."""

        monkeypatch.setattr("main.publish", lambda **kwargs: None)
        monkeypatch.setattr("main.stamp_freshness", lambda readings: None)

    def test_db_outage_does_not_silence_the_alert(self, monkeypatch):
        def down(*args, **kwargs):
            raise RuntimeError("mysql is down")

        self.quiet(monkeypatch)
        monkeypatch.setattr("main.store_readings", down)
        monkeypatch.setattr("main.last_readings", down)

        low = [(BASE, 50.0)]
        notifier = RecordingNotifier()

        run_once(FakeCollector(low), notifier, 300, BACKOFF_MIN, None)

        assert notifier.seen == [low]

    def test_failed_fetch_falls_back_to_the_database(self, monkeypatch):
        # Опрос не удался — тревога идёт по свежайшей строке базы, где
        # устаревшее значение notify отсеет по возрасту, а не по пустоте.
        stored = [(BASE, 120.0)]
        self.quiet(monkeypatch)
        monkeypatch.setattr("main.store_readings", lambda readings: 0)
        monkeypatch.setattr("main.last_readings", lambda n: stored)

        notifier = RecordingNotifier()
        delay, backoff, _ = run_once(
            FakeCollector(RuntimeError("network down")),
            notifier,
            300,
            BACKOFF_MIN,
            None,
        )

        assert notifier.seen == [stored]
        assert delay == BACKOFF_MIN
        assert backoff == BACKOFF_MIN * 2

    def test_successful_cycle_resets_backoff_and_publishes_the_mark(
        self, monkeypatch
    ):
        fetched = [(BASE, 120.0), (BASE + timedelta(minutes=5), 90.0)]
        published = {}
        monkeypatch.setattr("main.store_readings", lambda readings: len(readings))
        monkeypatch.setattr("main.last_readings", lambda n: fetched[-1:])
        monkeypatch.setattr("main.publish", lambda **kwargs: published.update(kwargs))
        monkeypatch.setattr("main.stamp_freshness", lambda readings: None)

        notifier = RecordingNotifier()
        delay, backoff, last_success = run_once(
            FakeCollector(fetched), notifier, 300, BACKOFF_MIN * 8, None
        )

        assert notifier.seen == [fetched]
        assert delay == 300
        assert backoff == BACKOFF_MIN
        assert last_success is not None
        assert published["last_success"] == last_success
