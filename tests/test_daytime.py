"""Перцентили, на которых стоят и подневная сводка, и профиль обычного дня."""

import pytest

from daytime import _percentile


class TestPercentile:
    def test_a_single_value_is_every_percentile(self):
        # День с единственным замером бывает при смене сенсора: p25 = p50 = p75,
        # а не исключение и не три случайных копии — см. комментарий в daytime.
        for q in (0, 25, 50, 75, 100):
            assert _percentile([137.0], q) == 137.0

    def test_median_of_an_odd_list_is_the_middle_value(self):
        assert _percentile([3.0, 1.0, 2.0], 50) == 2.0

    def test_interpolates_between_neighbours(self):
        # R type 7 на [10, 20, 30, 40]: позиция p25 — 0.75, между 10 и 20.
        assert _percentile([10.0, 20.0, 30.0, 40.0], 25) == pytest.approx(17.5)

    def test_extremes_are_min_and_max(self):
        values = [5.0, 1.0, 9.0]

        assert _percentile(values, 0) == 1.0
        assert _percentile(values, 100) == 9.0

    def test_does_not_reorder_the_callers_list(self):
        values = [3.0, 1.0, 2.0]
        _percentile(values, 50)

        assert values == [3.0, 1.0, 2.0]
