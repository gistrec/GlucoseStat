"""Render the public JSON snapshot the dashboard page reads.

Everything the page needs lives in one file: nginx serves it as plain static
content, so a page view never touches MySQL and never reaches Abbott. The
snapshot deliberately carries nothing but timestamps and glucose values —
the LibreLinkUp payload also contains the patient's name, date of birth and
sensor serial, and none of that belongs on a public URL.
"""

import json
import os
import tempfile
from bisect import bisect_left
from datetime import date, datetime, time, timedelta, timezone
from itertools import pairwise
from statistics import median

from active import active_now
from agp import day_profile
from analysis import analyse
from database.queries import (
    fingersticks_since,
    journal_since,
    last_readings,
    latest_forecast,
    meal_origins_since,
    read_last_success,
    read_sensor_end,
    read_sensor_start,
    readings_since,
    sensor_starts_since,
    timezone_history,
)
from daytime import DISPLAY_TZ, _covered, _weigh, _weighted_percentile, _zone
from librelinkup import SENSOR_LIFETIME_FALLBACK_DAYS
from lows import high_episodes, low_episodes
from nights import night_summary


# Читается при загрузке модуля, до всякого .env, — потому и задавать его нужно
# в окружении: так его видят все трое (сборщик, превью, ручной пересбор), а не
# один из них. Через ``or``, а не значением по умолчанию: пустую переменную оно
# принимало за путь, и запись уходила в никуда — временный файл появлялся в
# каталоге над рабочим, а переименование в "" падало.
PUBLISH_PATH = os.getenv("PUBLISH_PATH") or os.path.join(
    os.path.dirname(__file__), "web", "data.json"
)

# Стандартный целевой диапазон для CGM (ADA/ATTD consensus): 70–180 mg/dL,
# те же 3.9–10.0 mmol/L, что показывает сам Libre.
TARGET_LOW_MGDL = 70
TARGET_HIGH_MGDL = 180

# Внутренняя граница диапазона — верх нормы натощак (7.2 mmol/L). До неё сахар
# «зелёный» в любое время; между ней и верхним порогом — «жёлтый», допустимый
# только после еды; выше 180 — «красный» независимо от еды. Статистика времени
# в диапазоне по-прежнему считается по 70–180: три зоны — про чтение графика,
# а не про новую метрику.
TARGET_MID_MGDL = 130

# Окно и шаг прореживания на период. Сырьё идёт с шагом 5 минут, но 30 дней
# в таком виде — это 8600 точек: график столько не покажет, а вес страницы
# вырастет на порядок. Статистика при этом всегда считается по сырым данным.
# У месяца шага нет: он не прореживается, а сводится по дням — даже прореженная
# ломаная на месячном окне читается как пила, а не как течение сахара.
# Шаги суток и двух суток дают одинаковые 288 точек: окно «48 часов» — та же
# суточная панель, растянутая на «вчера», и весить вдвое больше ей не с чего.
# Две недели — ровно один сенсор — ещё кривая, с шагом в полчаса: те же ~670
# точек, что у недели. 60 и 90 дней сводятся по дням, как месяц; 90 — окно
# HbA1c и отчёта AGP.
RANGES = {
    "day": (timedelta(days=1), 5),
    "two_days": (timedelta(days=2), 10),
    "week": (timedelta(days=7), 15),
    "two_weeks": (timedelta(days=14), 30),
    "month": (timedelta(days=30), None),
    "two_months": (timedelta(days=60), None),
    "quarter": (timedelta(days=90), None),
}

# С каким окном сравнивается предыдущий период. Почасовые — нет (шум), а 60 и
# 90 дней — нет из-за цены: сравнение удваивает выборку, и квартал потянул бы
# полгода минутных показаний на каждую публикацию раз в минуту.
COMPARED_RANGES = {"week", "two_weeks", "month"}

# Окно для оценки тренда. Libre рисует стрелку по последним ~15 минутам;
# на более коротком окне шум сенсора выдаёт скачки, которых нет.
TREND_WINDOW = timedelta(minutes=15)

# GMI считается по своему окну, а не по выбранному на странице: иначе под одним
# названием живут два разных числа — «GMI за неделю» и «GMI за месяц», — и ни
# одно из них не то, что понимает под GMI врач. Bergenstal et al. (2018)
# калибровали формулу на 14 днях при покрытии не ниже 70 %; ниже этого порога
# число не показывается вовсе, потому что оценка HbA1c по трём дням — это не
# осторожная оценка, а выдумка.
GMI_WINDOW = timedelta(days=14)
GMI_MIN_COVERAGE = 0.7

# Ниже этой доли ожидаемых замеров день в подневной сводке помечается неполным:
# медиана по горстке точек выглядит как медиана по суткам, а означает другое.
DAY_MIN_COVERAGE = 0.5

# Сравнение с предыдущим периодом. Порог покрытия свой, а не GMI_MIN_COVERAGE:
# числа совпадают, но связывать два независимых решения одним нельзя — GMI
# калиброван на своём пороге, а здесь порог отвечает лишь на вопрос «есть ли
# с чем сравнивать».
COMPARE_MIN_COVERAGE = 0.7

# Пороги значимости. Среднее: 5 мг/дл ≈ 0,3 ммоль/л — меньше MARD сенсора,
# то есть заведомый шум. Доля в диапазоне: пять процентных пунктов — примерно
# час в сутки.
COMPARE_MIN_AVG_MGDL = 5
COMPARE_MIN_TIR_PP = 5

