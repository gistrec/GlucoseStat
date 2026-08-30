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
from datetime import datetime, timedelta, timezone

from analysis import analyse
from database.queries import journal_since, last_readings, readings_since


PUBLISH_PATH = os.getenv(
    "PUBLISH_PATH", os.path.join(os.path.dirname(__file__), "web", "data.json")
)

# Стандартный целевой диапазон для CGM (ADA/ATTD consensus): 70–180 mg/dL,
# те же 3.9–10.0 mmol/L, что показывает сам Libre.
TARGET_LOW_MGDL = 70
TARGET_HIGH_MGDL = 180

# Окно и шаг прореживания на период. Сырьё идёт с шагом 5 минут, но 30 дней
# в таком виде — это 8600 точек: график столько не покажет, а вес страницы
# вырастет на порядок. Статистика при этом всегда считается по сырым данным.
RANGES = {
    "day": (timedelta(days=1), 5),
    "week": (timedelta(days=7), 15),
    "month": (timedelta(days=30), 60),
}

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

# Номинальный шаг CGM: 288 измерений в сутки. Libre 3 отдаёт чаще, так что
# порог покрытия получается консервативным — и хорошо.
CGM_READINGS_PER_DAY = 288

# События рисуются только на суточной панели: сотня отметок на месячном окне
# сливается в сплошную полосу, из которой ничего не прочитать.
EVENT_WINDOW = timedelta(days=1)

# А разбор приёмов пищи собирается за две недели: на суточном окне выборки
# слишком мало, чтобы медиана подъёма что-то значила.
ANALYSIS_WINDOW = timedelta(days=14)


def _downsample(
    readings: list[tuple[datetime, float]], step_minutes: int
) -> list[list[int]]:
    """Average readings into fixed buckets, as ``[unix_seconds, mg/dL]``."""

    if not readings:
        return []

    step = step_minutes * 60
    buckets: dict[int, list[float]] = {}
    for timestamp, mgdl in readings:
        bucket = int(timestamp.replace(tzinfo=timezone.utc).timestamp()) // step * step
        buckets.setdefault(bucket, []).append(mgdl)

    return [
        [bucket, round(sum(values) / len(values))]
        for bucket, values in sorted(buckets.items())
    ]


def _stats(readings: list[tuple[datetime, float]]) -> dict | None:
    """Summarise a window: average, time in range, variability, GMI."""

    if not readings:
        return None

    values = [mgdl for _, mgdl in readings]
    count = len(values)
    average = sum(values) / count

    in_range = sum(1 for v in values if TARGET_LOW_MGDL <= v <= TARGET_HIGH_MGDL)
    below = sum(1 for v in values if v < TARGET_LOW_MGDL)
    above = sum(1 for v in values if v > TARGET_HIGH_MGDL)

    variance = sum((v - average) ** 2 for v in values) / count
    deviation = variance**0.5

    return {
        "count": count,
        "avg": round(average, 1),
        "min": round(min(values)),
        "max": round(max(values)),
        "tir": round(100 * in_range / count, 1),
        "below": round(100 * below / count, 1),
        "above": round(100 * above / count, 1),
        # Коэффициент вариации: ≤36% считается стабильной гликемией.
        "cv": round(100 * deviation / average, 1) if average else None,
    }


def _gmi(readings: list[tuple[datetime, float]], now: datetime) -> dict | None:
    """Glucose Management Indicator over its own fortnight, or nothing.

    Возвращает ``None``, когда данных за две недели слишком мало: показать
    расчётный HbA1c по трём дням хуже, чем не показать ничего — число выглядит
    так же солидно, а означает совсем другое.
    """

    window = [item for item in readings if item[0] >= now - GMI_WINDOW]
    if not window:
        return None

    expected = CGM_READINGS_PER_DAY * GMI_WINDOW.days
    coverage = len(window) / expected
    if coverage < GMI_MIN_COVERAGE:
        return None

    average = sum(mgdl for _, mgdl in window) / len(window)

    return {
        "value": round(3.31 + 0.02392 * average, 1),
        "days": GMI_WINDOW.days,
        # Больше 100 % — норма: Libre отдаёт чаще номинальных 288 в сутки.
        "coverage": round(100 * coverage),
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
    """Group journal entries into the three lanes the page draws.

    Записи без своей величины пропускаются: столбик нулевой высоты на панели
    неотличим от её отсутствия, а место в снимке занимает.
    """

    lanes: dict[str, list[list[float]]] = {"meals": [], "bolus": [], "basal": []}

    for occurred_at, kind, carbs, units in journal:
        if occurred_at < since:
            continue

        if kind == "meal":
            lane, amount = "meals", carbs
        elif kind in ("bolus", "basal"):
            lane, amount = kind, units
        else:
            continue

        if amount is None:
            continue

        seconds = int(occurred_at.replace(tzinfo=timezone.utc).timestamp())
        lanes[lane].append([seconds, round(amount, 1)])

    return lanes


def build_snapshot(
    readings: list[tuple[datetime, float]],
    journal: list[tuple[datetime, str, float | None, float | None]],
    now: datetime,
    last_success: float | None = None,
    latest: dict | None = None,
) -> dict:
    """Assemble the snapshot the page reads. Pure: no database, no clock.

    Отдельно от ``publish`` ради ``preview.py``: собирая снимок сам, превью
    показывало бы вчерашнюю форму страницы и молча расходилось бы с боевой —
    именно так оно и проглядело добавленный ключ ``gmi``.
    """

    series, stats = {}, {}
    for name, (span, step_minutes) in RANGES.items():
        subset = [item for item in readings if item[0] >= now - span]
        series[name] = {"step": step_minutes, "points": _downsample(subset, step_minutes)}
        stats[name] = _stats(subset)

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

    return {
        "generated_at": int(now.replace(tzinfo=timezone.utc).timestamp()),
        "collector": {
            "last_success": int(last_success) if last_success else None,
        },
        "target": {"low": TARGET_LOW_MGDL, "high": TARGET_HIGH_MGDL},
        "latest": latest if latest is not None else _trend(readings),
        "series": series,
        "stats": stats,
        # Своё окно, не выбранное на странице — см. GMI_WINDOW.
        "gmi": _gmi(readings, now),
        "events": _events(journal, now - EVENT_WINDOW),
        "analysis": analyse(
            meals, readings, now, hypo_mgdl=TARGET_LOW_MGDL, boluses=boluses
        ),
    }


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
        last_success = _stored_last_success(path)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    window = max(span for span, _ in RANGES.values())
    readings = readings_since(now - window)

    # Журнал ведёт бот, и его может не быть вовсе — тогда список пуст, панели
    # событий на странице просто не появятся.
    journal = journal_since(now - ANALYSIS_WINDOW)

    snapshot = build_snapshot(
        readings,
        journal,
        now,
        last_success=last_success,
        # Не из readings: последнее измерение может быть старше окна графиков,
        # и тогда странице нужно показать «данных нет с такого-то числа».
        latest=_trend(last_readings()),
    )

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    # NamedTemporaryFile в том же каталоге: os.replace атомарен только внутри
    # одной файловой системы.
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(snapshot, handle, separators=(",", ":"))
        temp_path = handle.name

    os.chmod(temp_path, 0o644)
    os.replace(temp_path, path)


if __name__ == "__main__":
    publish()
