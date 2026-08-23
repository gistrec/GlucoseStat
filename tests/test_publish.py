"""Tests for the snapshot maths.

Everything here is a pure function over a list of readings — no database, no
network. Importing ``publish`` is safe without configuration because the
engine is built lazily on first use.
"""

from datetime import datetime, timedelta

import pytest

from publish import (
    TARGET_HIGH_MGDL,
    TARGET_LOW_MGDL,
    _downsample,
    _stats,
    _trend,
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

    def test_gmi_follows_the_published_formula(self):
        # Bergenstal et al. 2018: GMI = 3.31 + 0.02392 × среднее в мг/дл.
        stats = _stats(readings(100, 100))

        assert stats["gmi"] == pytest.approx(3.31 + 0.02392 * 100, abs=0.05)

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
