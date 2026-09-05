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
from typing import NamedTuple


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

# Насколько близко идущие записи — один приём пищи. Тарелка и добавка через
# четверть часа порознь дают первой огрызок окна, а второй — опору посреди
# подъёма первой; полчаса взяты по той же причине, что и в ``DOSE_WINDOW``.
# Промежуток считается от начала приёма, а не от последней записи: иначе еда
# каждые двадцать минут склеилась бы в один приём длиной в день.
SAME_MEAL_GAP = timedelta(minutes=30)

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

AGREEMENT_ABSOLUTE_LOW_G = 15.0
AGREEMENT_OK_RATIO = 0.10
AGREEMENT_LOW_RATIO = 0.25

CARBS_EPSILON_G = 0.05

#: Сколько точек рисуется на странице. Три — потому что столько уровней
#: уверенности предлагает бот: «знаю состав», «прикинул», «наугад».
TRUST_DOTS = 3

#: Уверенность по умолчанию, когда человека не спросили: у весов число
#: измерено, у слова — названо навскидку. Оценка по фото сюда не попадает — там
#: уверенность считается по разбросу прогонов, см. ``_photo_dots``.
DEFAULT_DOTS = {"weighed": TRUST_DOTS, "spoken": TRUST_DOTS - 1}


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


def _merge(
    meals: list[tuple[datetime, float]],
) -> list[list[tuple[datetime, float]]]:
    """Сгруппировать записи ближе ``SAME_MEAL_GAP``: группа — один приём пищи.

    Приём начинается с первой своей записи: подъём считается от опоры до еды, а
    не от уровня, до которого она уже успела поднять.
    """

    merged: list[list[tuple[datetime, float]]] = []
    for record in meals:
        if merged and record[0] - merged[-1][0][0] <= SAME_MEAL_GAP:
            merged[-1].append(record)
        else:
            merged.append([record])
    return merged


class Trust(NamedTuple):
    """Две независимые вещи об одном числе углеводов.

    ``origin`` — чем оно получено: ``weighed`` весами, ``spoken`` со слов
    человека, ``photo`` прогонами модели по снимку. ``dots`` — сколько в нём
    веры, от 1 до ``TRUST_DOTS``.

    Раздельно, потому что это разные вопросы, и раньше их слитность врала в обе
    стороны: взвешенной порции нельзя было сказать «доел половину, считал на
    глаз», а названной со слов — «состав написан на упаковке». Любое из полей
    бывает пустым: у записи без таблиц бота неизвестен способ, у старой записи —
    уверенность.
    """

    origin: str | None = None
    dots: int | None = None


def _photo_dots(median: float | None, spread: float | None) -> int | None:
    """Уверенность оценки по фото — из того, насколько сошлись прогоны.

    Пороги согласия — те же, что в ``carbs/aggregate.py`` у бота: под оценкой он
    пишет «Согласованность: высокая/средняя/низкая», и точки на странице обязаны
    говорить о том же приёме то же самое. Меняя их там, поменять и здесь.
    """

    if median is None or spread is None:
        return None
    if spread > AGREEMENT_ABSOLUTE_LOW_G:
        return 1
    if spread == 0:
        return TRUST_DOTS
    if median <= 0:
        return 1

    ratio = spread / median
    if ratio > AGREEMENT_LOW_RATIO:
        return 1
    if ratio < AGREEMENT_OK_RATIO:
        return TRUST_DOTS
    return TRUST_DOTS - 1


def _origin(
    source: str | None,
    was_weighed: bool | None,
    median: float | None,
    confirmed: float | None,
) -> str | None:
    """Чем получено число. ``None`` — если про запись ничего не известно:
    страница рисуется и без таблиц бота, просто без значка."""

    # Весы перебивают всё: у взвешенной порции число измерено, и чем его
    # предлагала модель, к нему уже не относится.
    if was_weighed:
        return "weighed"
    if source is not None and source != "photo_estimate":
        return "spoken"
    if median is None:
        return None
    # Число, исправленное человеком на глаз, — тоже со слов: прогоны спорили о
    # своей медиане, а в журнал ушла чужая.
    if confirmed is not None and abs(confirmed - median) > CARBS_EPSILON_G:
        return "spoken"
    return "photo"


