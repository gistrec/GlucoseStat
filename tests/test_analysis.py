"""Postprandial analysis: rise, peak, return, and what gets excluded."""

from datetime import datetime, timedelta

from analysis import SNACK_CARBS, TARGET_RISE, WINDOW, analyse, excursion, summarise


HYPO = 70
START = datetime(2026, 8, 28, 12, 0)


def curve(values: list[float], start: datetime = START, step_minutes: int = 5):
    """Показания с шагом 5 минут, как их отдаёт сенсор."""

    return [
        (start + timedelta(minutes=step_minutes * i), value)
        for i, value in enumerate(values)
    ]


def flat(value: float, minutes: int, start: datetime = START):
    return curve([value] * (minutes // 5 + 1), start=start)


def rising_then_back(baseline: float, peak: float, minutes: int = 240):
    """Кривая, поднимающаяся к пику на середине окна и возвращающаяся к опоре."""

    steps = minutes // 5
    values = []
    for i in range(steps + 1):
        half = steps / 2
        share = i / half if i <= half else (steps - i) / half
        values.append(baseline + (peak - baseline) * share)
    return curve(values)


LATER = START + WINDOW + timedelta(hours=1)


class TestExcursion:
    def test_reads_baseline_peak_and_return(self):
        readings = rising_then_back(100, 160)

        result = excursion((START, 60.0), readings, LATER, [], HYPO)

        assert result["baseline"] == 100
        assert result["peak"] == 160
        assert result["peak_min"] == 120
        assert result["rise"] == 60
        assert result["ret"] == 0
        assert result["complete"] is True

    def test_no_baseline_means_no_analysis(self):
        """Сенсор молчал в момент еды — подъём не от чего считать."""

        readings = flat(120, 240, start=START + timedelta(hours=1))

        assert excursion((START, 60.0), readings, LATER, [], HYPO) is None

    def test_hypo_in_the_window_is_flagged(self):
        readings = curve([120, 110, 90, 68, 80, 95] + [110] * 43)

        result = excursion((START, 60.0), readings, LATER, [], HYPO)

        assert result["hypo"] is True

    def test_a_low_before_the_meal_is_not_counted(self):
        """Окно начинается с приёма пищи: гипогликемия получасом раньше — это
        событие предыдущего окна, а не следствие этой еды."""

        readings = curve([60, 65], start=START - timedelta(minutes=30)) + flat(120, 240)

        result = excursion((START, 60.0), readings, LATER, [], HYPO)

        assert result["hypo"] is False

    def test_second_meal_cuts_the_window_short(self):
        """Точки после следующей еды принадлежат двум событиям сразу."""

        readings = rising_then_back(100, 160)
        others = [START + timedelta(hours=1)]

        result = excursion((START, 60.0), readings, LATER, others, HYPO)

        assert result["cut"] is True
        assert result["curve"][-1][0] == 60

    def test_cut_window_has_no_return(self):
        """Уровень в момент следующей еды — её опора, а не возврат после этой."""

        readings = rising_then_back(100, 160)
        others = [START + timedelta(hours=1)]

        result = excursion((START, 60.0), readings, LATER, others, HYPO)

        assert result["ret"] is None

    def test_cut_window_judges_coverage_by_its_own_length(self):
        """Часовому огрызку хватает дюжины точек — мерить его полными четырьмя
        часами значило бы браковать каждое обрезанное окно."""

        readings = rising_then_back(100, 160)
        others = [START + timedelta(hours=1)]

        result = excursion((START, 60.0), readings, LATER, others, HYPO)

        assert result["complete"] is True

    def test_a_meal_before_the_window_does_not_cut(self):
        readings = rising_then_back(100, 160)
        others = [START - timedelta(hours=1)]

        result = excursion((START, 60.0), readings, LATER, others, HYPO)

        assert result["cut"] is False
        assert result["curve"][-1][0] == 240

    def test_unfinished_window_is_incomplete(self):
        """Окно ещё не закрылось: пик может быть впереди."""

        readings = curve([100, 110, 130, 150])

        result = excursion((START, 60.0), readings, START + timedelta(minutes=20), [], HYPO)

        assert result["complete"] is False

    def test_sparse_window_is_incomplete(self):
        """Четыре точки на четыре часа — форму кривой по ним не прочитать."""

        readings = curve([100, 140, 130, 105], step_minutes=60)

        result = excursion((START, 60.0), readings, LATER, [], HYPO)

        assert result["complete"] is False

    def test_missing_ending_leaves_return_unknown(self):
        """«Вернулось к исходному» подтверждается показанием, а не его
        отсутствием."""

        readings = flat(120, 180)  # обрывается за час до конца окна

        result = excursion((START, 60.0), readings, LATER, [], HYPO)

        assert result["ret"] is None

    def test_curve_is_offset_from_the_meal(self):
        result = excursion((START, 60.0), rising_then_back(100, 160), LATER, [], HYPO)

        assert result["curve"][0][0] == 0
        assert result["curve"][-1][0] == 240


class TestSummarise:
    def make(self, rise, hypo=False, complete=True, cut=False, peak_min=90):
        return {
            "rise": rise,
            "hypo": hypo,
            "complete": complete,
            "cut": cut,
            "peak_min": peak_min,
        }

    def test_median_not_mean(self):
        """Один праздничный обед не должен сдвигать картину обычной недели."""

        items = [self.make(40), self.make(45), self.make(200)]

        assert summarise(items, HYPO)["rise"] == 45

    def test_incomplete_and_cut_are_skipped(self):
        items = [
            self.make(40),
            self.make(999, complete=False),
            self.make(999, cut=True),
        ]

        result = summarise(items, HYPO)

        assert result["count"] == 1
        assert result["skipped"] == 2

    def test_good_needs_both_a_small_rise_and_no_low(self):
        """Подъём в пределах ориентира, добытый ценой гипогликемии, —
        не тот исход, который стоит считать удачным."""

        items = [
            self.make(TARGET_RISE - 10),
            self.make(TARGET_RISE - 10, hypo=True),
            self.make(TARGET_RISE + 50),
        ]

        result = summarise(items, HYPO)

        assert result["good"] == 1
        assert result["hypo"] == 1

    def test_nothing_clean_means_no_summary(self):
        assert summarise([self.make(40, complete=False)], HYPO) is None

    def test_empty_input(self):
        assert summarise([], HYPO) is None


class TestAnalyse:
    def test_ties_it_together(self):
        readings = rising_then_back(100, 160)
        result = analyse([(START, 60.0)], readings, LATER, HYPO)

        assert result["window_min"] == 240
        assert result["targets"]["hypo"] == HYPO
        assert len(result["meals"]) == 1
        assert result["summary"]["count"] == 1

    def test_snack_does_not_cut_a_meal_window(self):
        """Долька шоколада не должна стоить обеду его разбора."""

        readings = rising_then_back(100, 160)
        meals = [(START, 60.0), (START + timedelta(hours=1), float(SNACK_CARBS))]

        result = analyse(meals, readings, LATER, HYPO)

        lunch = result["meals"][0]
        assert lunch["cut"] is False
        assert lunch["curve"][-1][0] == 240

    def test_a_real_second_meal_cuts_the_first(self):
        readings = rising_then_back(100, 160)
        meals = [(START, 60.0), (START + timedelta(hours=1), SNACK_CARBS + 1.0)]

        result = analyse(meals, readings, LATER, HYPO)

        assert result["meals"][0]["cut"] is True
        assert result["meals"][0]["curve"][-1][0] == 60

    def test_meal_without_readings_disappears(self):
        result = analyse([(START, 60.0)], [], LATER, HYPO)

        assert result["meals"] == []
        assert result["summary"] is None

    def test_limit_keeps_the_freshest(self):
        """Обрезаем старые: свежие окна интереснее, а каждая кривая — полсотни
        точек в снимке."""

        meals, readings = [], []
        for day in range(5):
            moment = START + timedelta(days=day)
            meals.append((moment, 60.0))
            readings.extend(rising_then_back(100, 160 + day)[:49])
            readings = [
                (timestamp + timedelta(days=0), value) for timestamp, value in readings
            ]

        # Каждый приём пищи со своей кривой, начинающейся в его же момент
        readings = []
        for day in range(5):
            readings.extend(
                (moment + timedelta(days=day), value)
                for moment, value in rising_then_back(100, 160 + day)
            )

        result = analyse(meals, readings, LATER + timedelta(days=5), HYPO, limit=2)

        assert len(result["meals"]) == 2
        assert result["meals"][0]["t"] < result["meals"][1]["t"]
