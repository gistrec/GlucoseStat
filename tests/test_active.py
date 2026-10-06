"""Остаток короткого инсулина и углеводов — плашка над графиком."""

from datetime import timedelta, timezone

import pytest

from active import active_now, cob_fraction, iob_fraction
from conftest import BASE


def ts(moment):
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


class TestCurves:
    def test_insulin_is_whole_at_the_shot_and_gone_after_five_hours(self):
        assert iob_fraction(0) == pytest.approx(1.0)
        assert iob_fraction(300) == 0.0
        assert iob_fraction(-5) == 0.0

    def test_insulin_only_decays(self):
        values = [iob_fraction(m) for m in range(0, 300, 5)]
        assert values == sorted(values, reverse=True)

    def test_carbs_wait_fifteen_minutes_then_go_linearly(self):
        assert cob_fraction(10) == 1.0
        assert cob_fraction(15 + 120) == pytest.approx(0.5)
        assert cob_fraction(15 + 240) == 0.0


class TestActiveNow:
    def test_empty_journal_says_nothing(self):
        assert active_now([], BASE) == {"insulin": None, "carbs": None}

    def test_bolus_reports_what_is_left_of_what(self):
        shot = BASE - timedelta(minutes=138)
        active = active_now([(shot, "bolus", None, 5.0)], BASE)["insulin"]

        assert active["of"] == 5.0
        assert active["left"] == pytest.approx(5 * iob_fraction(138), abs=0.05)
        assert active["last"] == ts(shot)
        assert active["peak"] == ts(shot + timedelta(minutes=75))
        assert active["until"] == ts(shot + timedelta(minutes=300))

    def test_spent_entries_drop_out_of_the_total(self):
        # Вчерашний укол не должен удлинять знаменатель полосы.
        journal = [
            (BASE - timedelta(hours=8), "bolus", None, 6.0),
            (BASE - timedelta(minutes=30), "bolus", None, 2.0),
        ]
        insulin = active_now(journal, BASE)["insulin"]
        assert insulin["of"] == 2.0
        assert insulin["count"] == 1

    def test_several_shots_add_up(self):
        journal = [
            (BASE - timedelta(hours=3), "bolus", None, 4.0),
            (BASE - timedelta(hours=1), "bolus", None, 2.0),
            (BASE - timedelta(minutes=20), "bolus", None, 1.0),
        ]
        insulin = active_now(journal, BASE)["insulin"]

        assert insulin["of"] == 7.0
        assert insulin["count"] == 3
        assert insulin["left"] == pytest.approx(
            4 * iob_fraction(180) + 2 * iob_fraction(60) + iob_fraction(20), abs=0.05
        )
        # Пик и конец действия — у последнего укола.
        assert insulin["last"] == ts(BASE - timedelta(minutes=20))

    def test_basal_is_not_counted(self):
        journal = [(BASE - timedelta(minutes=30), "basal", None, 14.0)]
        assert active_now(journal, BASE)["insulin"] is None

    def test_meal_absorbs_until_its_own_end(self):
        meal = BASE - timedelta(minutes=135)
        carbs = active_now([(meal, "meal", 60.0, None)], BASE)["carbs"]

        assert carbs == {
            "left": 30.0,
            "of": 60.0,
            "count": 1,
            "last": ts(meal),
            "until": ts(meal + timedelta(minutes=255)),
        }

    def test_tail_below_threshold_is_silent(self):
        journal = [(BASE - timedelta(minutes=299), "bolus", None, 1.0)]
        assert active_now(journal, BASE)["insulin"] is None

    def test_entries_without_amount_are_skipped(self):
        journal = [
            (BASE - timedelta(minutes=10), "meal", None, None),
            (BASE - timedelta(minutes=10), "pen_bolus", None, None),
        ]
        assert active_now(journal, BASE) == {"insulin": None, "carbs": None}
