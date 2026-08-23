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

from database.queries import last_readings, readings_since


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
        # Glucose Management Indicator — оценка HbA1c по среднему CGM
        # (Bergenstal et al., 2018). Осмысленна на окне от двух недель,
        # поэтому на дневной панели её не показываем.
        "gmi": round(3.31 + 0.02392 * average, 1),
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


def publish(path: str = PUBLISH_PATH, last_success: float | None = None) -> None:
    """Write the snapshot atomically so nginx never serves a half-written file.

    ``last_success`` is when the collector last reached LibreLinkUp. It is
    published separately from ``generated_at`` because the two diverge exactly
    when it matters: while Abbott is unreachable the snapshot keeps being
    rewritten, but the data behind it stops moving, and the page has to say so
    rather than quietly showing yesterday's glucose as current.
    """

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    window = max(span for span, _ in RANGES.values())
    readings = readings_since(now - window)

    series, stats = {}, {}
    for name, (span, step_minutes) in RANGES.items():
        subset = [item for item in readings if item[0] >= now - span]
        series[name] = {"step": step_minutes, "points": _downsample(subset, step_minutes)}
        stats[name] = _stats(subset)

    snapshot = {
        "generated_at": int(now.replace(tzinfo=timezone.utc).timestamp()),
        "collector": {
            "last_success": int(last_success) if last_success else None,
        },
        "target": {"low": TARGET_LOW_MGDL, "high": TARGET_HIGH_MGDL},
        # Не из readings: последнее измерение может быть старше окна графиков,
        # и тогда странице нужно показать «данных нет с такого-то числа».
        "latest": _trend(last_readings()),
        "series": series,
        "stats": stats,
    }

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
