"""Ночь отдельно: гипогликемии, минимум и дрейф натощак.

Ночь — единственная часть суток, которую человек не видит. Днём низкий сахар
замечают по себе; в три часа его замечает только сенсор, и то если кто-нибудь
потом посмотрит. Все прочие экраны страницы ночь прячут: недельная ломаная
прорежена пятнадцатиминутными корзинами, и провал в 3,6 ммоль показывается на
ней как 4,3 — усреднение ровно там, где важен не средний уровень, а минимум.
Поэтому ночной минимум и счёт гипогликемий считаются здесь по сырым замерам,
а не по тому, что нарисовано.

Два знаменателя, и это главное решение модуля. Гипогликемии и минимум считаются
по всем ночам, которые покрыты сенсором: это измеренные факты безопасности, и
фильтровать их нельзя — самая опасная ночь обычно и есть та, где был поздний
ужин. Дрейф — величина про базал, и он считается только по ночам «натощак», где
ужин успел отработать: иначе разность описывает хвост болюса, а читается как
свойство ночи. Страница обязана назвать оба знаменателя вслух.
"""

from datetime import date, datetime, time, timedelta, timezone

from daytime import DISPLAY_TZ, _covered, _percentile, _zone

# Своё окно, не AGP_WINDOW и не ANALYSIS_WINDOW: ночей всего по одной в сутки,
# и две недели здесь — это четырнадцать наблюдений, а не четырнадцать дней
# кривой. Неделя — то, что человек ещё помнит и с чем может связать перемену
# дозы; за месяц ночи с разным базалом смешались бы в одну медиану.
NIGHTS_WINDOW = timedelta(days=7)

# Границы ночи по местным часам. Шесть утра, а не восемь: позже начинается
# подъём, который у большинства выглядит как рост сам по себе, и минимум ночи
# смешался бы с утренним завтраком.
NIGHT_FROM_HOUR = 0
NIGHT_TO_HOUR = 6

# С какого часа меряется дрейф. К трём ночи болюс на ужин отработал — быстрые
# аналоги живут около пяти часов, — и разность 03:00 → 06:00 говорит о базале.
# От полуночи она говорила бы о другом: на живой неделе медиана 00:00 → 06:00
# даёт +0,3 (ночь «тянет вверх»), а 03:00 → 06:00 на тех же ночах — −0,7,
# потому что первый вариант меряет падение после позднего ужина.
DRIFT_FROM_HOUR = 3

# Сколько должно пройти от последней еды или короткого до начала дрейфа, чтобы
# ночь считалась натощак. Четыре часа — остаток действия быстрого аналога:
# раньше этого срока разность меряет инсулин, а не ночь.
FASTING_GAP = timedelta(hours=4)

# Ночь с покрытием ниже этой доли не участвует ни в счёте, ни в медианах:
# «гипогликемии не было» по двум часам из шести — не факт, а его видимость.
# Порог тот же, что у неполного дня в подневной сводке (DAY_MIN_COVERAGE).
NIGHT_MIN_COVERAGE = 0.5

# Кворум для медиан — как RATIO_MIN_MEALS у углеводного коэффициента: три
# наблюдения это мало, но знаменатель назван рядом, и читатель видит, из чего
# сложено число. Меньше трёх не показывается вовсе. Порог один, а применяется
# к своему знаменателю: у минимума это все зачтённые ночи, у дрейфа — чистые.
NIGHTS_MIN_MEDIAN = 3

# Шаг корзины для ночной линии. Значение корзины — минимум, а не среднее: вся
# фича про провал, и прореживание не вправе его сгладить. Ровно на этом
# спотыкается недельная ломаная, где корзины усредняются.
SPARK_SLOT_MINUTES = 15

# Сколько времени перед отметкой берётся за её уровень. Четверть часа: сенсор
# отдаёт точку не по расписанию, а требовать замер секунда в секунду значило бы
# терять дрейф из-за пропущенного опроса.
DRIFT_TOLERANCE = timedelta(minutes=15)