# События рисуются только на почасовых панелях — 24 и 48 часов: сотня отметок
# на месячном окне сливается в сплошную полосу, из которой ничего не прочитать.
# Двое суток, а не одни: окно «48 часов» существует ради «а что было ровно
# сутки назад», и события обязаны покрывать оба дня. Суточная панель режет из
# этого запаса свой кусок сама.
EVENT_WINDOW = timedelta(days=2)

#: Виды записи «начата новая ручка» и какой инсулин в ней. Значения — те же
#: строки, что пишет бот в ``journal_entries.kind``; на странице по второму
#: слову метка красится как короткий или как длинный.
PEN_KINDS = {"pen_bolus": "bolus", "pen_basal": "basal"}

# А разбор приёмов пищи собирается за две недели: на суточном окне выборки
# слишком мало, чтобы медиана подъёма что-то значила.
ANALYSIS_WINDOW = timedelta(days=14)

# Эпизоды вне нормы считаются за неделю — самое широкое окно, на котором
# рисуется кривая: у месяца своя форма, подневные коробки, и там доля времени
# вне нормы уже стоит в каждом дне. Окно одно на оба порога: полосы рисуются
# на одной панели, и разная глубина памяти у верхней и нижней значила бы, что
# на левом краю кривой подъёмы ещё помечены, а провалы уже нет.
EPISODE_WINDOW = timedelta(days=7)

# А молчание сенсора — за двое суток: полосу «нет сигнала» рисуют только
# почасовые панели, и на недельной от часового пропуска остаётся три пикселя,
# в которых нечего подписывать.
GAP_WINDOW = timedelta(days=2)

# Полнота данных считается за неделю — ровно столько живёт половина сенсора,
# и за этот срок видно, пропадает он системно или один раз не выгрузился.
# Окно не то же, что у полос на графике (GAP_WINDOW): там вопрос «почему тут
# дырка в кривой», здесь — «можно ли верить статистике за неделю».
SENSOR_WINDOW = timedelta(days=7)

# Разрыв, после которого соединять замеры линией уже нельзя. Libre отдаёт точку
# раз в пять минут: один пропуск — обычная задержка выгрузки, три подряд
# означают, что сенсора на месте не было. Тем же порогом страница рвёт кривую
# (GAP_STEPS в app.js), и разъехаться им нельзя: полоса объясняет ровно те
# разрывы, которые на графике видны.
SENSOR_SILENCE = timedelta(minutes=15)

# Порог пометки «возможный шум сенсора»: точка, в которую кривая прыгнула и
# из которой сразу прыгнула обратно, оба раза не меньше чем на 0,4 ммоль/л
# (7,2 мг/дл) и не дольше 2 минут на шаг. Зигзаг, а не скорость: прежнее
# правило «скачок от 0,6 за 2 минуты» ловило в основном быстрый, но ровный
# подъём после еды — 41 пометка из 45 за неделю 02–09.10 — и ставило кольцо
# на соседку иглы, а не на неё. Ровная кривая обратно не прыгает, шум и
# компрессия — прыгают. Считается по сырым замерам: усреднение в корзину
# ``_downsample`` смазывает как раз ту иглу, которую нужно поймать.
ARTIFACT_DELTA_MGDL = 7.2
ARTIFACT_MAX_SECONDS = 120

# Сверка сенсора с глюкометром. Пара — замер и ближайшая к нему точка сенсора
# не дальше пяти минут: дальше разница говорит уже о движении сахара, а не о
# сенсоре. Смещение — медиана разниц, а не среднее: одна опечатка при вводе
# (93,7 вместо 193,7) сдвинула бы среднее по десятку пар на полторы ммоль/л.
# Предупреждение — только когда смещение и велико, и устойчиво: от пяти пар,
# по модулю от 0,5 ммоль/л (9 мг/дл — на уровне MARD самого Libre, меньше
# уже не отличить от шума) и в одну сторону хотя бы у трёх пар из четырёх.
BIAS_MAX_OFFSET = timedelta(minutes=5)
BIAS_MIN_PAIRS = 5
BIAS_MIN_MGDL = 9
BIAS_MIN_SHARE = 0.75


def _downsample(
    readings: list[tuple[datetime, float]], step_minutes: int, low: int
) -> list[list[int]]:
    """Average readings into fixed buckets, as ``[unix_seconds, mg/dL]``.

    Кроме корзин, где сахар опускался ниже ``low``: такая отдаёт свой минимум,
    а не среднее. Пятнадцатиминутная корзина недельной панели усредняет провал
    до 3,6 ммоль с соседями и рисует 4,3 — то есть стирает гипогликемию ровно
    на том окне, где её ищут глазами. Ошибка при этом сдвигается в безопасную
    сторону: линия показывает провал, который действительно был, вместо
    благополучия, которого не было.

    Только вниз и только за порогом: усреднять пики незачем — высокий сахар
    держится часами и в корзину целиком, а лишний экстремум сверху сделал бы
    кривую нервной без всякой пользы.
    """

    if not readings:
        return []

    step = step_minutes * 60
    buckets: dict[int, list[float]] = {}
    for timestamp, mgdl in readings:
        bucket = int(timestamp.replace(tzinfo=timezone.utc).timestamp()) // step * step
        buckets.setdefault(bucket, []).append(mgdl)

    return [
        [
            bucket,
            round(min(values) if min(values) < low else sum(values) / len(values)),
        ]
        for bucket, values in sorted(buckets.items())
    ]


