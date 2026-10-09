"""Сенсор: дата установки из ответа Abbott и то, что из неё публикуется."""

from datetime import timedelta, timezone

from conftest import BASE
from test_main import FakeCollector

import main
from librelinkup import (
    SENSOR_LIFETIME_FALLBACK_DAYS,
    SENSOR_MODELS,
    _parse_sensor,
)
from publish import SENSOR_WINDOW, _bias, _sensor, build_snapshot


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

    def test_an_unknown_model_falls_back_to_the_default_lifetime(self):
        # Нумерация моделей у Abbott не опубликована, и таблица пустая: пока
        # в ней нет строки, срок берётся запасной, а не угаданный.
        sensor = _parse_sensor({"a": unix(BASE), "pt": 4}, BASE)

        assert sensor.kind == 4
        assert sensor.lifetime_days == SENSOR_LIFETIME_FALLBACK_DAYS

    def test_a_known_model_decides_the_lifetime(self, monkeypatch):
        monkeypatch.setitem(SENSOR_MODELS, 7, 14)

        sensor = _parse_sensor({"a": unix(BASE), "pt": 7}, BASE)

        assert sensor.lifetime_days == 14

    def test_a_missing_model_is_not_an_error(self):
        sensor = _parse_sensor({"a": unix(BASE)}, BASE)

        assert sensor.kind is None
        assert sensor.lifetime_days == SENSOR_LIFETIME_FALLBACK_DAYS

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
        old = BASE - timedelta(days=SENSOR_LIFETIME_FALLBACK_DAYS * 2 + 1)

        assert _parse_sensor({"a": unix(old)}, BASE) is None


class TestSensorCard:
    """Что из этого попадает в снимок."""

    def test_it_publishes_both_marks_as_the_collector_wrote_them(self):
        # Срок считает сборщик — он один знает модель. Здесь обе отметки уже
        # готовы, и публикация обязана отдать их как есть.
        started = BASE - timedelta(days=9)
        ends = started + timedelta(days=15)

        card = _sensor(started, ends, readings(BASE - timedelta(hours=1), BASE), BASE)

        assert card["started"] == unix(started)
        assert card["ends"] == unix(ends)
        assert card["lifetime_days"] == 15

    def test_an_old_row_without_an_end_gets_the_fallback(self):
        # Отметку писал прежний сборщик, конца срока в базе нет. Карточка с
        # датой на сутки точнее, чем карточка без даты.
        started = BASE - timedelta(days=9)

        card = _sensor(started, None, readings(BASE - timedelta(hours=1), BASE), BASE)

        assert card["ends"] == unix(
            started + timedelta(days=SENSOR_LIFETIME_FALLBACK_DAYS)
        )
        assert card["lifetime_days"] == SENSOR_LIFETIME_FALLBACK_DAYS

    def test_the_model_decides_the_lifetime(self):
        # Pro живёт пятнадцать суток, обычный — четырнадцать, и карточка
        # повторяет то, что посчитал сборщик, а не свою константу.
        started = BASE - timedelta(days=2)

        for days in (14, 15):
            card = _sensor(started, started + timedelta(days=days), [], BASE)
            assert card["lifetime_days"] == days

    def test_without_a_start_the_dates_are_empty_but_the_card_is_not(self):
        # Половина карточки считается по самим показаниям и не зависит ни от
        # Abbott, ни от базы: полноту данных страница покажет и без даты.
        card = _sensor(None, None, readings(BASE - SENSOR_WINDOW, BASE), BASE)

        assert card["started"] is None
        assert card["ends"] is None
        assert card["coverage"] == 100.0

    def test_a_full_window_is_complete(self):
        card = _sensor(None, None, readings(BASE - SENSOR_WINDOW, BASE), BASE)

        assert card["coverage"] == 100.0
        assert card["quiet"] == 0
        assert card["quiet_minutes"] == 0

    def test_a_silence_eats_the_coverage(self):
        # Шесть часов молчания в середине недели — ровно 3,6 % окна.
        head = readings(BASE - SENSOR_WINDOW, BASE - timedelta(days=4))
        tail = readings(BASE - timedelta(days=4) + timedelta(hours=6), BASE)

        card = _sensor(None, None, head + tail, BASE)

        assert card["quiet"] == 1
        assert card["quiet_minutes"] == 360
        assert card["coverage"] == 96.4

    def test_the_running_silence_counts_too(self):
        # Сенсор молчит прямо сейчас: окно от этого не становится полным, и
        # карточка обязана сказать то же, что шапка страницы.
        card = _sensor(
            None, None, readings(BASE - SENSOR_WINDOW, BASE - timedelta(hours=3)), BASE
        )

        assert card["quiet"] == 1
        assert card["quiet_minutes"] == 180

    def test_an_old_silence_is_clipped_to_the_window(self):
        # Молчание началось за пределами недели: в полноте недели участвует
        # только та его часть, что попала внутрь.
        head = readings(BASE - timedelta(days=10), BASE - timedelta(days=8))
        tail = readings(BASE - timedelta(days=6), BASE)

        card = _sensor(None, None, head + tail, BASE)

        assert card["quiet_minutes"] == 24 * 60

    def test_an_empty_window_is_empty(self):
        card = _sensor(None, None, [], BASE)

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
            "bias",
        }