def _local_moment(day: date, hour: int, zone) -> datetime:
    """Местный час этих суток как naive-UTC — той же формы, что показания.

    Через ``combine``, а не прибавлением часов к полуночи: сутки перехода на
    летнее время длятся 23 часа, и «три ночи» в них не равно «полночь плюс
    три».
    """

    return (
        datetime.combine(day, time(hour=hour), tzinfo=zone)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def _level_at(
    readings: list[tuple[datetime, float]], moment: datetime
) -> float | None:
    """Уровень к отметке: медиана последней четверти часа до неё.

    Медиана окна, а не ближайшая точка: у сенсора шум порядка 0,2–0,3 ммоль,
    и разность двух одиночных замеров наполовину состояла бы из него.

    Окно до отметки, а не вокруг неё: симметричное залезало бы за 03:00 и
    смешивало уровень «к трём» с тем, что случилось после, — то есть с частью
    самого дрейфа, который мы этой разностью и меряем.
    """

    window = [
        mgdl
        for moment_at, mgdl in readings
        if moment - DRIFT_TOLERANCE <= moment_at < moment
    ]
    return _percentile(window, 50) if window else None


def _spark(readings: list[tuple[datetime, float]]) -> list[list[int]]:
    """Ночная линия корзинами по ``SPARK_SLOT_MINUTES`` — по минимуму корзины.

    Минимум, а не среднее: линия рисуется рядом с числом «минимум за ночь», и
    нижняя точка линии обязана быть тем же самым замером. Усреднённая корзина
    показывала бы провал мельче, чем он был, — то есть ровно ту ошибку, ради
    которой ночь и вынесена отдельно.
    """

    step = SPARK_SLOT_MINUTES * 60
    buckets: dict[int, float] = {}
    for moment, mgdl in readings:
        bucket = int(moment.replace(tzinfo=timezone.utc).timestamp()) // step * step
        buckets[bucket] = min(buckets.get(bucket, mgdl), mgdl)

    return [[bucket, round(mgdl)] for bucket, mgdl in sorted(buckets.items())]


def _fasting(
    journal: list[tuple[datetime, str, float | None, float | None]],
    evening: datetime,
    drift_start: datetime,
    night_end: datetime,
) -> bool:
    """Успел ли ужин отработать к началу дрейфа — по записям журнала.

    Три условия, и все три обязательны:

    * последняя еда или короткий раньше ``drift_start`` не менее чем на
      ``FASTING_GAP``;
    * внутри окна дрейфа не ели и не кололись — ночной перекус ломает разность
      так же, как поздний ужин;
    * вечер вообще описан журналом. Пустой вечер — это не «не ел», а «не
      записал», и принять его за голодный значило бы объявить ночь чистой ровно
      там, где о ней ничего не известно.
    """

    events = [
        moment
        for moment, kind, _, _ in journal
        if kind in ("meal", "bolus") and evening <= moment < night_end
    ]
    if not events:
        return False
    if any(moment >= drift_start for moment in events):
        return False
    return drift_start - max(events) >= FASTING_GAP


def night_summary(
    readings: list[tuple[datetime, float]],
    journal: list[tuple[datetime, str, float | None, float | None]],
    now: datetime,
    hypo_mgdl: int,
) -> dict | None:
    """Ночи окна: каждая со своим минимумом, и две сводки поверх них.

    Чистая функция над теми же naive-UTC показаниями, что приходят в
    ``build_snapshot``; всё в мг/дл целыми, как и остальной снимок.

    Массив ночей плотный: ночь без показаний тоже в нём, с ``count: 0``.
    Пропущенная молча, она сдвинула бы соседние даты друг к другу, и неделя
    с тремя ночами выглядела бы как неделя из трёх ночей.
    """

    zone = _zone()
    window_start = now - NIGHTS_WINDOW

    nights: list[dict] = []
    # Со вчерашней даты окна: ночь принадлежит дате своего утра, и окно в семь
    # суток пересекает восемь дат — первая входит хвостом.
    day = window_start.replace(tzinfo=timezone.utc).astimezone(zone).date()
    last_day = now.replace(tzinfo=timezone.utc).astimezone(zone).date()

    while day <= last_day:
        start = _local_moment(day, NIGHT_FROM_HOUR, zone)
        end = _local_moment(day, NIGHT_TO_HOUR, zone)
        drift_start = _local_moment(day, DRIFT_FROM_HOUR, zone)
        # Вечер, который эту ночь кормил: от полудня прошлых суток. Не от
        # полуночи — ужин случается до неё, и именно он решает, чистая ли ночь.
        evening = _local_moment(day - timedelta(days=1), 12, zone)

        observed_start = max(start, window_start)
        observed_end = min(end, now)
        if observed_end <= observed_start:
            day += timedelta(days=1)
            continue

        night = [
            item for item in readings if observed_start <= item[0] < observed_end
        ]
        span = (observed_end - observed_start).total_seconds()
        coverage = _covered(night) / span if night and span > 0 else 0.0

        record = {
            "date": day.isoformat(),
            "start": int(observed_start.replace(tzinfo=timezone.utc).timestamp()),
            "end": int(observed_end.replace(tzinfo=timezone.utc).timestamp()),
            "count": len(night),
            # Потолок: вес хвостовой точки может дать волосок сверх ста, а
            # «101 %» читается как баг, а не как полнота. Та же оговорка, что
            # у подневной сводки.
            "coverage": min(100, round(100 * coverage)),
            # Ночь, обрезанная краем окна или «сейчас», не участвует в сводках
            # наравне с целой: половина ночи без гипогликемии не то же самое,
            # что ночь без неё.
            "partial": observed_start > start or observed_end < end,
        }

        if night:
            values = [mgdl for _, mgdl in night]
            record["min"] = round(min(values))
            record["hypo"] = min(values) < hypo_mgdl
            record["points"] = _spark(night)

            fasting = _fasting(journal, evening, drift_start, end)
            record["fasting"] = fasting
            record["drift"] = None
            # У обрезанной ночи дрейфа нет по определению: разность до шести
            # утра, которых ещё не было, — это не маленький дрейф, а никакой.
            if fasting and not record["partial"]:
                from_value = _level_at(night, drift_start)
                to_value = _level_at(night, end)
                if from_value is not None and to_value is not None:
                    record["drift"] = round(to_value - from_value)

        nights.append(record)
        day += timedelta(days=1)

    # Сводки считаются по целым ночам достаточного покрытия — тем самым, про
    # которые у страницы есть право что-то утверждать.
    counted = [
        item
        for item in nights
        if item["count"] and not item["partial"]
        and item["coverage"] >= 100 * NIGHT_MIN_COVERAGE
    ]
    if not counted:
        return None

    clean = [item for item in counted if item["fasting"] and item["drift"] is not None]

    return {
        "tz": DISPLAY_TZ,
        "from": NIGHT_FROM_HOUR,
        "to": NIGHT_TO_HOUR,
        "drift_from": DRIFT_FROM_HOUR,
        "hypo_mgdl": hypo_mgdl,
        # Знаменатель счёта гипогликемий: ночей, за которые сенсор отвечает.
        "counted": len(counted),
        "hypo_nights": sum(1 for item in counted if item["hypo"]),
        # Знаменатель медиан: ночей, где ужин успел отработать.
        "clean": len(clean),
        "min_median": (
            round(_percentile([item["min"] for item in counted], 50))
            if len(counted) >= NIGHTS_MIN_MEDIAN
            else None
        ),
        "drift_median": (
            round(_percentile([item["drift"] for item in clean], 50))
            if len(clean) >= NIGHTS_MIN_MEDIAN
            else None
        ),
        "nights": nights,
    }
