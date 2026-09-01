"""Postprandial analysis: rise, peak, return, and what gets excluded."""

from datetime import datetime, timedelta, timezone

from analysis import (
    SNACK_CARBS,
    TARGET_RISE,
    WINDOW,
    analyse,
    excursion,
    summarise,
    trust_level,
)


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

        assert result["curve"][-1][0] == 60

    def test_cut_marks_only_a_window_left_over_the_target(self):
        """Следующая еда пришла на подъём выше ориентира: чем он кончился,
        никто не видел."""

        readings = rising_then_back(100, 220)
        others = [START + timedelta(hours=1)]

        result = excursion((START, 60.0), readings, LATER, others, HYPO)

        assert result["rise"] > TARGET_RISE
        assert result["cut"] is True

    def test_a_rise_within_the_target_survives_the_cut(self):
        """Подъём, уложившийся в ориентир, — уже исход: обрезка его не унесла."""

        readings = rising_then_back(100, 160)
        others = [START + timedelta(hours=1)]

        result = excursion((START, 60.0), readings, LATER, others, HYPO)

        assert result["rise"] <= TARGET_RISE
        assert result["cut"] is False

    def test_truncated_window_has_no_return(self):
        """Уровень в момент следующей еды — её опора, а не возврат после этой."""

        readings = rising_then_back(100, 160)
        others = [START + timedelta(hours=1)]

        result = excursion((START, 60.0), readings, LATER, others, HYPO)

        assert result["ret"] is None

    def test_truncated_window_judges_coverage_by_its_own_length(self):
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

    def test_hypo_on_a_cut_window_still_counts(self):
        """Реакция на гипо — еда, а еда режет окно; счёт по одним чистым окнам
        терял бы ровно те гипогликемии, которые случились."""

        items = [self.make(40), self.make(TARGET_RISE + 20, hypo=True, cut=True)]

        result = summarise(items, HYPO)

        assert result["count"] == 1
        assert result["total"] == 2
        assert result["hypo"] == 1

    def test_nothing_clean_keeps_the_safety_counts(self):
        """Без чистых окон медианы мерить не по чему — но гипогликемия из
        прерванного окна не вправе пропасть со страницы вместе с ними."""

        items = [
            self.make(40, complete=False),
            self.make(TARGET_RISE + 20, hypo=True, cut=True),
        ]

        result = summarise(items, HYPO)

        assert result["count"] == 0
        assert result["total"] == 2
        assert result["rise"] is None
        assert result["peak_min"] is None
        assert result["hypo"] == 1
        assert result["skipped"] == 2

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

    def test_snack_neither_cuts_nor_gets_reviewed(self):
        """Долька шоколада не должна ни стоить обеду разбора, ни получать
        свой: её «окно» перемеряло бы кривую обеда с опоры посреди подъёма."""

        readings = rising_then_back(100, 160)
        meals = [(START, 60.0), (START + timedelta(hours=1), float(SNACK_CARBS))]

        result = analyse(meals, readings, LATER, HYPO)

        assert len(result["meals"]) == 1
        lunch = result["meals"][0]
        assert lunch["cut"] is False
        assert lunch["curve"][-1][0] == 240
        assert result["summary"]["count"] == 1

    def test_snack_does_not_steal_the_bolus(self):
        """Укол между обедом и перекусом принадлежит обеду: перекус не
        разбирается, и доза, ушедшая к нему, пропала бы со страницы."""

        readings = rising_then_back(100, 160)
        meals = [(START, 60.0), (START + timedelta(minutes=40), float(SNACK_CARBS))]
        boluses = [(START + timedelta(minutes=25), 4.0)]

        result = analyse(meals, readings, LATER, HYPO, boluses=boluses)

        assert result["meals"][0]["dose"] == {"units": 4.0, "lead_min": -25}

    def test_two_records_in_a_row_are_one_meal(self):
        """Тарелка и добавка через двадцать минут — один ужин: порознь первая
        получила бы огрызок окна, а вторая — опору посреди её подъёма."""

        readings = rising_then_back(100, 160)
        meals = [(START, 20.0), (START + timedelta(minutes=20), 20.0)]

        result = analyse(meals, readings, LATER, HYPO)

        assert len(result["meals"]) == 1
        assert result["meals"][0]["carbs"] == 40.0
        assert result["meals"][0]["curve"][-1][0] == 240

    def test_a_merged_meal_keeps_the_records_it_was_made_of(self):
        """Число в колонке углеводов сложено — страница объясняет, из чего."""

        readings = rising_then_back(100, 160)
        second = START + timedelta(minutes=20)
        meals = [(START, 20.0), (second, 20.0)]

        result = analyse(meals, readings, LATER, HYPO)

        assert result["meals"][0]["parts"] == [
            [int(START.replace(tzinfo=timezone.utc).timestamp()), 20.0],
            [int(second.replace(tzinfo=timezone.utc).timestamp()), 20.0],
        ]

    def test_a_single_record_has_nothing_to_explain(self):
        readings = rising_then_back(100, 160)

        result = analyse([(START, 60.0)], readings, LATER, HYPO)

        assert result["meals"][0]["parts"] is None

    def test_a_chain_of_records_does_not_grow_past_the_gap(self):
        """Промежуток считается от начала приёма: еда каждые двадцать минут
        иначе склеилась бы в один приём длиной в день."""

        meals = [
            (START, 20.0),
            (START + timedelta(minutes=20), 20.0),
            (START + timedelta(minutes=40), 20.0),
        ]

        result = analyse(meals, rising_then_back(100, 160), LATER, HYPO)

        assert [meal["carbs"] for meal in result["meals"]] == [40.0, 20.0]

    def test_the_second_helping_keeps_its_bolus(self):
        """Укол под добавку — доза того же ужина, а не пропавшая строка."""

        readings = rising_then_back(100, 160)
        meals = [(START, 20.0), (START + timedelta(minutes=20), 20.0)]
        boluses = [(START + timedelta(minutes=20), 4.0)]

        result = analyse(meals, readings, LATER, HYPO, boluses=boluses)

        assert result["meals"][0]["dose"] == {"units": 4.0, "lead_min": -20}

    def test_a_real_second_meal_cuts_the_first(self):
        readings = rising_then_back(100, 220)
        meals = [(START, 60.0), (START + timedelta(hours=1), SNACK_CARBS + 1.0)]

        result = analyse(meals, readings, LATER, HYPO)

        assert result["meals"][0]["cut"] is True
        assert result["meals"][0]["curve"][-1][0] == 60

    def test_bolus_near_the_meal_is_attached(self):
        readings = rising_then_back(100, 160)
        boluses = [(START - timedelta(minutes=15), 6.0)]

        result = analyse([(START, 60.0)], readings, LATER, HYPO, boluses=boluses)

        assert result["meals"][0]["dose"] == {"units": 6.0, "lead_min": 15}

    def test_distant_bolus_is_not_attached(self):
        """Коррекция за два часа до обеда — не его доза."""

        readings = rising_then_back(100, 160)
        boluses = [(START - timedelta(hours=2), 6.0)]

        result = analyse([(START, 60.0)], readings, LATER, HYPO, boluses=boluses)

        assert result["meals"][0]["dose"] is None

    def test_bolus_goes_to_the_nearest_meal_only(self):
        """Доза между двумя приёмами не должна красоваться в двух строках."""

        readings = rising_then_back(100, 160)
        meals = [(START, 60.0), (START + timedelta(minutes=40), 30.0)]
        boluses = [(START + timedelta(minutes=25), 4.0)]

        result = analyse(meals, readings, LATER, HYPO, boluses=boluses)

        assert result["meals"][0]["dose"] is None
        assert result["meals"][1]["dose"] == {"units": 4.0, "lead_min": 15}

    def test_split_dose_sums_units_and_keeps_the_first_lead(self):
        """Именно первый укол решает, успел ли инсулин к пику."""

        readings = rising_then_back(100, 160)
        boluses = [
            (START - timedelta(minutes=20), 4.0),
            (START + timedelta(minutes=10), 2.0),
        ]

        result = analyse([(START, 60.0)], readings, LATER, HYPO, boluses=boluses)

        assert result["meals"][0]["dose"] == {"units": 6.0, "lead_min": 20}

    def test_bolus_after_the_meal_has_negative_lead(self):
        readings = rising_then_back(100, 160)
        boluses = [(START + timedelta(minutes=10), 6.0)]

        result = analyse([(START, 60.0)], readings, LATER, HYPO, boluses=boluses)

        assert result["meals"][0]["dose"] == {"units": 6.0, "lead_min": -10}

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