def _gaps(readings: list[tuple[datetime, float]], since: datetime) -> list[dict]:
    """Промежутки, в которые сенсор молчал, как ``[{start, end, minutes}]``.

    По сырым замерам — по той же причине, по какой по ним считаются эпизоды
    ниже нормы. Край корзины прореживания сдвигает границу на свой шаг, и одно
    и то же молчание на суточной панели называлось бы «1 ч 45 мин», а на
    двухсуточной, с её десятиминутным шагом, — «1 ч 50 мин»: два числа про один
    промежуток, расходящиеся от нажатия кнопки.

    Промежуток, который ещё длится, сюда не попадает: у него нет правого края,
    и приписать ему конец значило бы объявить сенсор заговорившим. Его
    дорисовывает страница — от ``latest`` до момента снимка.
    """

    gaps = []
    for (left, _), (right, _) in pairwise(readings):
        if right - left <= SENSOR_SILENCE or right < since:
            continue
        gaps.append(
            {
                "start": int(left.replace(tzinfo=timezone.utc).timestamp()),
                "end": int(right.replace(tzinfo=timezone.utc).timestamp()),
                "minutes": round((right - left).total_seconds() / 60),
            }
        )

    return gaps


def _bias(
    started: datetime | None,
    readings: list[tuple[datetime, float]],
    fingersticks: list[tuple[datetime, float]],
) -> dict | None:
    """Насколько текущий сенсор расходится с глюкометром, или ``None``.

    Только по замерам с момента установки: у прошлого сенсора своя ошибка, и
    смешав их, плашка винила бы новый за грехи старого. ``None`` — и когда пар
    мало, и когда расхождение в пределах шума: плашка появляется, только если
    есть что сказать (см. ``BIAS_*``).
    """

    if started is None or not readings:
        return None

    times = [moment for moment, _ in readings]
    pairs: list[tuple[float, float]] = []  # (глюкометр, сенсор − глюкометр)
    for moment, finger in fingersticks:
        if moment < started:
            continue
        index = bisect_left(times, moment)
        nearest = min(
            (i for i in (index - 1, index) if 0 <= i < len(times)),
            key=lambda i: abs(times[i] - moment),
        )
        if abs(times[nearest] - moment) <= BIAS_MAX_OFFSET:
            pairs.append((finger, readings[nearest][1] - finger))

    if len(pairs) < BIAS_MIN_PAIRS:
        return None

    offset = median(diff for _, diff in pairs)
    agreeing = [finger for finger, diff in pairs if diff * offset > 0]
    if abs(offset) < BIAS_MIN_MGDL or len(agreeing) / len(pairs) < BIAS_MIN_SHARE:
        return None

    return {
        "mgdl": round(offset, 1),
        "pairs": len(pairs),
        # Сколько пар согласны со смещением и при каком сахаре по глюкометру:
        # смещение проверено только в этом диапазоне, и плашка называет его,
        # а не обещает ту же ошибку на любом сахаре.
        "agree": len(agreeing),
        "from": round(min(agreeing), 1),
        "to": round(max(agreeing), 1),
    }


def _sensor(
    started: datetime | None,
    ends: datetime | None,
    readings: list[tuple[datetime, float]],
    now: datetime,
    fingersticks: list[tuple[datetime, float]] | None = None,
) -> dict:
    """Сенсор: когда надет, когда кончится и сколько данных от него дошло.

    Обе отметки приходят из базы, куда их кладёт сборщик: срок жизни зависит
    от модели прибора, а модель видна только тому, кто разговаривает с Abbott
    (см. ``SENSOR_MODELS``). Здесь конец срока уже посчитан, и дело страницы —
    сказать, сколько до него осталось; считает она это сама, по своим часам,
    иначе число протухало бы между перестройками снимка и к утру показывало бы
    вчерашнее.

    Отметок может не быть: у рендерера на реплике таблицы может не быть вовсе,
    а Abbott присылает блок сенсора не в каждом ответе. Тогда половина карточки
    про срок молчит, а половина про полноту данных остаётся — она считается по
    самим показаниям и ни от чего внешнего не зависит.

    Конца срока может не быть и при известном начале — так выглядит отметка,
    записанная прежним сборщиком, который срока ещё не считал. Тогда он
    достраивается запасным сроком: карточка с датой на сутки точнее, чем
    карточка без даты.

    Полнота — доля окна, покрытая показаниями. Не доля ожидаемых замеров:
    шаг выгрузки у Libre плавает между минутой и пятью, и «пришло 1823 из
    2016» говорило бы больше о частоте опроса, чем о том, были ли данные.
    """

    window_start = now - SENSOR_WINDOW
    silence = SENSOR_SILENCE.total_seconds()

    # Промежутки, в которые данных не было: между замерами, до первого замера
    # и после последнего. Последние два — такие же дыры, как середина: окно
    # недели не становится полным оттого, что сенсор молчит прямо сейчас.
    quiet: list[tuple[datetime, datetime]] = []
    for (left, _), (right, _) in pairwise(readings):
        if (right - left).total_seconds() > silence:
            quiet.append((left, right))

    if readings:
        if readings[0][0] > window_start:
            quiet.append((window_start, readings[0][0]))
        if (now - readings[-1][0]).total_seconds() > silence:
            quiet.append((readings[-1][0], now))
    else:
        quiet.append((window_start, now))

    # Каждый промежуток обрезается окном: молчание, начавшееся девять дней
    # назад, портит полноту недели ровно на ту часть, что попала в неделю.
    silent_seconds = 0.0
    count = 0
    for left, right in quiet:
        overlap = (min(right, now) - max(left, window_start)).total_seconds()
        if overlap <= silence:
            continue
        silent_seconds += overlap
        count += 1

    total = SENSOR_WINDOW.total_seconds()
    if started and not ends:
        ends = started + timedelta(days=SENSOR_LIFETIME_FALLBACK_DAYS)

    return {
        "started": int(started.replace(tzinfo=timezone.utc).timestamp())
        if started
        else None,
        "ends": int(ends.replace(tzinfo=timezone.utc).timestamp())
        if started and ends
        else None,
        # Срок — не константа страницы, а разница между двумя отметками: у
        # разных моделей он разный, и назвать его числом здесь значит однажды
        # разойтись с датой, которая стоит рядом.
        "lifetime_days": (ends - started).days if started and ends else None,
        "window_days": SENSOR_WINDOW.days,
        "coverage": round(100 * (1 - silent_seconds / total), 1),
        "quiet": count,
        "quiet_minutes": round(silent_seconds / 60),
        "bias": _bias(started, readings, fingersticks or []),
    }


