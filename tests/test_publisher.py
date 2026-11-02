"""Публикатор: тревога, метка свежести и снимок — по основному датчику.

Метка свежести — то, по чему Netdata замечает молчание данных. Ошибка, которую
стоит ловить, — ставить ей текущее время: тогда данные казались бы свежими
всякий раз, когда процесс работает, то есть ровно тогда, когда это ничего не
доказывает.
"""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import BASE
from publisher import run_once, stamp_freshness


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
        stamp_freshness([(BASE, 120.0), (BASE + timedelta(minutes=5), 125.0)], str(path))

        assert path.stat().st_mtime == pytest.approx(unix(BASE + timedelta(minutes=5)), abs=1)

    def test_a_later_step_moves_the_mark_forward(self, tmp_path):
        path = tmp_path / ".last-reading"
        stamp_freshness([(BASE, 120.0)], str(path))
        stamp_freshness([(BASE + timedelta(hours=1), 120.0)], str(path))

        assert path.stat().st_mtime == pytest.approx(unix(BASE + timedelta(hours=1)), abs=1)

    def test_the_file_stays_empty(self, tmp_path):
        # filecheck читает только stat, содержимое ему не нужно — и лишний
        # разбор файла означал бы ещё одно место, где мониторинг молча врёт.
        path = tmp_path / ".last-reading"
        stamp_freshness([(BASE, 120.0)], str(path))
        stamp_freshness([(BASE + timedelta(minutes=5), 120.0)], str(path))

        assert path.stat().st_size == 0


class RecordingNotifier:
    def __init__(self):
        self.seen = []
        self.verified = []

    def check(self, readings, verified=None):
        self.seen.append(list(readings))
        self.verified.append(verified)


class TestRunOnce:
    @pytest.fixture
    def world(self, monkeypatch):
        """База из словаря: основной датчик и по точке на каждый."""

        state = {
            "primary": "libre",
            "latest": {"libre": [(BASE, 120.0)], "ottai": [(BASE, 60.0)]},
            "stick": None,
            "published": [],
            "stamped": [],
            "asked": [],
        }

        def last_readings(n, sensor="libre"):
            state["asked"].append(sensor)
            return state["latest"].get(sensor, [])

        monkeypatch.setattr("publisher.primary_sensor", lambda: state["primary"])
        monkeypatch.setattr("publisher.last_readings", last_readings)
        monkeypatch.setattr("publisher.latest_fingerstick", lambda: state["stick"])
        monkeypatch.setattr("publisher.publish", lambda **kwargs: state["published"].append(kwargs))
        monkeypatch.setattr("publisher.stamp_freshness", lambda readings: state["stamped"].append(readings))
        return state

    def test_the_alert_reads_the_primary(self, world):
        notifier = RecordingNotifier()

        run_once(notifier)

        assert notifier.seen == [[(BASE, 120.0)]]
        assert world["asked"] == ["libre"]

    def test_switching_to_ottai_alerts_on_ottai(self, world):
        world["primary"] = "ottai"
        notifier = RecordingNotifier()

        run_once(notifier)

        assert notifier.seen == [[(BASE, 60.0)]]
        assert world["stamped"] == [[(BASE, 60.0)]]

    def test_a_fingerstick_reaches_the_notifier(self, world):
        world["stick"] = (BASE, 95.0)
        notifier = RecordingNotifier()

        run_once(notifier)

        assert notifier.verified == [(BASE, 95.0)]

    def test_a_broken_fingerstick_read_still_alerts(self, world, monkeypatch):
        # Не смогли спросить про кровь — значит, сверки нет, и тревога
        # поднимается как без неё: молчать по такой причине нельзя.
        def down():
            raise RuntimeError("journal is gone")

        monkeypatch.setattr("publisher.latest_fingerstick", down)
        notifier = RecordingNotifier()

        run_once(notifier)

        assert notifier.seen == [[(BASE, 120.0)]]
        assert notifier.verified == [None]

    def test_every_step_publishes_without_a_last_success(self, world):
        # last_success publish() читает из базы, куда его пишет libre.py.
        run_once(None)
        run_once(None)

        assert world["published"] == [{"cache": None}, {"cache": None}]

    def test_a_broken_primary_read_falls_back_to_libre(self, world, monkeypatch):
        def down():
            raise RuntimeError("no registry")

        monkeypatch.setattr("publisher.primary_sensor", down)

        run_once(RecordingNotifier())

        assert world["asked"] == ["libre"]

    def test_with_mysql_down_it_stays_up_and_silent(self, world, monkeypatch):
        # Принятая цена: тревога из базы, и пока MySQL лежит, тревог нет.
        # Процесс при этом не падает и пробует снова на следующем шаге.
        def down(*args, **kwargs):
            raise RuntimeError("mysql is down")

        monkeypatch.setattr("publisher.last_readings", down)
        monkeypatch.setattr("publisher.publish", down)
        notifier = RecordingNotifier()

        run_once(notifier)

        # notify видит пустой список — и ничего не поднимает.
        assert notifier.seen == [[]]
        assert world["stamped"] == [[]]

    def test_a_failed_alert_does_not_cost_the_snapshot(self, world):
        class Broken:
            def check(self, readings, verified=None):
                raise RuntimeError("pushover is down")

        run_once(Broken())

        assert len(world["published"]) == 1
