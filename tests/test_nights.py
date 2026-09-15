"""Ночная сводка: два знаменателя, минимум по сырью, дрейф натощак.

``BASE`` — местная полночь (см. conftest), поэтому ночь «BASE минус сутки» —
целая: от 00:00 до 06:00 по Белграду.
"""

from datetime import timedelta

from conftest import BASE

from daytime import DISPLAY_TZ
from nights import (
    DRIFT_FROM_HOUR,
    FASTING_GAP,
    NIGHT_MIN_COVERAGE,
    NIGHTS_MIN_MEDIAN,
    SPARK_SLOT_MINUTES,
    night_summary,
)

HYPO = 70


def night_of(days_ago, values=None, value=120.0, step_minutes=5):
    """Ночь 00:00–06:00 тех суток, что кончились ``days_ago`` дней назад.

    ``values`` — функция от номера замера: так тест задаёт провал в конкретную
    минуту, не перечисляя все семьдесят две точки.
    """

    midnight = BASE - timedelta(days=days_ago)
    count = int(6 * 60 / step_minutes)
    return [
        (
            midnight + timedelta(minutes=step_minutes * i),
            value if values is None else values(i),
        )
        for i in range(count)
    ]


def week(nights=7, **kwargs):
    readings = []
    for day in range(nights, 0, -1):
        readings += night_of(day, **kwargs)
    return readings


def supper(days_ago, hours_before_drift=None):
    """Еда и короткий перед ночью ``days_ago``, за N часов до начала дрейфа.

    Отсчёт от 03:00, а не от полуночи: правило «натощак» смотрит именно на этот
    разрыв, и тест не должен пересчитывать его в уме при каждой правке
    ``FASTING_GAP``.
    """

    if hours_before_drift is None:
        hours_before_drift = FASTING_GAP.total_seconds() / 3600 + 1

    moment = (
        BASE
        - timedelta(days=days_ago)
        + timedelta(hours=DRIFT_FROM_HOUR)
        - timedelta(hours=hours_before_drift)
    )
    return [
        (moment, "meal", 60.0, None),
        (moment, "bolus", None, 5.0),
    ]


def suppers(nights=7, **kwargs):
    journal = []
    for day in range(nights, 0, -1):
        journal += supper(day, **kwargs)
    return sorted(journal)


class TestCounting:
    def test_a_quiet_week_counts_every_night(self):
        summary = night_summary(week(), suppers(), BASE, HYPO)

        assert summary["tz"] == DISPLAY_TZ
        assert summary["counted"] == 7
        assert summary["hypo_nights"] == 0
        assert len(summary["nights"]) == 7

    def test_the_night_counts_its_minutes_below(self):
        # Флага «была гипогликемия» мало: минута под порогом и полчаса под ним
        # — разные ночи. Три замера подряд с шагом в пять минут плюс возврат
        # выше порога дают пятнадцать минут эпизода.
        readings = week(nights=6) + night_of(
            7, values=lambda i: 65.0 if 40 <= i <= 42 else 120.0
        )

        summary = night_summary(readings, suppers(), BASE, HYPO)
        low = next(item for item in summary["nights"] if item["hypo"])

        assert low["low_count"] == 1
        assert low["low_minutes"] == 20
        assert summary["hypo_minutes"] == 20

    def test_a_quiet_week_has_no_minutes_below(self):
        summary = night_summary(week(), suppers(), BASE, HYPO)

        assert summary["hypo_minutes"] == 0

    def test_a_single_low_reading_makes_the_night_hypo(self):
        # Ровно тот случай, ради которого ночь считается по сырью: один замер
        # 65 мг/дл в 03:20. На недельной ломаной он попадает в корзину с
        # соседями и показывается как 4,3 ммоль — гипогликемии будто не было.
        readings = week(nights=6) + night_of(
            7, values=lambda i: 65.0 if i == 40 else 120.0
        )

        summary = night_summary(readings, suppers(), BASE, HYPO)

        assert summary["hypo_nights"] == 1
        assert summary["counted"] == 7
        low = next(item for item in summary["nights"] if item["hypo"])
        assert low["min"] == 65

    def test_a_night_below_coverage_is_not_counted(self):
        # Два часа из шести: «гипогликемии не было» по трети ночи — это не
        # факт, а его видимость.
        thin = [
            point
            for point in night_of(7)
            if point[0] < BASE - timedelta(days=7) + timedelta(hours=2)
        ]

        summary = night_summary(week(nights=6) + thin, suppers(), BASE, HYPO)

        assert summary["counted"] == 6
        skipped = next(
            item for item in summary["nights"] if item["coverage"] < 100 * NIGHT_MIN_COVERAGE
        )
        assert skipped["count"] > 0

    def test_a_night_without_readings_still_takes_its_place(self):
        summary = night_summary(week(nights=6), suppers(), BASE, HYPO)

        assert summary["counted"] == 6
        assert len(summary["nights"]) == 7
        empty = [item for item in summary["nights"] if item["count"] == 0]
        assert len(empty) == 1
        assert "min" not in empty[0]

    def test_no_covered_nights_publish_nothing(self):
        assert night_summary([], [], BASE, HYPO) is None


