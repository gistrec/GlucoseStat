"""Профиль обычного дня: слоты по местному времени, квартили по дням."""

from datetime import datetime, timedelta

from agp import (
    AGP_MIN_DAYS,
    AGP_MIN_SLOT_DAYS,
    AGP_SLOT_MINUTES,
    AGP_WINDOW,
    day_profile,
)
from conftest import BASE
from daytime import DISPLAY_TZ

SLOTS = 24 * 60 // AGP_SLOT_MINUTES


def full_day(midnight, value=120.0, hours=24):
    """Сутки с замером каждые 5 минут — номинальная плотность сенсора."""

    return [
        (midnight + timedelta(minutes=5 * i), value) for i in range(int(hours * 12))
    ]


def fortnight(days=14, value=120.0):
    """Полные дни, заканчивающиеся местной полуночью BASE."""

    readings = []
    for day in range(days, 0, -1):
        readings += full_day(BASE - timedelta(days=day), value)
    return readings


class TestDayProfile:
    def test_a_full_fortnight_fills_every_slot(self):
        profile = day_profile(fortnight(), BASE)

        assert profile["tz"] == DISPLAY_TZ
        assert profile["slot_min"] == AGP_SLOT_MINUTES
        assert profile["days"] == AGP_WINDOW.days
        assert len(profile["slots"]) == SLOTS
        assert all(slot is not None for slot in profile["slots"])

    def test_too_few_days_publish_nothing(self):
        # Коридор по паре суток выглядит так же уверенно, как по четырнадцати,
        # а означает совсем другое — прецедент тот же, что у GMI.
        assert day_profile(fortnight(days=AGP_MIN_DAYS - 1), BASE) is None

    def test_no_readings_publish_nothing(self):
        assert day_profile([], BASE) is None

    def test_quartiles_weigh_days_not_readings(self):
        # Семь дней по 100 и день, где сенсор отдал в нулевой слот три точки
        # по 200: день схлопывается в одно значение, и медиана остаётся 100.
        # Взвешивай по замерам — тройной день перетянул бы её к 200.
        readings = fortnight(days=7, value=100.0)
        odd_day = BASE - timedelta(days=8)
        readings += [
            (moment, 200.0 if moment < odd_day + timedelta(minutes=15) else 100.0)
            for moment, _ in full_day(odd_day)
        ]

        slot = day_profile(readings, BASE)["slots"][0]

        assert slot == [100, 100, 100, 8]

    def test_a_reading_at_local_midnight_lands_in_slot_zero(self):
        # BASE — ровно местная полночь: замер в 00:00 обязан попасть в слот 0,
        # а не в последний слот предыдущих суток.
        readings = []
        for moment, value in fortnight():
            local_minute = (moment.hour * 60 + moment.minute + 2 * 60) % 1440
            readings.append((moment, 240.0 if local_minute == 0 else value))

        slots = day_profile(readings, BASE)["slots"]

        # В слоте 0 три замера на день (00:00, 00:05, 00:10), полуночный — 240.
        assert slots[0][1] == round((240 + 120 + 120) / 3)
        assert slots[1][1] == 120

    def test_spring_forward_day_shortchanges_the_morning_slots(self):
        # В ночь на 29 марта 2026 Белград прыгает с 02:00 на 03:00: местного
        # часа 02:00–03:00 не существует, и слоты 8–11 недосчитываются дня.
        now = datetime(2026, 3, 29, 22, 0, 0)  # местная полночь 30 марта
        readings = [
            (now - AGP_WINDOW + timedelta(minutes=5 * i), 120.0)
            for i in range(AGP_WINDOW.days * 288)
        ]

        slots = day_profile(readings, now)["slots"]

        for slot in range(8, 12):
            assert slots[slot][3] == slots[0][3] - 1

    def test_a_sparse_slot_is_null_not_a_guess(self):
        # Половина суток покрыта полностью, вторая — нет вовсе: пустые слоты
        # публикуются как null, а профиль целиком выживает ровно на границе
        # AGP_MIN_SLOT_SHARE.
        readings = []
        for day in range(14, 0, -1):
            readings += full_day(BASE - timedelta(days=day), hours=12)

        profile = day_profile(readings, BASE)

        assert profile is not None
        assert profile["slots"][0] is not None
        assert profile["slots"][SLOTS - 1] is None

    def test_less_than_half_the_slots_kills_the_profile(self):
        # Коридор на четверти ширины холста неотличим от сбоя отрисовки.
        readings = []
        for day in range(14, 0, -1):
            readings += full_day(BASE - timedelta(days=day), hours=11)

        assert day_profile(readings, BASE) is None

    def test_slots_thinner_than_the_floor_are_null(self):
        # Слот, накрытый меньше чем AGP_MIN_SLOT_DAYS днями, — «два завтрака»,
        # а не «обычно». Семь дней покрывают первую половину суток, и лишь
        # четыре из них дотягиваются до часа после полудня: профиль живёт, а
        # недобравший слот гаснет.
        readings = []
        for day in range(AGP_MIN_DAYS, 0, -1):
            readings += full_day(BASE - timedelta(days=day), hours=12)
        for day in range(AGP_MIN_SLOT_DAYS - 1, 0, -1):
            midnight = BASE - timedelta(days=day)
            readings += [
                (midnight + timedelta(hours=12, minutes=5 * i), 120.0)
                for i in range(12)
            ]

        profile = day_profile(readings, BASE)

        assert profile is not None
        assert profile["slots"][48] is None
        assert profile["slots"][0] is not None
