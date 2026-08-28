"""Postprandial analysis: what the curve did after each meal.

Pure functions over readings and journal entries — no database, no network, so
the arithmetic is testable without either.

This module describes outcomes and never prescribes. It reports how far glucose
rose, when it peaked, whether it came back, and whether a low followed; it never
judges a dose. Whether an injection was "right" depends on how long before the
meal it went in, on activity, illness, and on insulin still active from an
earlier dose — none of which is in this data.
"""

import statistics
from datetime import datetime, timedelta, timezone


# Окно разбора. Короткий инсулин отрабатывает 3–5 часов, за четыре часа
# нормальная кривая успевает подняться и вернуться — а шестичасовое окно почти
# всегда захватывало бы следующий приём пищи.
WINDOW = timedelta(hours=4)

# Насколько далеко ищется опорное значение вокруг метки события. Сенсор отдаёт
# точку раз в пять минут, так что четверть часа переживает один пропуск, но не
# выдаёт за опору показание из совсем другого места кривой.
NEAREST_TOLERANCE = timedelta(minutes=15)

# Сколько показаний должно быть в окне, чтобы разбор что-то значил. Ожидается
# около 48 (4 часа по 5 минут); на половине от этого форма кривой ещё читается,
# ниже — уже додумывается.
MIN_COVERAGE = 0.5

# Клинические ориентиры, в мг/дл. Подъём меньше 50 (2,8 ммоль/л) и возврат в
# пределах 30 (1,7 ммоль/л) — то, к чему обычно стремятся; это ориентиры для
# чтения графика, а не пороги, из которых что-то следует автоматически.
TARGET_RISE = 50
TARGET_RETURN = 30

# Пик обычно приходится на 60–90 минут. Числа нужны только подписи на странице.
TYPICAL_PEAK_MIN = (60, 90)


def _seconds(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def _nearest(
    readings: list[tuple[datetime, float]],
    moment: datetime,
    tolerance: timedelta = NEAREST_TOLERANCE,
) -> float | None:
    """Показание, ближайшее к моменту времени, если оно достаточно близко."""

    best: tuple[timedelta, float] | None = None
    for timestamp, mgdl in readings:
        distance = abs(timestamp - moment)
        if distance <= tolerance and (best is None or distance < best[0]):
            best = (distance, mgdl)

    return None if best is None else best[1]


def excursion(
    meal: tuple[datetime, float],
    readings: list[tuple[datetime, float]],
    now: datetime,
    other_meals: list[datetime],
    hypo_mgdl: int,
) -> dict | None:
    """Разобрать один приём пищи. ``None``, если данных под ним нет.

    Возвращает описание того, что произошло: опора, пик, время до пика,
    возврат к исходному и была ли гипогликемия. Вывода о дозе здесь нет и быть
    не может — см. модуль.
    """

    started, carbs = meal
    finished = started + WINDOW

    baseline = _nearest(readings, started)
    if baseline is None:
        # Без опоры подъём не от чего считать: сенсор в этот момент молчал.
        return None

    window = [item for item in readings if started <= item[0] <= finished]
    if not window:
        return None

    expected = WINDOW.total_seconds() / 300
    complete = finished <= now and len(window) >= expected * MIN_COVERAGE

    peak_at, peak = max(window, key=lambda item: item[1])
    ending = _nearest(readings, finished)

    return {
        "t": _seconds(started),
        "carbs": round(carbs, 1),
        "baseline": round(baseline),
        "peak": round(peak),
        "peak_min": int((peak_at - started).total_seconds() // 60),
        "rise": round(peak - baseline),
        # None, если окно ещё не закрылось или сенсор молчал в его конце:
        # «вернулось к исходному» — утверждение, которое нужно подтвердить
        # показанием, а не отсутствием такового.
        "ret": None if ending is None else round(ending - baseline),
        "hypo": any(mgdl < hypo_mgdl for _, mgdl in window),
        # Второй приём пищи внутри окна делает разбор бессмысленным: подъём
        # принадлежит уже двум событиям сразу.
        "overlap": any(started < other <= finished for other in other_meals),
        "complete": complete,
        "curve": [
            [int((timestamp - started).total_seconds() // 60), round(mgdl)]
            for timestamp, mgdl in window
        ],
    }


def summarise(excursions: list[dict], hypo_mgdl: int) -> dict | None:
    """Свести разборы в несколько чисел.

    Считается только по завершённым и не перекрытым окнам: незакрытое окно ещё
    не знает своего пика, а перекрытое приписывает одному приёму пищи подъём от
    двух.
    """

    clean = [item for item in excursions if item["complete"] and not item["overlap"]]
    if not clean:
        return None

    return {
        "count": len(clean),
        # Медиана, а не среднее: один разобранный праздничный обед иначе
        # сдвинул бы картину обычной недели.
        "rise": round(statistics.median(item["rise"] for item in clean)),
        "peak_min": round(statistics.median(item["peak_min"] for item in clean)),
        "hypo": sum(1 for item in clean if item["hypo"]),
        # «Уложился» — подъём в пределах ориентира и без гипогликемии следом.
        # Это описание исхода, а не оценка дозы.
        "good": sum(
            1 for item in clean if item["rise"] <= TARGET_RISE and not item["hypo"]
        ),
        "skipped": len(excursions) - len(clean),
    }


def analyse(
    meals: list[tuple[datetime, float]],
    readings: list[tuple[datetime, float]],
    now: datetime,
    hypo_mgdl: int,
    limit: int = 24,
) -> dict:
    """Разобрать последние приёмы пищи и свести их в итог.

    ``limit`` ограничивает число кривых, уезжающих на страницу: каждая — до
    полусотни точек, и три десятка их хватает, чтобы увидеть форму, не утроив
    вес снимка.
    """

    ordered = sorted(meals)
    moments = [moment for moment, _ in ordered]

    excursions = []
    for index, meal in enumerate(ordered):
        others = moments[:index] + moments[index + 1 :]
        item = excursion(meal, readings, now, others, hypo_mgdl)
        if item is not None:
            excursions.append(item)

    # Свежие интереснее старых: обрезаем с начала, а итог считаем по тем же
    # окнам, что показаны, — иначе число в сводке не сойдётся с картинкой.
    excursions = excursions[-limit:]

    return {
        "window_min": int(WINDOW.total_seconds() // 60),
        "targets": {
            "rise": TARGET_RISE,
            "ret": TARGET_RETURN,
            "hypo": hypo_mgdl,
            "peak_min": list(TYPICAL_PEAK_MIN),
        },
        "meals": excursions,
        "summary": summarise(excursions, hypo_mgdl),
    }