class TestDrift:
    def rising(self, i):
        """+36 мг/дл (2 ммоль) ровно между 03:00 и 06:00."""

        return 100.0 if i < 36 else 136.0

    def test_drift_is_measured_from_three_to_six(self):
        summary = night_summary(
            week(values=self.rising), suppers(), BASE, HYPO
        )

        assert summary["clean"] == 7
        assert summary["drift_median"] == 36

    def test_a_late_supper_leaves_the_night_dirty(self):
        # Час до порога не дотянул: короткий ещё работает, и разность мерила бы
        # его, а не базал.
        late = suppers(hours_before_drift=FASTING_GAP.total_seconds() / 3600 - 1)

        summary = night_summary(week(values=self.rising), late, BASE, HYPO)

        assert summary["clean"] == 0
        assert summary["drift_median"] is None
        # Счёт ночей при этом не страдает: безопасность считается по всем.
        assert summary["counted"] == 7

    def test_the_gap_counts_from_the_drift_hour(self):
        # Ровно порог — уже натощак: граница включающая, и ужин, отстоящий от
        # трёх ночи на FASTING_GAP, проходит. Ради этого порог и опущен —
        # обычный ужин до полуночи обязан давать зачётную ночь.
        journal = suppers(hours_before_drift=FASTING_GAP.total_seconds() / 3600)

        summary = night_summary(week(values=self.rising), journal, BASE, HYPO)

        assert summary["clean"] == 7

    def test_a_night_snack_leaves_the_night_dirty(self):
        journal = suppers()
        journal.append((BASE - timedelta(days=7) + timedelta(hours=4), "meal", 20.0, None))

        summary = night_summary(week(values=self.rising), sorted(journal), BASE, HYPO)

        assert summary["clean"] == 6

    def test_an_unrecorded_evening_is_not_a_fasting_night(self):
        # Пустой вечер — это «не записал», а не «не ел»: объявить такую ночь
        # чистой значило бы посчитать дрейф там, где о ней ничего не известно.
        summary = night_summary(week(values=self.rising), [], BASE, HYPO)

        assert summary["clean"] == 0
        assert summary["drift_median"] is None

    def test_too_few_clean_nights_publish_no_median(self):
        journal = suppers(nights=NIGHTS_MIN_MEDIAN - 1)

        summary = night_summary(week(values=self.rising), journal, BASE, HYPO)

        assert summary["clean"] == NIGHTS_MIN_MEDIAN - 1
        assert summary["drift_median"] is None
        # Минимум считается по своему знаменателю и от чистоты не зависит.
        assert summary["min_median"] == 100


class TestSpark:
    def test_the_line_keeps_the_dip_the_number_names(self):
        # Корзина отдаёт минимум, а не среднее: нижняя точка линии — тот же
        # замер, который подписан числом рядом.
        readings = night_of(1, values=lambda i: 65.0 if i == 40 else 120.0)

        summary = night_summary(readings + week(nights=7), suppers(), BASE, HYPO)
        night = next(item for item in summary["nights"] if item.get("hypo"))

        assert min(value for _, value in night["points"]) == night["min"] == 65

    def test_the_line_is_thinned_to_its_slots(self):
        summary = night_summary(week(), suppers(), BASE, HYPO)
        night = next(item for item in summary["nights"] if item["count"])

        assert len(night["points"]) == 6 * 60 // SPARK_SLOT_MINUTES


class TestWindowEdges:
    def test_tonight_is_partial_and_carries_no_drift(self):
        # «Сейчас» — четыре утра: ночь ещё не кончилась, шести часов не было.
        now = BASE + timedelta(hours=4)
        tonight = [
            point
            for point in night_of(0, value=100.0)
            if point[0] < now
        ]

        summary = night_summary(week() + tonight, suppers(), now, HYPO)
        last = summary["nights"][-1]

        assert last["partial"] is True
        assert last["drift"] is None
        # В знаменателях её нет: половина ночи без гипогликемии — не то же
        # самое, что ночь без неё. Старейшая ночь окна обрезана его левым
        # краем — по той же причине и с тем же исходом, поэтому из восьми
        # записей зачтены шесть.
        assert len(summary["nights"]) == 8
        assert summary["nights"][0]["partial"] is True
        assert summary["counted"] == 6

    def test_the_drift_hour_is_local(self):
        summary = night_summary(week(), suppers(), BASE, HYPO)

        assert summary["drift_from"] == DRIFT_FROM_HOUR
        assert summary["from"] == 0
        assert summary["to"] == 6