def _artifacts(readings: list[tuple[datetime, float]], since: datetime) -> list[dict]:
    """Точки-иглы: кривая прыгнула в них и сразу вернулась (см. ``ARTIFACT_*``).

    Каждая запись несёт ``dv`` — скачок в точку, ``back`` — скачок из неё и
    ``dsec`` — сколько длилась игла от соседки до соседки, чтобы подсказка
    объясняла пометку числами, а не словом «шум». Публикуется сама игла: её
    глаз и видит выбившейся из кривой.

    Последняя точка не проверяется: обратного скачка у неё ещё нет, и пометка
    появится через минуту, со следующим замером. Окно — по самой игле.
    """

    artifacts = []
    for (left_t, left_v), (mid_t, mid_v), (right_t, right_v) in zip(
        readings, readings[1:], readings[2:]
    ):
        if mid_t < since:
            continue

        into = (mid_t - left_t).total_seconds()
        out = (right_t - mid_t).total_seconds()
        if not (0 < into <= ARTIFACT_MAX_SECONDS and 0 < out <= ARTIFACT_MAX_SECONDS):
            continue

        jump, back = mid_v - left_v, right_v - mid_v
        if jump * back >= 0 or min(abs(jump), abs(back)) < ARTIFACT_DELTA_MGDL:
            continue

        artifacts.append(
            {
                "t": int(mid_t.replace(tzinfo=timezone.utc).timestamp()),
                "mgdl": round(mid_v),
                "dv": round(jump / 18.0, 2),
                "back": round(back / 18.0, 2),
                "dsec": round(into + out),
            }
        )

    return artifacts


def _stats(readings: list[tuple[datetime, float]]) -> dict | None:
    """Summarise a window: average, time in range, variability, GMI."""

    if not readings:
        return None

    pairs = _weigh(readings)
    values = [mgdl for _, mgdl in readings]
    count = len(values)
    total = sum(weight for _, weight in pairs)
    average = sum(value * weight for value, weight in pairs) / total

    in_range = sum(
        weight for value, weight in pairs if TARGET_LOW_MGDL <= value <= TARGET_HIGH_MGDL
    )
    below = sum(weight for value, weight in pairs if value < TARGET_LOW_MGDL)
    above = sum(weight for value, weight in pairs if value > TARGET_HIGH_MGDL)

    variance = sum(weight * (value - average) ** 2 for value, weight in pairs) / total
    deviation = variance**0.5

    return {
        "count": count,
        # Покрытые секунды окна. Наружу почти не смотрят, но именно по ним
        # _compare решает, есть ли с чем сравнивать: счёт замеров при
        # неоднородном шаге записи о покрытии не говорит ничего.
        "covered": round(total),
        "avg": round(average, 1),
        "min": round(min(values)),
        "max": round(max(values)),
        "tir": round(100 * in_range / total, 1),
        "below": round(100 * below / total, 1),
        "above": round(100 * above / total, 1),
        # Коэффициент вариации: ≤36% считается стабильной гликемией.
        "cv": round(100 * deviation / average, 1) if average else None,
    }


