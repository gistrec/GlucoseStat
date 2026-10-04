"""Сенсор: дата установки из ответа Abbott и то, что из неё публикуется."""

from datetime import timedelta, timezone

from conftest import BASE
from test_main import FakeCollector

import main
from librelinkup import SENSOR_LIFETIME_DAYS, _parse_sensor
from publish import SENSOR_WINDOW, _sensor, build_snapshot


def unix(moment):
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def readings(start, until, step_minutes=5):
    """Ровный ряд замеров от ``start`` до ``until``."""

    out = []
    moment = start
    while moment <= until:
        out.append((moment, 110.0))
        moment += timedelta(minutes=step_minutes)
    return out


class TestParseSensor:
    """Блок ``sensor`` ответа ``graph`` — необязательный и чужой."""

    def test_it_reads_the_activation_epoch(self):
        started = BASE - timedelta(days=3)

        sensor = _parse_sensor({"a": unix(started), "sn": "секрет"}, BASE)

        assert sensor.started == started

    def test_a_missing_block_is_not_an_error(self):
        # Сборщик обязан пережить ответ без блока: показания важнее срока.
        assert _parse_sensor(None, BASE) is None
        assert _parse_sensor({}, BASE) is None
        assert _parse_sensor({"a": None}, BASE) is None

    def test_a_boolean_is_not_an_epoch(self):
        # bool — подкласс int, и без явной проверки True стал бы 1970 годом.
        assert _parse_sensor({"a": True}, BASE) is None

    def test_the_future_is_refused(self):
        assert _parse_sensor({"a": unix(BASE + timedelta(hours=1))}, BASE) is None

    def test_milliseconds_are_refused(self):
        # Миллисекунды вместо секунд — дата за пределами эпохи или в далёком
        # будущем; и то и другое отсеивается тем же правилом.
        assert _parse_sensor({"a": unix(BASE) * 1000}, BASE) is None

    def test_an_ancient_activation_is_refused(self):
        # Сенсор не живёт месяц: такое число означает, что в поле не epoch.
        old = BASE - timedelta(days=SENSOR_LIFETIME_DAYS * 2 + 1)

        assert _parse_sensor({"a": unix(old)}, BASE) is None


class TestSensorCard:
    """Что из этого попадает в снимок."""

    def test_it_counts_the_lifetime_from_the_start(self):
        started = BASE - timedelta(days=9)

        card = _sensor(started, readings(BASE - timedelta(hours=1), BASE), BASE)

        assert card["started"] == unix(started)
        assert card["ends"] == unix(started + timedelta(days=SENSOR_LIFETIME_DAYS))
        assert card["lifetime_days"] == SENSOR_LIFETIME_DAYS

    def test_without_a_start_the_dates_are_empty_but_the_card_is_not(self):
        # Половина карточки считается по самим показаниям и не зависит ни от
        # Abbott, ни от базы: полноту данных страница покажет и без даты.
        card = _sensor(None, readings(BASE - SENSOR_WINDOW, BASE), BASE)

        assert card["started"] is None
        assert card["ends"] is None
        assert card["coverage"] == 100.0

    def test_a_full_window_is_complete(self):
        card = _sensor(None, readings(BASE - SENSOR_WINDOW, BASE), BASE)

        assert card["coverage"] == 100.0
        assert card["quiet"] == 0
        assert card["quiet_minutes"] == 0

    def test_a_silence_eats_the_coverage(self):
        # Шесть часов молчания в середине недели — ровно 3,6 % окна.
        head = readings(BASE - SENSOR_WINDOW, BASE - timedelta(days=4))
        tail = readings(BASE - timedelta(days=4) + timedelta(hours=6), BASE)

        card = _sensor(None, head + tail, BASE)

        assert card["quiet"] == 1
        assert card["quiet_minutes"] == 360
        assert card["coverage"] == 96.4

    def test_the_running_silence_counts_too(self):
        # Сенсор молчит прямо сейчас: окно от этого не становится полным, и
        # карточка обязана сказать то же, что шапка страницы.
        card = _sensor(None, readings(BASE - SENSOR_WINDOW, BASE - timedelta(hours=3)), BASE)

        assert card["quiet"] == 1
        assert card["quiet_minutes"] == 180

    def test_an_old_silence_is_clipped_to_the_window(self):
        # Молчание началось за пределами недели: в полноте недели участвует
        # только та его часть, что попала внутрь.
        head = readings(BASE - timedelta(days=10), BASE - timedelta(days=8))
        tail = readings(BASE - timedelta(days=6), BASE)

        card = _sensor(None, head + tail, BASE)

        assert card["quiet_minutes"] == 24 * 60

    def test_an_empty_window_is_empty(self):
        card = _sensor(None, [], BASE)

        assert card["coverage"] == 0.0
        assert card["quiet"] == 1


class TestSnapshotCarriesIt:
    """Снимок несёт карточку — и не несёт серийного номера."""

    def test_build_snapshot_publishes_the_sensor(self):
        started = BASE - timedelta(days=2)
        snapshot = build_snapshot(
            readings(BASE - timedelta(hours=2), BASE),
            [],
            BASE,
            sensor_started=started,
        )

        assert snapshot["sensor"]["started"] == unix(started)

    def test_the_snapshot_has_no_serial_anywhere(self):
        snapshot = build_snapshot(
            readings(BASE - timedelta(hours=2), BASE), [], BASE, sensor_started=BASE
        )

        assert set(snapshot["sensor"]) == {
            "started",
            "ends",
            "lifetime_days",
            "window_days",
            "coverage",
            "quiet",
            "quiet_minutes",
        }


def test_the_collector_stores_the_start(monkeypatch):
    """run_once кладёт дату установки в базу, когда сборщик её знает."""

    stored = []
    monkeypatch.setattr(main, "store_readings", lambda rows: len(rows))
    monkeypatch.setattr(main, "last_readings", lambda limit=10: [])
    monkeypatch.setattr(main, "store_last_success", lambda when: None)
    monkeypatch.setattr(main, "publish", lambda **kwargs: None)
    monkeypatch.setattr(main, "store_sensor_start", lambda when: stored.append(when))

    class Known:
        started = BASE - timedelta(days=2)

    main.run_once(FakeCollector([(BASE, 110.0)], sensor=Known()), None, 300, 60, None)

    assert stored == [Known.started]
