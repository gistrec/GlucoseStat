"""Сколько короткого инсулина ещё работает и сколько углеводов ещё всасывается.

Кривые — копия ``ml/features.py`` из GlucoseBot, числа в числа. Там по ним
считаются IOB и COB, которые получает модель прогноза, и плашка на странице
обязана показывать ту же величину, что видит модель: «2,1 ед ещё работает»
рядом с хвостом прогноза, посчитанным по другим 2,1, — два ответа на один
вопрос. Меняются кривые там — меняются и здесь, вместе.

Обе кривые не подобраны по нашим данным, а взяты готовыми: инсулин — по oref0
(НовоРапид, действие 5 ч, пик 75 мин), углеводы — линейно за 4 часа после
15 минут задержки. «Всасывание до 01:10» поэтому — оценка модели, а не
измерение, и подпись на странице это говорит.
"""

import math
from datetime import datetime, timedelta, timezone

DIA_MINUTES = 300.0
PEAK_MINUTES = 75.0
CARB_DELAY_MINUTES = 15.0
CARB_ABSORPTION_MINUTES = 240.0

# Ниже этого плашка молчит: «0,0 ед ещё работает» сообщает только то, что
# хвост кривой формально не кончился.
MIN_UNITS = 0.05
MIN_CARBS = 0.5


def iob_fraction(minutes: float) -> float:
    """Доля болюса, ещё действующая через ``minutes`` после укола (oref0)."""

    dia, peak = DIA_MINUTES, PEAK_MINUTES
    if minutes < 0 or minutes >= dia:
        return 0.0

    tau = peak * (1 - peak / dia) / (1 - 2 * peak / dia)
    a = 2 * tau / dia
    s = 1 / (1 - a + (1 + a) * math.exp(-dia / tau))
    t = minutes
    return 1 - s * (1 - a) * (
        (t * t / (tau * dia * (1 - a)) - t / tau - 1) * math.exp(-t / tau) + 1
    )


def cob_fraction(minutes: float) -> float:
    """Доля углеводов, ещё не всосавшаяся через ``minutes`` после еды."""

    if minutes < 0:
        return 0.0
    if minutes <= CARB_DELAY_MINUTES:
        return 1.0
    return max(0.0, 1.0 - (minutes - CARB_DELAY_MINUTES) / CARB_ABSORPTION_MINUTES)


def _seconds(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def _remaining(
    entries: list[tuple[datetime, float]],
    now: datetime,
    fraction,
    lasts: timedelta,
    threshold: float,
) -> dict | None:
    """Остаток по записям, которые ещё действуют, и рамка вокруг него.

    ``of`` — сумма только действующих записей, а не всего за день: полоса
    показывает, какая доля от того, что ещё в игре, осталась, и вчерашний
    ужин в знаменателе сделал бы её короче правды.
    """

    live = [
        (moment, amount, fraction((now - moment).total_seconds() / 60))
        for moment, amount in entries
    ]
    live = [(moment, amount, left) for moment, amount, left in live if left > 0]
    left = sum(amount * part for _, amount, part in live)
    if left < threshold:
        return None

    last = max(moment for moment, _, _ in live)
    return {
        "left": round(left, 1),
        "of": round(sum(amount for _, amount, _ in live), 1),
        "last": _seconds(last),
        "until": _seconds(last + lasts),
    }


def active_now(
    journal: list[tuple[datetime, str, float | None, float | None]], now: datetime
) -> dict:
    """Ключ ``active`` снимка: ``insulin`` и ``carbs``, каждый или None.

    Только короткий инсулин: длинный — суточный фон, его «остаток» не про
    последний укол и в кривую oref0 не ложится.
    """

    boluses = [
        (moment, units)
        for moment, kind, _, units in journal
        if kind == "bolus" and units is not None
    ]
    meals = [
        (moment, carbs)
        for moment, kind, carbs, _ in journal
        if kind == "meal" and carbs is not None
    ]

    insulin = _remaining(
        boluses, now, iob_fraction, timedelta(minutes=DIA_MINUTES), MIN_UNITS
    )
    if insulin:
        # Пик — у последнего укола: «пик прошёл» спрашивает о нём, а не о
        # завтраке, который давно отработал свой.
        insulin["peak"] = insulin["last"] + int(PEAK_MINUTES * 60)

    carbs = _remaining(
        meals,
        now,
        cob_fraction,
        timedelta(minutes=CARB_DELAY_MINUTES + CARB_ABSORPTION_MINUTES),
        MIN_CARBS,
    )
    return {"insulin": insulin, "carbs": carbs}
