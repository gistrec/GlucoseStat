"""Профиль обычного дня (AGP): медиана и коридор 25–75 % по времени суток.

Отвечает на вопрос, которого не задаёт ни один другой экран: сегодняшний день
обычный — или сегодня что-то не так. Раскладка по слотам считается здесь, а не
на странице: в браузер уезжают 96 прореженных слотов, а не две недели сырья.

Модуль не называется ``profile.py`` нарочно: так зовётся модуль стандартной
библиотеки, ``pytest.ini`` задаёт ``pythonpath = .``, и файл с таким именем в
корне ломал бы ``import cProfile`` — то есть ``python -m cProfile`` — во всём
проекте.
"""

from datetime import date, datetime, timedelta, timezone

from daytime import DISPLAY_TZ, _percentile, _zone

# Своё окно, а не GMI_WINDOW и не ANALYSIS_WINDOW: числа совпадают, но причины
# разные — профиль усредняет форму дня, а не оценивает HbA1c и не собирает
# выборку приёмов пищи, — и меняться они должны порознь.
AGP_WINDOW = timedelta(days=14)

# Четверть часа: форма дня уже читается, а слотов всего 96, а не 288.
AGP_SLOT_MINUTES = 15

# Меньше недели — не «обычный день», а пересказ пары суток: коридор по трём
# дням выглядит так же уверенно, как по четырнадцати, а означает другое.
AGP_MIN_DAYS = 7

# Слот, накрытый меньшим числом дней, публикуется как null: медиана двух
# завтраков — это не «обычно», это два завтрака.
AGP_MIN_SLOT_DAYS = 5

# Если заполнено меньше половины слотов, профиль не публикуется целиком:
# коридор на четверти ширины холста неотличим от сбоя отрисовки.
AGP_MIN_SLOT_SHARE = 0.5


def day_profile(
    readings: list[tuple[datetime, float]], now: datetime
) -> dict | None:
    """Median and 25–75 % corridor per time-of-day slot, or nothing.

    Чистая функция над теми же naive-UTC показаниями, что приходят в
    ``build_snapshot``; всё в мг/дл целыми, как и остальной снимок.
    """

    window = [item for item in readings if item[0] >= now - AGP_WINDOW]
    if not window:
        return None

    zone = _zone()
    slot_count = 24 * 60 // AGP_SLOT_MINUTES

    # слот -> местный день -> замеры этого дня, попавшие в слот
    per_slot: dict[int, dict[date, list[float]]] = {}
    days_seen: set[date] = set()
    for timestamp, mgdl in window:
        local = timestamp.replace(tzinfo=timezone.utc).astimezone(zone)
        days_seen.add(local.date())
        slot = (local.hour * 60 + local.minute) // AGP_SLOT_MINUTES
        per_slot.setdefault(slot, {}).setdefault(local.date(), []).append(mgdl)

    # Окно в 14 суток пересекает до 15 календарных дат: краевые даты входят
    # кусками и вместе дают не больше четырнадцати дней данных. Подпись на
    # странице обещает дни, а не даты.
    days = min(len(days_seen), AGP_WINDOW.days)
    if days < AGP_MIN_DAYS:
        return None

    slots: list[list[int] | None] = []
    for slot in range(slot_count):
        by_day = per_slot.get(slot, {})
        # Квартили считаются по дням, а не по замерам: день, где сенсор отдал
        # в слот три точки вместо одной, не должен весить втрое.
        day_values = [sum(values) / len(values) for values in by_day.values()]
        if len(day_values) < AGP_MIN_SLOT_DAYS:
            slots.append(None)
            continue

        slots.append(
            [
                round(_percentile(day_values, 25)),
                round(_percentile(day_values, 50)),
                round(_percentile(day_values, 75)),
                len(day_values),
            ]
        )

    filled = sum(1 for slot in slots if slot is not None)
    if filled < AGP_MIN_SLOT_SHARE * slot_count:
        return None

    return {
        "tz": DISPLAY_TZ,
        "slot_min": AGP_SLOT_MINUTES,
        "days": days,
        "slots": slots,
    }