def _daily(
    readings: list[tuple[datetime, float]], now: datetime, span: timedelta
) -> list[dict]:
    """Summarise every local day the window touches, empty days included.

    Массив плотный: сутки без единого показания попадают в него с ``count: 0``.
    Иначе страница не отличила бы «сенсор был снят» от «таких суток не было»,
    и пропавший день молча сдвинул бы соседей друг к другу.

    ``start``/``end`` — unix-секунды наблюдаемого куска суток, обрезанного
    окном и «сейчас», а не календарные границы: сегодняшняя коробка стоит в
    середине прожитой части дня, а не обещает вечер, которого ещё не было.
    """

    zone = _zone()
    window_start = (now - span).replace(tzinfo=timezone.utc)
    window_end = now.replace(tzinfo=timezone.utc)

    by_day: dict[date, list[tuple[datetime, float]]] = {}
    for item in readings:
        # Правая граница окна — явная: когда «сейчас» совпадает с местной
        # полуночью, замер с меткой ровно в неё принадлежит дню нулевой
        # длины, которого в массиве нет, — и обязан быть отброшен здесь, а
        # не потеряться молча в дне, до которого не дойдёт цикл ниже.
        if item[0].replace(tzinfo=timezone.utc) >= window_end:
            continue
        local = item[0].replace(tzinfo=timezone.utc).astimezone(zone)
        by_day.setdefault(local.date(), []).append(item)

    days = []
    day = window_start.astimezone(zone).date()
    while True:
        # Полночь берётся из зоны, а не прибавлением 24 часов: сутки перехода
        # на летнее и зимнее время длятся 23 и 25 часов.
        midnight = datetime.combine(day, time(), tzinfo=zone).astimezone(timezone.utc)
        if midnight >= window_end:
            break
        next_midnight = datetime.combine(
            day + timedelta(days=1), time(), tzinfo=zone
        ).astimezone(timezone.utc)

        start = max(midnight, window_start)
        end = min(next_midnight, window_end)
        clipped = start > midnight or end < next_midnight

        day_readings = by_day.get(day, [])
        if not day_readings:
            days.append({"start": int(start.timestamp()), "end": int(end.timestamp()), "count": 0})
        else:
            # Знаменатель — фактическая длина наблюдаемого куска: полный
            # 25-часовой день перехода не помечается неполным.
            coverage = _covered(day_readings) / (end - start).total_seconds()
            # Веса — те же, что в _stats: коробка дня и его плитки обязаны
            # рассказывать об одном и том же дне одинаковым способом.
            pairs = _weigh(day_readings)
            days.append(
                {
                    "start": int(start.timestamp()),
                    "end": int(end.timestamp()),
                    **_stats(day_readings),
                    "p25": round(_weighted_percentile(pairs, 25)),
                    "p50": round(_weighted_percentile(pairs, 50)),
                    "p75": round(_weighted_percentile(pairs, 75)),
                    # Потолок: вес хвостовой точки может дать волосок сверх
                    # ста, а «101 %» читается как баг, не как полнота.
                    "coverage": min(100, round(100 * coverage)),
                    "partial": clipped or coverage < DAY_MIN_COVERAGE,
                    # Причина неполноты, а не только её факт: обрезанные окном
                    # сутки и сутки с молчавшим сенсором выглядят на графике
                    # одинаково, но говорят разное — «это ещё не весь день» и
                    # «этому дню верить меньше». Страница обязана назвать ту,
                    # которая случилась.
                    "clipped": clipped,
                }
            )

        day += timedelta(days=1)

    return days


def _gmi(readings: list[tuple[datetime, float]], now: datetime) -> dict | None:
    """Glucose Management Indicator over its own fortnight, or nothing.

    Возвращает ``None``, когда данных за две недели слишком мало: показать
    расчётный HbA1c по трём дням хуже, чем не показать ничего — число выглядит
    так же солидно, а означает совсем другое.
    """

    window = [item for item in readings if item[0] >= now - GMI_WINDOW]
    if not window:
        return None

    coverage = _covered(window) / GMI_WINDOW.total_seconds()
    if coverage < GMI_MIN_COVERAGE:
        return None

    # Средняя по времени, как и в _stats: формула GMI определена через
    # среднюю глюкозу за период, а не через среднее по строкам журнала.
    pairs = _weigh(window)
    average = sum(value * weight for value, weight in pairs) / sum(
        weight for _, weight in pairs
    )

    return {
        "value": round(3.31 + 0.02392 * average, 1),
        "days": GMI_WINDOW.days,
        # Потолок — как у покрытия дня: хвостовой вес даёт волосок сверх ста.
        "coverage": min(100, round(100 * coverage)),
    }


def _compare(stats: dict, previous: dict | None, span: timedelta) -> dict:
    """Окно против предыдущего такого же: дельты, оценка, значимость.

    Когда сравнивать не с чем, публикуется причина, а не пустота: «предыдущего
    периода нет» и «в нём слишком мало измерений» — разные фразы, и выбирать
    между ними должен тот, кто знает счёт, то есть сборщик.

    ``cv`` не сравнивается нарочно: вариабельность — это 100·σ/среднее, и при
    падающем среднем с тем же разбросом она растёт чисто арифметически —
    улучшающийся дневник получал бы «хуже». Стрелку, которую пришлось бы
    оговаривать в подписи карточки, честнее не рисовать вовсе.
    """

    days = span // timedelta(days=1)
    if previous is None:
        return {"days": days, "count": 0, "reason": "no_data"}

    if previous["covered"] < COMPARE_MIN_COVERAGE * span.total_seconds():
        return {"days": days, "count": previous["count"], "reason": "thin"}

    avg_delta = round(stats["avg"] - previous["avg"], 1)
    tir_delta = round(stats["tir"] - previous["tir"], 1)

    return {
        "days": days,
        "count": previous["count"],
        "reason": None,
        "avg": {
            "was": previous["avg"],
            "delta": avg_delta,
            # «Меньше среднее — лучше» верно ровно до порога гипогликемии:
            # среднее, упавшее ниже него, — не успех, а выросшее не обязано
            # быть провалом (подъём из гипогликемии — движение к цели).
            # Оценка ставится только там, где она честная; остальное — null.
            # Порог — тот же TARGET_LOW_MGDL, которым красится кривая.
            "better": (
                True if avg_delta < 0 and stats["avg"] >= TARGET_LOW_MGDL else None
            ),
            "significant": abs(avg_delta) >= COMPARE_MIN_AVG_MGDL,
        },
        "tir": {
            "was": previous["tir"],
            "delta": tir_delta,
            # Доле времени в диапазоне оба направления понятны без оговорок.
            "better": tir_delta > 0 if tir_delta != 0 else None,
            "significant": abs(tir_delta) >= COMPARE_MIN_TIR_PP,
        },
    }


#: Насколько прогноз может отстать от «сейчас» и остаться в снимке. Та же
#: мера, которой страница гасит значение в шапке (STALE_AFTER_MS): прогноз,
#: считанный от точки, которой уже нет на правом краю, — хвост у чужой кривой.
FORECAST_MAX_AGE = timedelta(minutes=20)