class TestTrustLevel:
    def test_weighed_beats_a_wide_spread(self):
        """У взвешенной порции число измерено: спор прогонов был о фотографии,
        а не о том, что показала чашка."""

        assert trust_level("photo_estimate", True, 80.0, 42.4) == "weighed"

    def test_manual_entry_has_no_runs_to_agree(self):
        assert trust_level("manual", None, None, None) == "manual"

    def test_absolute_spread_overrides_the_ratio(self):
        """18 г на порции в 90 — формально 20 %, а на деле полторы хлебные
        единицы неизвестно куда."""

        assert trust_level("photo_estimate", False, 90.8, 18.4) == "low"

    def test_levels_follow_the_ratio(self):
        assert trust_level("photo_estimate", False, 30.5, 2.7) == "ok"
        assert trust_level("photo_estimate", False, 41.8, 7.8) == "medium"
        assert trust_level("photo_estimate", False, 10.0, 4.0) == "low"

    def test_a_hand_edited_number_is_spoken_too(self):
        """Кнопка «Исправить» меняет число, не взвешивая: прогоны спорили о
        своей медиане, а в журнал ушла чужая — их согласие о ней ничего не
        говорит."""

        assert trust_level("photo_estimate", False, 60.0, 3.0, confirmed=45.0) == "manual"

    def test_an_untouched_median_keeps_its_agreement(self):
        assert trust_level("photo_estimate", False, 60.0, 3.0, confirmed=60.0) == "ok"

    def test_nothing_known_is_not_a_level(self):
        assert trust_level("photo_estimate", False, None, None) is None
        assert trust_level() is None


