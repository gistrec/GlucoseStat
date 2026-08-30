"""Tests for the snapshot maths.

Everything here is a pure function over a list of readings — no database, no
network. Importing ``publish`` is safe without configuration because the
engine is built lazily on first use.
"""

import json
from datetime import datetime, timedelta

import pytest

from publish import (
    TARGET_HIGH_MGDL,
    TARGET_LOW_MGDL,
    CGM_READINGS_PER_DAY,
    _downsample,
    _events,
    _gmi,
    _stats,
    _stored_last_success,
    _trend,
    publish,
)


BASE = datetime(2026, 8, 22, 12, 0, 0)


def readings(*values, step_minutes=5, start=BASE):
    """Build readings spaced evenly apart."""

    return [(start + timedelta(minutes=step_minutes * i), v) for i, v in enumerate(values)]


class TestDownsample:
    def test_empty_input_gives_no_points(self):
        assert _downsample([], 5) == []

    def test_averages_within_a_bucket(self):
        # Три замера в пределах одного 15-минутного окна складываются в один.
        data = readings(100, 110, 120, step_minutes=1)
        points = _downsample(data, 15)

        assert len(points) == 1
        assert points[0][1] == 110

    def test_separate_buckets_stay_separate(self):
        data = readings(100, 200, step_minutes=60)
        points = _downsample(data, 15)

        assert [p[1] for p in points] == [100, 200]

    def test_points_are_sorted_by_time(self):
        data = readings(100, 110, 120, step_minutes=30)
        points = _downsample(data, 15)

        assert [p[0] for p in points] == sorted(p[0] for p in points)

    def test_bucket_timestamps_align_to_the_step(self):
        step = 15
        points = _downsample(readings(100, 110, step_minutes=7), step)

        assert all(p[0] % (step * 60) == 0 for p in points)


class TestStats:
    def test_no_readings_gives_nothing(self):
        assert _stats([]) is None

    def test_counts_time_in_range(self):
        # Ровно половина внутри 70–180, четверть ниже, четверть выше.
        data = readings(100, 150, 50, 250)
        stats = _stats(data)

        assert stats["tir"] == 50.0
        assert stats["below"] == 25.0
        assert stats["above"] == 25.0
        assert stats["tir"] + stats["below"] + stats["above"] == 100.0

    def test_range_boundaries_count_as_in_range(self):
        stats = _stats(readings(TARGET_LOW_MGDL, TARGET_HIGH_MGDL))

        assert stats["tir"] == 100.0

    def test_reports_average_and_extremes(self):
        stats = _stats(readings(80, 100, 120))

        assert stats["avg"] == 100.0
        assert stats["min"] == 80
        assert stats["max"] == 120
        assert stats["count"] == 3

    def test_constant_readings_have_no_variation(self):
        assert _stats(readings(120, 120, 120))["cv"] == 0.0

    def test_variation_grows_with_spread(self):
        steady = _stats(readings(110, 120, 130))["cv"]
        jumpy = _stats(readings(60, 120, 180))["cv"]

        assert jumpy > steady


class TestTrend:
    def test_no_readings_gives_nothing(self):
        assert _trend([]) is None

    def test_uses_the_latest_reading(self):
        latest = _trend(readings(100, 150, 200))

        assert latest["mgdl"] == 200

    def test_rate_is_per_minute_over_the_trend_window(self):
        # 15 минут, +30 мг/дл на всём окне -> 2 мг/дл в минуту.
        data = readings(100, 130, step_minutes=15)
        latest = _trend(data)

        assert latest["rate"] == pytest.approx(2.0)

    def test_falling_glucose_gives_a_negative_rate(self):
        latest = _trend(readings(200, 100, step_minutes=20))

        assert latest["rate"] < 0

    def test_a_lone_reading_has_no_rate(self):
        # Не с чем сравнивать: скорость неизвестна, а не равна нулю.
        assert _trend(readings(120))["rate"] is None

    def test_readings_inside_the_window_do_not_set_a_rate(self):
        # Оба замера моложе окна тренда — сравнивать по-прежнему не с чем.
        assert _trend(readings(100, 110, step_minutes=1))["rate"] is None


class TestEvents:
    def entry(self, kind, minutes_ago, carbs=None, units=None):
        return (BASE - timedelta(minutes=minutes_ago), kind, carbs, units)

    def test_splits_into_three_lanes(self):
        journal = [
            self.entry("meal", 60, carbs=62.0),
            self.entry("bolus", 75, units=6.0),
            self.entry("basal", 600, units=18.0),
        ]

        lanes = _events(journal, BASE - timedelta(days=1))

        assert len(lanes["meals"]) == 1
        assert len(lanes["bolus"]) == 1
        assert len(lanes["basal"]) == 1
        assert lanes["meals"][0][1] == 62.0

    def test_older_than_the_window_is_dropped(self):
        # Панель событий рисуется только на суточном окне: сотня отметок за
        # месяц сливается в сплошную полосу.
        journal = [self.entry("meal", 60 * 30, carbs=62.0)]

        assert _events(journal, BASE - timedelta(days=1))["meals"] == []

    def test_entry_without_an_amount_is_skipped(self):
        # Столбик нулевой высоты неотличим от отсутствия столбика.
        journal = [self.entry("meal", 60), self.entry("bolus", 60)]

        lanes = _events(journal, BASE - timedelta(days=1))

        assert lanes["meals"] == [] and lanes["bolus"] == []

    def test_unknown_kind_does_not_break_publishing(self):
        # Бот может завести новый вид записи раньше, чем дашборд про него узнает.
        journal = [self.entry("exercise", 60, units=30.0)]

        lanes = _events(journal, BASE - timedelta(days=1))

        assert lanes == {"meals": [], "bolus": [], "basal": []}