def _forecast(
    rows: list[tuple[datetime, int, float, str]], now: datetime
) -> dict | None:
    """Хвост модели для страницы: от какой точки, какая модель, куда ведёт.

    Точки — абсолютное время и мг/дл, как у кривой: странице не нужно
    складывать горизонты самой. Устаревший прогноз не публикуется вовсе, а не
    помечается: страница, увидев ключ, нарисует хвост, и единственный способ
    ей этого не позволить — ключа не дать.
    """

    if not rows:
        return None
    made_at = rows[0][0]
    if now - made_at > FORECAST_MAX_AGE:
        return None
    return {
        "made_at": int(made_at.replace(tzinfo=timezone.utc).timestamp()),
        "model": rows[0][3],
        "points": [
            [
                int((made_at + timedelta(minutes=horizon)).replace(tzinfo=timezone.utc).timestamp()),
                round(mgdl),
            ]
            for _, horizon, mgdl, _ in sorted(rows, key=lambda row: row[1])
        ],
    }


def _trend(readings: list[tuple[datetime, float]]) -> dict | None:
    """Latest reading plus its rate of change in mg/dL per minute."""

    if not readings:
        return None

    timestamp, mgdl = readings[-1]
    latest = {
        "t": int(timestamp.replace(tzinfo=timezone.utc).timestamp()),
        "mgdl": round(mgdl),
        "rate": None,
    }

    cutoff = timestamp - TREND_WINDOW
    earlier = [(ts, value) for ts, value in readings if ts <= cutoff]
    if earlier:
        past_timestamp, past_mgdl = earlier[-1]
        minutes = (timestamp - past_timestamp).total_seconds() / 60
        if minutes > 0:
            latest["rate"] = round((mgdl - past_mgdl) / minutes, 2)

    return latest


def _events(
    journal: list[tuple[datetime, str, float | None, float | None]], since: datetime
) -> dict:
    """Group journal entries into the lanes the page draws.

    Записи без своей величины пропускаются: столбик нулевой высоты на панели
    неотличим от её отсутствия, а место в снимке занимает.

    Исключение — смена ручки (``pen_bolus``/``pen_basal``): у неё величины нет
    по построению, это отметка момента, а не количество. Она идёт отдельным
    списком ``pens`` парами «момент, какой инсулин»: страница рисует её в
    дорожке инсулина, а не своим столбиком, — иначе с этого момента разбор
    стал бы читать ноль единиц там, где просто открыли новую ручку.
    """

    lanes: dict[str, list[list]] = {"meals": [], "bolus": [], "basal": [], "pens": []}

    for occurred_at, kind, carbs, units in journal:
        if occurred_at < since:
            continue

        seconds = int(occurred_at.replace(tzinfo=timezone.utc).timestamp())

        if kind in PEN_KINDS:
            lanes["pens"].append([seconds, PEN_KINDS[kind]])
            continue

        if kind == "meal":
            lane, amount = "meals", carbs
        elif kind in ("bolus", "basal"):
            lane, amount = kind, units
        else:
            continue

        if amount is None:
            continue

        lanes[lane].append([seconds, round(amount, 1)])

    return lanes


def _sugars(
    fingersticks: list[tuple[datetime, float]], since: datetime
) -> list[dict]:
    """Замеры глюкометром как ``[{t, mgdl}]`` — точки поверх кривой.

    Не ряд и не дорожка: это та же величина, что у кривой, в тех же единицах и
    на той же оси, поэтому замер стоит на своём значении рядом с точкой
    сенсора. Вторая шкала или своя дорожка внизу разорвали бы ровно ту связь,
    ради которой замер и записывают: видно должно быть расхождение, а не два
    числа по отдельности.

    В сам ряд сенсора они при этом не подмешиваются: кривую рисует сенсор, и
    время в диапазоне, AGP и ночные сводки считаются по ней одной.
    """

    return [
        {
            "t": int(moment.replace(tzinfo=timezone.utc).timestamp()),
            "mgdl": round(mgdl, 1),
        }
        for moment, mgdl in fingersticks
        if moment >= since
    ]