ORIGIN_OK = {
    "source": "photo_estimate",
    "was_weighed": False,
    "median": 60.0,
    "spread": 3.0,
}
ORIGIN_LOW = {
    "source": "photo_estimate",
    "was_weighed": False,
    "median": 60.0,
    "spread": 40.0,
}


class TestTrustInAnalyse:
    def test_the_level_reaches_the_meal(self):
        readings = rising_then_back(100, 160)

        result = analyse(
            [(START, 60.0)], readings, LATER, HYPO, origins={START: [ORIGIN_OK]}
        )

        assert result["meals"][0]["trust"] == "ok"

    def test_a_merged_meal_inherits_its_worst_part(self):
        """Взвешенная половина не делает достоверной вторую, названную на
        глаз: приём стоит столько, сколько его худшая запись."""

        readings = rising_then_back(100, 160)
        addition = START + timedelta(minutes=20)
        meals = [(START, 20.0), (addition, 20.0)]

        result = analyse(
            meals,
            readings,
            LATER,
            HYPO,
            origins={
                START: [{"source": "photo_estimate", "was_weighed": True}],
                addition: [ORIGIN_LOW],
            },
        )

        assert len(result["meals"]) == 1
        assert result["meals"][0]["trust"] == "low"

    def test_two_records_on_one_second_take_the_worse(self):
        readings = rising_then_back(100, 160)

        result = analyse(
            [(START, 60.0)],
            readings,
            LATER,
            HYPO,
            origins={START: [ORIGIN_OK, ORIGIN_LOW]},
        )

        assert result["meals"][0]["trust"] == "low"

    def test_without_origins_the_meal_has_no_level(self):
        """Журнал заводит бот, и страница обязана рисоваться без его таблиц."""

        readings = rising_then_back(100, 160)

        result = analyse([(START, 60.0)], readings, LATER, HYPO)

        assert result["meals"][0]["trust"] is None