class TestJournalAbsent:
    """Дашборд обязан пережить отсутствие журнала.

    Таблицу заводит бот, и до его первой выкладки её на сервере нет. Если
    запрос к ней уронит publish(), страница перестанет обновляться — то есть
    выкладка коллектора сломает то, что работало годами.
    """

    def test_missing_table_yields_no_events(self, monkeypatch):
        from sqlalchemy import create_engine

        from database import connection
        from database.queries import journal_since

        # База без единой таблицы — ровно то, что увидит коллектор до бота
        empty = create_engine("sqlite://")
        monkeypatch.setattr(connection, "_engine", lambda: empty)

        assert journal_since(BASE - timedelta(days=1)) == []


class TestGmi:
    """GMI считается по своим двум неделям, а не по выбранному на странице окну."""

    EXPECTED_AT_100 = 3.31 + 0.02392 * 100

    def fortnight(self, value=100.0):
        count = CGM_READINGS_PER_DAY * 14
        start = BASE - timedelta(days=14)
        return [(start + timedelta(minutes=5 * i), value) for i in range(count)]

    def test_follows_the_published_formula(self):
        # Bergenstal et al. 2018: GMI = 3.31 + 0.02392 × среднее в мг/дл.
        result = _gmi(self.fortnight(100.0), BASE)

        assert result["value"] == pytest.approx(self.EXPECTED_AT_100, abs=0.05)
        assert result["days"] == 14

    def test_thin_coverage_yields_nothing(self):
        """Расчётный HbA1c по трём дням выглядит так же солидно, как по
        четырнадцати, а означает совсем другое."""

        sparse = self.fortnight()[: CGM_READINGS_PER_DAY * 3]

        assert _gmi(sparse, BASE) is None

    def test_readings_older_than_the_window_do_not_count(self):
        # Тысяча измерений по 400 мг/дл сдвинула бы среднее, попади они в расчёт
        old = [
            (BASE - timedelta(days=30) + timedelta(minutes=5 * i), 400.0)
            for i in range(1000)
        ]

        result = _gmi(old + self.fortnight(100.0), BASE)

        assert result["value"] == pytest.approx(self.EXPECTED_AT_100, abs=0.05)

    def test_no_readings(self):
        assert _gmi([], BASE) is None


class TestStoredLastSuccess:
    """The collector's memory dies with its process; the snapshot's does not."""

    def test_inherits_the_mark_from_the_previous_snapshot(self, tmp_path):
        path = tmp_path / "data.json"
        path.write_text('{"collector": {"last_success": 1756500000}}', encoding="utf-8")

        assert _stored_last_success(str(path)) == 1756500000.0

    def test_missing_file_means_nothing_to_inherit(self, tmp_path):
        assert _stored_last_success(str(tmp_path / "data.json")) is None

    def test_corrupt_or_alien_json_means_nothing_to_inherit(self, tmp_path):
        # Включая истинные, но нечисловые значения: чужой файл не должен
        # ронять каждую публикацию, пока его не поправят руками.
        path = tmp_path / "data.json"
        alien = [
            "{not json",
            '{"collector": null}',
            '{"collector": {"last_success": "yesterday"}}',
            '{"collector": {"last_success": [1, 2]}}',
        ]

        for content in alien:
            path.write_text(content, encoding="utf-8")
            assert _stored_last_success(str(path)) is None, content

    def test_null_mark_stays_null(self, tmp_path):
        # «Сборщик ещё не получал данные» — честное состояние свежей установки,
        # его наследование не должно превращать null в ошибку.
        path = tmp_path / "data.json"
        path.write_text('{"collector": {"last_success": null}}', encoding="utf-8")

        assert _stored_last_success(str(path)) is None


class TestPublishCarryForward:
    def test_standalone_run_keeps_the_previous_mark(self, tmp_path, monkeypatch):
        """Разовый пересбор снимка не должен вешать на живую страницу красное
        «сборщик ещё не получал данные» под свежими цифрами."""

        monkeypatch.setattr("publish.readings_since", lambda since: [])
        monkeypatch.setattr("publish.journal_since", lambda since: [])
        monkeypatch.setattr("publish.last_readings", list)

        path = str(tmp_path / "data.json")
        publish(path=path, last_success=1756500000.0)
        publish(path=path)

        with open(path, encoding="utf-8") as handle:
            snapshot = json.load(handle)

        assert snapshot["collector"]["last_success"] == 1756500000