def build_snapshot(
    readings: list[tuple[datetime, float]],
    journal: list[tuple[datetime, str, float | None, float | None]],
    now: datetime,
    last_success: float | None = None,
    latest: dict | None = None,
    origins: dict[datetime, list[dict]] | None = None,
    fingersticks: list[tuple[datetime, float]] | None = None,
    sensor_started: datetime | None = None,
    sensor_ends: datetime | None = None,
    forecast_rows: list[tuple[datetime, int, float, str]] | None = None,
    zones: list[tuple[datetime, str]] | None = None,
    sensor_changes: list[datetime] | None = None,
) -> dict:
    """Assemble the snapshot the page reads. Pure: no database, no clock.

    Отдельно от ``publish`` ради ``preview.py``: собирая снимок сам, превью
    показывало бы вчерашнюю форму страницы и молча расходилось бы с боевой —
    именно так оно и проглядело добавленный ключ ``gmi``.
    """

    series, stats = {}, {}
    for name, (span, step_minutes) in RANGES.items():
        subset = [item for item in readings if item[0] >= now - span]
        if step_minutes is None:
            series[name] = {"kind": "daily", "days": _daily(subset, now, span)}
        else:
            series[name] = {
                "kind": "points",
                "step": step_minutes,
                "points": _downsample(subset, step_minutes, TARGET_LOW_MGDL),
            }

        window_stats = _stats(subset)
        # Сравниваются только окна из COMPARED_RANGES. Почасовые — нет: день
        # против вчерашнего на CGM — в основном шум, стрелка переворачивалась бы
        # почти каждый день. Тот же довод у карточки GMI. 60 и 90 дней — нет
        # из-за цены выборки (см. COMPARED_RANGES). Явная проверка на None
        # обязательна: {**None} бросает TypeError, а месяц без данных —
        # штатный случай.
        if window_stats is None or name not in COMPARED_RANGES:
            stats[name] = window_stats
        else:
            # Граница справа строгая: текущий срез берёт >= now - span, и
            # замер ровно на стыке не должен попасть в оба окна.
            prev_subset = [
                item for item in readings if now - 2 * span <= item[0] < now - span
            ]
            stats[name] = {
                **window_stats,
                "prev": _compare(window_stats, _stats(prev_subset), span),
            }

    meals = [
        (occurred_at, carbs)
        for occurred_at, kind, carbs, _ in journal
        if kind == "meal" and carbs is not None
    ]
    # Только короткий инсулин: базальный — суточный фон, к еде он не относится.
    boluses = [
        (occurred_at, units)
        for occurred_at, kind, _, units in journal
        if kind == "bolus" and units is not None
    ]

    # Срез под обе полосы эпизодов — один: нарезать его дважды значило бы
    # пройти недельное сырьё лишний раз ради того же списка.
    episode_readings = [item for item in readings if item[0] >= now - EPISODE_WINDOW]

    return {
        "generated_at": int(now.replace(tzinfo=timezone.utc).timestamp()),
        "collector": {
            "last_success": int(last_success) if last_success else None,
        },
        "target": {
            "low": TARGET_LOW_MGDL,
            "mid": TARGET_MID_MGDL,
            "high": TARGET_HIGH_MGDL,
        },
        # Зона, в которой сборщик нарезает сутки. Страница держит ту же зону
        # своей константой и по этому ключу может её сверить, а не верить.
        "timezone": DISPLAY_TZ,
        "latest": latest if latest is not None else _trend(readings),
        # Хвост модели. None — и страница рисует линейный, как прежде: ключ
        # отсутствует ровно тогда, когда рисовать по модели нечестно.
        "forecast": _forecast(forecast_rows or [], now),
        # Остаток короткого инсулина и углеводов — те же IOB и COB, что видит
        # модель прогноза (кривые в active.py). По всему журналу, а не по
        # EVENT_WINDOW: окно событий шире пяти часов действия, но связывать их
        # незачем.
        "active": active_now(journal, now),
        "series": series,
        "stats": stats,
        # Своё окно, не выбранное на странице — см. GMI_WINDOW.
        "gmi": _gmi(readings, now),
        # Профиль обычного дня — тоже по своему окну (AGP_WINDOW): «обычно»
        # не зависит от того, какая панель открыта.
        "profile": day_profile(readings, now),
        # И ночи — по своему (NIGHTS_WINDOW). Порог гипогликемии передаётся, а
        # не берётся модулем у себя: «ниже целевого диапазона» на странице и
        # «ночная гипогликемия» обязаны означать одно число.
        "nights": night_summary(readings, journal, now, hypo_mgdl=TARGET_LOW_MGDL),
        "events": _events(journal, now - EVENT_WINDOW),
        # Сверки глюкометром — по тому же окну, что события: рисуются они на тех
        # же почасовых панелях, и своя константа для того же окна разъехалась бы
        # с ними при первой же правке.
        "sugars": _sugars(fingersticks or [], now - EVENT_WINDOW),
        # Эпизоды вне нормы — тоже по своему окну, и тоже по сырью: прореженная
        # кривая не отвечает ни на «сколько раз», ни на «сколько минут».
        # Порог каждой полосы — тот же, которым красится кривая и подложка зон:
        # своя константа однажды разошлась бы с ними, и полоса появлялась бы
        # там, где график ещё зелёный.
        "lows": low_episodes(episode_readings, TARGET_LOW_MGDL),
        "highs": high_episodes(episode_readings, TARGET_HIGH_MGDL),
        # Молчание сенсора — тоже по сырью и тоже по своему окну: полосу
        # «нет сигнала» рисуют только почасовые панели.
        "gaps": _gaps(readings, now - GAP_WINDOW),
        # Сам сенсор: сколько ему осталось и сколько данных от него дошло.
        # Отметка установки приходит снаружи, как и last_success: снимок
        # собирается без базы, иначе его не собрал бы preview.py.
        "sensor": _sensor(
            sensor_started, sensor_ends, readings, now, fingersticks or []
        ),
        # Установки сенсоров — пунктир «новый сенсор» на всех панелях, поэтому
        # окно — самой длинной из них, квартала.
        "sensor_changes": [
            int(moment.replace(tzinfo=timezone.utc).timestamp())
            for moment in (sensor_changes or [])
            if moment >= now - RANGES["quarter"][0]
        ],
        # Окно — двух суток (RANGES["two_days"]), а не отдельная константа:
        # кольца рисуют обе почасовые панели, и второго источника правды для
        # этого окна заводить незачем.
        "artifacts": _artifacts(readings, now - RANGES["two_days"][0]),
        "analysis": analyse(
            meals,
            readings,
            now,
            hypo_mgdl=TARGET_LOW_MGDL,
            boluses=boluses,
            origins=origins,
            zones=zones,
        ),
    }


def _db_last_success() -> float | None:
    """Отметка сборщика из базы, приведённая к epoch."""

    stored = read_last_success()
    if stored is None:
        return None

    # Зону возвращаем явно: иначе timestamp() истолкует наивный UTC по зоне
    # хоста и сдвинет отметку на смещение — на московском это три часа.
    return stored.replace(tzinfo=timezone.utc).timestamp()


