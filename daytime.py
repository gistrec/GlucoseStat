"""Местные сутки: зона отображения и перцентили.

Модуль без обратных зависимостей: его импортируют и ``publish.py`` (подневная
сводка месяца), и профиль обычного дня, и превью. Зона одна на всех и та же,
что константа ``TIMEZONE`` в ``web/app.js``: сутки, нарезанные сборщиком, и
подписи, которые ставит страница, обязаны совпадать — иначе коробка дня и его
дата разъезжаются на час-два, и никто этого не видит.
"""

import math
import os
from functools import lru_cache
from zoneinfo import ZoneInfo

# Через ``or``, а не значением по умолчанию: пустая переменная в окружении
# должна означать «зона не задана», а не зону с пустым именем.
DISPLAY_TZ = os.getenv("DISPLAY_TZ") or "Europe/Belgrade"


@lru_cache(maxsize=1)
def _zone() -> ZoneInfo:
    """Разрешение зоны один раз: ``ZoneInfo`` читает базу tzdata с диска."""

    return ZoneInfo(DISPLAY_TZ)


def _percentile(values: list[float], q: float) -> float:
    """Перцентиль с линейной интерполяцией (R type 7); ``q`` — в процентах.

    Своя, а не ``statistics.quantiles``: на списке из одного элемента 3.11
    бросает ``StatisticsError``, а 3.13 возвращает три копии значения. День с
    единственным замером бывает при смене сенсора, и форма снимка не должна
    зависеть от версии интерпретатора на хосте.
    """

    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _weighted_percentile(pairs: list[tuple[float, float]], q: float) -> float:
    """Перцентиль пар ``(значение, вес)``; при равных весах — та же R type 7.

    Позиция значения — накопленный вес слева, делённый на полный вес без
    последнего: при равных весах это в точности (i-1)/(n-1) обычного type 7,
    так что переход на веса сам по себе не сдвигает ни одного числа — сдвиг
    появляется только там, где веса действительно разные.
    """

    ordered = sorted(pairs)
    if len(ordered) == 1:
        return ordered[0][0]

    span = sum(weight for _, weight in ordered) - ordered[-1][1]
    if span <= 0:
        return ordered[-1][0]

    target = span * q / 100
    seen = 0.0
    for (value, weight), (following, _) in zip(ordered, ordered[1:]):
        if seen + weight >= target:
            if weight == 0:
                return value
            return value + (following - value) * (target - seen) / weight
        seen += weight
    return ordered[-1][0]
