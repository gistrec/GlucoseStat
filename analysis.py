"""Postprandial analysis: what the curve did after each meal.

Pure functions over readings and journal entries — no database, no network, so
the arithmetic is testable without either.

This module describes outcomes and never prescribes. It reports how far glucose
rose, when it peaked, whether it came back, and whether a low followed; it shows
the meal's bolus and how far ahead it went in, but it never judges a dose.
Whether an injection was "right" also depends on activity, illness, and on
insulin still active from an earlier dose — none of which is in this data.
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

# Как далеко от отметки еды искать её болюс. Укол «к еде» бывает и заранее
# (упреждение), и после первых ложек — полчаса в обе стороны накрывают оба
# случая, не подбирая коррекцию, сделанную по совсем другому поводу.
DOSE_WINDOW = timedelta(minutes=30)

# Граница перекуса, в граммах углеводов. Еда не больше этого — долька шоколада,
# пара крекеров — почти не заметна на кривой, и обрывать из-за неё чужое окно
# значит терять разбор целого обеда ради события, след которого тонет в шуме
# сенсора. Своего разбора перекус не получает по той же причине: его «окно»
# перемеряло бы кривую соседней еды с опоры посреди её подъёма — двойной учёт,
# от которого защищает обрезка. Всё, что крупнее, — полноценный приём пищи:
# режет окна соседей и разбирается сам.
SNACK_CARBS = 10

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


def _doses(
    moments: list[datetime],
    boluses: list[tuple[datetime, float]],
) -> dict[datetime, dict]:
    """Привязать болюсы к ближайшей еде в пределах ``DOSE_WINDOW``.

    Каждый укол достаётся ровно одной еде — ближайшей: доза между обедом и
    перекусом не должна красоваться в двух строках сразу. Разбитая доза
    суммируется, а упреждение считается по первому уколу: именно он решает,
    успел ли инсулин к пику.
    """

    attached: dict[datetime, list[tuple[datetime, float]]] = {}
    for occurred_at, units in boluses:
        nearest = min(
            (moment for moment in moments if abs(moment - occurred_at) <= DOSE_WINDOW),
            key=lambda moment: abs(moment - occurred_at),
            default=None,
        )
        if nearest is not None:
            attached.setdefault(nearest, []).append((occurred_at, units))

    doses = {}
    for moment, shots in attached.items():
        first = min(occurred_at for occurred_at, _ in shots)
        doses[moment] = {
            "units": round(sum(units for _, units in shots), 1),
            # Положительное упреждение — укол до еды, отрицательное — после.
            "lead_min": round((moment - first).total_seconds() / 60),
        }
    return doses


def excursion(
    meal: tuple[datetime, float],
    readings: list[tuple[datetime, float]],
    now: datetime,
    other_meals: list[datetime],
    hypo_mgdl: int,
    dose: dict | None = None,
) -> dict | None:
    """Разобрать один приём пищи. ``None``, если данных под ним нет.

    Возвращает описание того, что произошло: опора, пик, время до пика,
    возврат к исходному и была ли гипогликемия. Окно, в которое попала
    следующая еда, обрезается по её моменту и помечается ``cut``. ``dose`` —
    болюс этой еды из ``_doses``; он показывается рядом, но вывода о нём здесь
    нет и быть не может — см. модуль.
    """

    started, carbs = meal
    finished = started + WINDOW

    # Следующая еда закрывает окно досрочно: точки после неё принадлежат двум
    # событиям сразу, и оставить их — приписать этому приёму чужой подъём.
    # Перекусы в ``other_meals`` не попадают — их отсеивает ``analyse``.
    cutoff = min([finished, *(other for other in other_meals if started < other <= finished)])
    cut = cutoff < finished

    baseline = _nearest(readings, started)
    if baseline is None:
        # Без опоры подъём не от чего считать: сенсор в этот момент молчал.
        return None

    window = [item for item in readings if started <= item[0] <= cutoff]
    if not window:
        return None

    expected = (cutoff - started).total_seconds() / 300
    complete = cutoff <= now and len(window) >= expected * MIN_COVERAGE

    peak_at, peak = max(window, key=lambda item: item[1])
    # У обрезанного окна возврата нет по построению: уровень в момент следующей
    # еды — это её опора, а не возврат к исходному после этой.
    ending = None if cut else _nearest(readings, finished)

    return {
        "t": _seconds(started),
        "carbs": round(carbs, 1),
        "dose": dose,
        "baseline": round(baseline),
        "peak": round(peak),
        "peak_min": int((peak_at - started).total_seconds() // 60),
        "rise": round(peak - baseline),
        # None, если окно ещё не закрылось или сенсор молчал в его конце:
        # «вернулось к исходному» — утверждение, которое нужно подтвердить
        # показанием, а не отсутствием такового.
        "ret": None if ending is None else round(ending - baseline),
        "hypo": any(mgdl < hypo_mgdl for _, mgdl in window),
        "cut": cut,
        "complete": complete,
        "curve": [
            [int((timestamp - started).total_seconds() // 60), round(mgdl)]
            for timestamp, mgdl in window
        ],
    }


def summarise(excursions: list[dict], hypo_mgdl: int) -> dict | None:
    """Свести разборы в несколько чисел.

    Медианы считаются только по завершённым и не обрезанным окнам: незакрытое
    окно ещё не знает своего пика, а обрезанное не досмотрело кривую до конца —
    его подъём мог не успеть случиться. Гипогликемии — по всем показанным
    окнам: это факт безопасности, а не качество кривой, и он не вправе пропасть
    со страницы вместе с медианами, когда чистых окон не осталось, — потому
    сводка есть всегда, а медианы в ней бывают ``None``.
    """

    if not excursions:
        return None

    clean = [item for item in excursions if item["complete"] and not item["cut"]]

    return {
        "count": len(clean),
        "total": len(excursions),
        # Медиана, а не среднее: один разобранный праздничный обед иначе
        # сдвинул бы картину обычной недели. None, когда мерить не по чему.
        "rise": round(statistics.median(item["rise"] for item in clean))
        if clean
        else None,
        "peak_min": round(statistics.median(item["peak_min"] for item in clean))
        if clean
        else None,
        # По всем окнам, включая прерванные: реакция на низкий сахар — еда, а
        # еда обрезает окно, и счёт по одним чистым окнам терял бы ровно те
        # гипогликемии, которые случились и были купированы.
        "hypo": sum(1 for item in excursions if item["hypo"]),
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
    boluses: list[tuple[datetime, float]] | None = None,
    limit: int = 24,
) -> dict:
    """Разобрать последние приёмы пищи и свести их в итог.

    Перекусы не разбираются — см. SNACK_CARBS. ``boluses`` — уколы короткого
    инсулина; каждый привязывается к ближайшей разбираемой еде в пределах
    ``DOSE_WINDOW``. ``limit`` ограничивает число кривых, уезжающих на
    страницу: каждая — до полусотни точек, и три десятка их хватает, чтобы
    увидеть форму, не утроив вес снимка. Страница обязана показывать реальный
    охват по датам, а не обещать «две недели»: при регулярном журнале лимит
    срабатывает раньше двухнедельного окна.
    """

    # Перекусы (до SNACK_CARBS граммов) не участвуют вовсе: не режут чужие
    # окна и не получают своего разбора — см. SNACK_CARBS. На главном графике
    # они остаются столбиками, как и были. Себя приём не режет:
    # ``started < other`` строгое.
    reviewed = sorted(meal for meal in meals if meal[1] > SNACK_CARBS)
    significant = [moment for moment, _ in reviewed]
    doses = _doses(significant, boluses or [])

    excursions = []
    for meal in reviewed:
        item = excursion(
            meal, readings, now, significant, hypo_mgdl, dose=doses.get(meal[0])
        )
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