class TestBias:
    """Сверка текущего сенсора с глюкометром — основание для плашки."""

    started = BASE - timedelta(days=3)

    def pairs(self, diffs, sensor=80.0):
        """Ряд сенсора на ``sensor`` и замеры, отстоящие от него на ``-diff``."""

        series = readings(self.started, BASE)
        series = [(moment, sensor) for moment, _ in series]
        sticks = [
            (self.started + timedelta(hours=6 * (i + 1), minutes=1), sensor - diff)
            for i, diff in enumerate(diffs)
        ]
        return series, sticks

    def test_a_steady_underread_is_reported(self):
        series, sticks = self.pairs([-18, -20, -15, -30, -12, -22])

        bias = _bias(self.started, series, sticks)

        assert bias == {"mgdl": -19.0, "pairs": 6, "share": 100}

    def test_one_typo_does_not_move_the_median(self):
        series, sticks = self.pairs([-18, -20, -15, -30, -12, +105])

        assert _bias(self.started, series, sticks)["mgdl"] == -16.5

    def test_too_few_pairs_say_nothing(self):
        series, sticks = self.pairs([-18, -20, -15, -30])

        assert _bias(self.started, series, sticks) is None

    def test_noise_within_the_sensor_accuracy_says_nothing(self):
        series, sticks = self.pairs([-5, -8, -3, -6, -7])

        assert _bias(self.started, series, sticks) is None

    def test_a_split_verdict_says_nothing(self):
        series, sticks = self.pairs([-20, -25, -15, +20, +18, -30])

        assert _bias(self.started, series, sticks) is None

    def test_the_previous_sensor_does_not_count(self):
        series, sticks = self.pairs([-18, -20, -15, -30, -12])
        old = [(self.started - timedelta(hours=1), 200.0)]

        bias = _bias(self.started, series, old + sticks)

        assert bias["pairs"] == 5

    def test_a_stick_far_from_any_reading_is_not_a_pair(self):
        series, sticks = self.pairs([-18, -20, -15, -30, -12])
        series = [item for item in series if abs(item[0] - sticks[0][0]) > timedelta(minutes=10)]

        assert _bias(self.started, series, sticks) is None

    def test_the_snapshot_carries_it(self):
        series, sticks = self.pairs([-18, -20, -15, -30, -12])

        snapshot = build_snapshot(
            series, [], BASE, fingersticks=sticks, sensor_started=self.started
        )

        assert snapshot["sensor"]["bias"]["pairs"] == 5


def test_the_collector_stores_both_marks(monkeypatch):
    """run_once кладёт в базу и установку, и конец срока по модели прибора."""

    starts, ends = [], []
    monkeypatch.setattr(main, "store_readings", lambda rows: len(rows))
    monkeypatch.setattr(main, "last_readings", lambda limit=10: [])
    monkeypatch.setattr(main, "store_last_success", lambda when: None)
    monkeypatch.setattr(main, "publish", lambda **kwargs: None)
    monkeypatch.setattr(main, "store_sensor_start", lambda when: starts.append(when))
    monkeypatch.setattr(main, "store_sensor_end", lambda when: ends.append(when))

    class Known:
        started = BASE - timedelta(days=2)
        kind = 4
        lifetime_days = 15

    main.run_once(FakeCollector([(BASE, 110.0)], sensor=Known()), None, 300, 60, None)

    assert starts == [Known.started]
    assert ends == [Known.started + timedelta(days=15)]
