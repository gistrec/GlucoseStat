"""Tests for the freshness sentinel Netdata watches.

The alarm is only as good as the mtime under it, and the mistake worth
catching is stamping the file with the current time — which would report data
as fresh whenever the collector runs, which is precisely when it cannot be
trusted to say so.
"""

from datetime import datetime, timedelta, timezone

import pytest

from main import stamp_freshness


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