def trust_level(
    source: str | None = None,
    was_weighed: bool | None = None,
    median: float | None = None,
    spread: float | None = None,
    confirmed: float | None = None,
    confidence: int | None = None,
) -> Trust:
    """Способ и уверенность для одной записи еды.

    ``confidence`` — ответ человека боту, и он старше всякого вывода: только он
    знает, доел ли порцию и читал ли состав на упаковке. Без ответа уверенность
    выводится по-старому — у весов полная, у слова средняя, у фото по разбросу
    прогонов, — чтобы записи, сделанные до появления вопроса, выглядели как
    выглядели.
    """

    origin = _origin(source, was_weighed, median, confirmed)

    if confidence in range(1, TRUST_DOTS + 1):
        return Trust(origin, confidence)

    if origin is None:
        return Trust()
    return Trust(origin, DEFAULT_DOTS.get(origin) or _photo_dots(median, spread))


def _worst_trust(levels) -> Trust:
    """Худшее из подтверждений: приём, сложенный из записей, честен по слабейшей.

    Способ берётся у той же записи, что дала худшую уверенность: сказать
    «взвешено» о приёме, где взвешена половина, а вторая половина названа
    наугад, — значит обещать точность, которой нет.
    """

    known = [level for level in levels if level is not None and level.dots is not None]
    if not known:
        # Способ без уверенности всё же стоит показать: значок есть, точек нет.
        origins = [level for level in levels if level is not None and level.origin]
        return origins[0] if origins else Trust()

    return min(known, key=lambda level: level.dots)


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
    parts: list[tuple[datetime, float]] | None = None,
    trust: Trust | None = None,
) -> dict | None:
    """Разобрать один приём пищи. ``None``, если данных под ним нет.

    Возвращает описание того, что произошло: опора, пик, время до пика,
    возврат к исходному и была ли гипогликемия. Окно, в которое попала
    следующая еда, обрезается по её моменту; ``cut`` ставится, если подъём к
    этому моменту вышел за ориентир. ``dose`` — болюс этой еды из ``_doses``;
    он показывается рядом, но вывода о нём здесь нет и быть не может — см.
    модуль. ``parts`` — записи, из которых сложился приём, если их было
    несколько. ``trust`` — способ и уверенность числа углеводов (``Trust``).
    """

    started, carbs = meal
    finished = started + WINDOW

    # Следующая еда закрывает окно досрочно: точки после неё принадлежат двум
    # событиям сразу, и оставить их — приписать этому приёму чужой подъём.
    # Перекусы в ``other_meals`` не попадают — их отсеивает ``analyse``.
    cutoff = min([finished, *(other for other in other_meals if started < other <= finished)])
    truncated = cutoff < finished

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
    ending = None if truncated else _nearest(readings, finished)

    rise = round(peak - baseline)
    # «Прервано» — про подъём, оставшийся за ориентиром: чем он кончился, никто
    # не видел. Уложившийся в ориентир обрезка не отменяет.
    cut = truncated and rise > TARGET_RISE

    return {
        "t": _seconds(started),
        "carbs": round(carbs, 1),
        # None у приёма из одной записи: страница объясняет звёздочкой только
        # то число, которое сложено, а на дорожке графика стоит порознь.
        "parts": None
        if parts is None
        else [[_seconds(moment), round(grams, 1)] for moment, grams in parts],
        # Пустой Trust уезжает как None: способ неизвестен и уверенность тоже,
        # а объект с двумя пустыми полями страница нарисовала бы как значок.
        "trust": None if trust is None or trust == Trust() else trust._asdict(),
        "dose": dose,
        "baseline": round(baseline),
        "peak": round(peak),
        "peak_min": int((peak_at - started).total_seconds() // 60),
        "rise": rise,
        # None, если окно ещё не закрылось или сенсор молчал в его конце:
        # «вернулось к исходному» — утверждение, которое нужно подтвердить
        # показанием, а не отсутствием такового.
        "ret": None if ending is None else round(ending - baseline),
        "hypo": any(mgdl < hypo_mgdl for at, mgdl in window if at > peak_at),
        # Гипогликемия до пика — та, с которой в еду вошли: дальше сахар с неё
        # только поднимался, и этот приём её и закрыл. После пика — та, к
        # которой еда с дозой привели. Раньше они считались вместе, и ночь,
        # начатая с 3,4 и поднятая до 9,8, попадала в «Исход» как гипогликемия,
        # хотя была её лечением.
        #
        # Граница — пик, а не первые четверть часа: любой срок пришлось бы
        # выбирать наугад, а затянувшаяся гипогликемия проваливалась бы в
        # «последствие» ровно оттого, что поднималась медленно.
        "from_hypo": any(mgdl < hypo_mgdl for at, mgdl in window if at <= peak_at),
        "cut": cut,
        "complete": complete,
        "curve": [
            [int((timestamp - started).total_seconds() // 60), round(mgdl)]
            for timestamp, mgdl in window
        ],
    }


def summarise(excursions: list[dict], hypo_mgdl: int) -> dict | None:
    """Свести разборы в несколько чисел.

    Медианы считаются только по завершённым и не прерванным окнам: незакрытое
    окно ещё не знает своего пика, а прерванное оборвалось на подъёме выше
    ориентира — чем он кончился, не видел никто. Гипогликемии — по всем
    показанным окнам: это факт безопасности, а не качество кривой, и он не
    вправе пропасть со страницы вместе с медианами, когда чистых окон не
    осталось, — потому сводка есть всегда, а медианы в ней бывают ``None``.
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
        # Съеденное на гипогликемии считается отдельно и рядом: без него
        # выходит, что ночных эпизодов не было вовсе, — а они были, просто
        # закрыты этой едой, и на странице им место в своей строке.
        "from_hypo": sum(1 for item in excursions if item.get("from_hypo")),
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
    origins: dict[datetime, list[dict]] | None = None,
) -> dict:
    """Разобрать последние приёмы пищи и свести их в итог.

    Перекусы не разбираются — см. SNACK_CARBS, а записи, идущие подряд, сперва
    сливаются в один приём — см. SAME_MEAL_GAP. ``boluses`` — уколы короткого
    инсулина; каждый привязывается к ближайшей разбираемой еде в пределах
    ``DOSE_WINDOW``. ``limit`` ограничивает число кривых, уезжающих на
    страницу: каждая — до полусотни точек, и три десятка их хватает, чтобы
    увидеть форму, не утроив вес снимка. Страница обязана показывать реальный
    охват по датам, а не обещать «две недели»: при регулярном журнале лимит
    срабатывает раньше двухнедельного окна.

    ``origins`` — сырьё для ``trust_level`` по метке записи; списком на метку,
    потому что две записи еды могут стоять на одной секунде.
    """

    # Перекусы (до SNACK_CARBS граммов) не участвуют вовсе: не режут чужие
    # окна и не получают своего разбора — см. SNACK_CARBS. На главном графике
    # они остаются столбиками, как и были. Себя приём не режет:
    # ``started < other`` строгое.
    sittings = _merge(sorted(meal for meal in meals if meal[1] > SNACK_CARBS))
    reviewed = [
        (records[0][0], sum(grams for _, grams in records)) for records in sittings
    ]
    significant = [moment for moment, _ in reviewed]
    doses = _doses(significant, boluses or [])

    trust = {
        moment: _worst_trust(trust_level(**origin) for origin in origins_at)
        for moment, origins_at in (origins or {}).items()
    }

    excursions = []
    for meal, records in zip(reviewed, sittings):
        item = excursion(
            meal,
            readings,
            now,
            significant,
            hypo_mgdl,
            dose=doses.get(meal[0]),
            parts=records if len(records) > 1 else None,
            trust=_worst_trust(trust.get(moment) for moment, _ in records),
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