def _stored_last_success(path: str) -> float | None:
    """Прошлое значение ``last_success`` из уже опубликованного снимка.

    ``last_success`` живёт в памяти сборщика и умирает с его процессом, а
    разовый запуск ``publish.py`` не знает его вовсе. Ни то ни другое не
    означает «сборщик ещё не получал данные» — только «этому процессу не
    докладывали». Честнее унаследовать отметку из прежнего снимка, чем
    повесить на живую страницу ложное предупреждение под свежими цифрами.
    """

    try:
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
        value = stored["collector"]["last_success"]
        # float() — внутри try: чужое значение вроде строки обязано дать
        # «наследовать нечего», а не ронять каждую публикацию, пока файл
        # не поправят руками.
        return float(value) if value else None
    except (OSError, ValueError, KeyError, TypeError):
        # Нет файла или он не о том — значит, наследовать нечего.
        return None


def publish(path: str = PUBLISH_PATH, last_success: float | None = None) -> None:
    """Write the snapshot atomically so nginx never serves a half-written file.

    ``last_success`` is when the collector last reached LibreLinkUp. It is
    published separately from ``generated_at`` because the two diverge exactly
    when it matters: while Abbott is unreachable the snapshot keeps being
    rewritten, but the data behind it stops moving, and the page has to say so
    rather than quietly showing yesterday's glucose as current. When the
    caller does not know it (a standalone run, a freshly restarted collector),
    the mark is inherited from the snapshot being replaced.
    """

    if last_success is None:
        # База первая: на другом хосте снимок свой и наследовать из него
        # нечего. Файл — запасной путь, пока сборщик не записал отметку.
        last_success = _db_last_success() or _stored_last_success(path)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Удвоенное окно — ради сравнения с предыдущим периодом. Откат этой
    # строки превратил бы месячное сравнение в вечное «сравнивать не с чем»
    # без единого внешнего признака, поэтому на неё есть тест. Сравниваемым
    # окнам — двойная длина, остальным — своя: сейчас это 90 дней квартала,
    # около 130 тысяч строк при минутном опросе. На вес data.json выборка не
    # влияет, серии режутся по своим окнам.
    window = max(
        span * (2 if name in COMPARED_RANGES else 1)
        for name, (span, _) in RANGES.items()
    )
    readings = readings_since(now - window)

    # Журнал ведёт бот, и его может не быть вовсе — тогда список пуст, панели
    # событий на странице просто не появятся.
    journal = journal_since(now - ANALYSIS_WINDOW)

    # Отметку пишет сборщик; на рендерере она приезжает репликацией. Нет
    # её — карточка сенсора покажет только полноту данных.
    sensor_started = read_sensor_start()

    # Сверки глюкометром — за двое суток для графика и за всю жизнь сенсора
    # для его сверки (``_bias``): страница всё равно режет точки по своему
    # окну в ``_sugars``, а в снимок уходит только смещение.
    fingersticks_from = now - EVENT_WINDOW
    if sensor_started is not None:
        fingersticks_from = min(fingersticks_from, sensor_started)

    snapshot = build_snapshot(
        readings,
        journal,
        now,
        last_success=last_success,
        origins=meal_origins_since(now - ANALYSIS_WINDOW),
        fingersticks=fingersticks_since(fingersticks_from),
        # Не из readings: последнее измерение может быть старше окна графиков,
        # и тогда странице нужно показать «данных нет с такого-то числа».
        # Сорок строк, а не десять: выборка обязана накрыть TREND_WINDOW при
        # минутном опросе, иначе точки старше 15 минут в ней нет, rate выходит
        # null — и страница молча гасит прогноз со стрелкой тренда.
        latest=_trend(last_readings(40)),
        sensor_started=sensor_started,
        sensor_ends=read_sensor_end(),
        # Бот пишет прогноз на каждую точку; нет таблицы или строк — пусто, и
        # хвост остаётся линейным.
        forecast_rows=latest_forecast(),
        # Смены пояса из бота — время суток приёма для коэффициента.
        zones=timezone_history(),
        sensor_changes=sensor_starts_since(now - RANGES["quarter"][0]),
    )

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    # NamedTemporaryFile в том же каталоге: os.replace атомарен только внутри
    # одной файловой системы.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
        ) as handle:
            temp_path = handle.name
            json.dump(snapshot, handle, separators=(",", ":"))
            # Переименование атомарно для читателя, но не для диска: без
            # сброса оно может лечь раньше самих данных, и жёсткая перезагрузка
            # оставит на месте живого снимка обрезанный.
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(temp_path, 0o644)
        os.replace(temp_path, path)
    except BaseException:
        # Каталог раздаёт nginx: недописанный снимок нельзя оставлять в нём
        # лежать. На успешном пути удалять нечего — replace уже переименовал.
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise


if __name__ == "__main__":
    # Ручной пересбор снимка — сам себе точка входа, и прочитать настройки
    # некому: сборщик зовёт load_dotenv() у себя, а импортируемый модуль
    # навязывать .env не вправе — превью обходится без него вовсе. Отсюда и
    # импорт здесь, а не наверху: загрузка окружения принадлежит запуску.
    from dotenv import load_dotenv

    load_dotenv()

    # Путь при этом остаётся тем, что вычислен при импорте, — то есть строку
    # PUBLISH_PATH в .env не увидит никто, и ручной пересбор тоже. Перечитать
    # её здесь было бы хуже, чем не читать вовсе: у сборщика путь связан
    # аргументом по умолчанию, а превью импортирует константу по значению, так
    # что слушался бы её один этот запуск — писал бы в свой файл и штамповал в
    # него «сборщик ещё не получал данные». Задавать переменную нужно в
    # окружении, тогда её видят все трое.
    publish()
