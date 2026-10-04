"""Эпизоды вне нормы: когда, как долго, как далеко за порогом.

Отдельным модулем, а не функцией внутри ``publish``, потому что считают их
двое: сам снимок — чтобы нарисовать полосы над кривой и под ней, — и ночная
сводка, которой нужны минуты внутри каждой ночи. Держать эту арифметику у
одного из них значило бы либо завести круг из импортов, либо посчитать эпизоды
дважды разными способами, и однажды разойтись в ответе на «сколько их было».

Считается по сырым замерам. Прореженная кривая на такие вопросы не отвечает:
два провала в соседних корзинах выглядят на ней одним, а минута на недельной
панели занимает четверть пикселя.

Низ и верх считает один и тот же проход: правила у них общие — границы по
пересечению порога, склейка дребезга, минимальная длительность, — и разойтись
им нельзя. Разное только направление сравнения и крайнее значение внутри
эпизода: у провала это дно, у подъёма — вершина.
"""

from datetime import datetime, timedelta, timezone

# Разрыв, который ещё не разделяет два эпизода. Пятнадцать минут — то же
# правило, по которому consensus-документы считают гипогликемию законченной:
# без него дребезг вокруг порога плодил бы пяток «эпизодов» там, где человек
# пережил один. Минимальной длительности у эпизода нарочно нет: провал в две
# минуты — тоже провал, и прятать его значило бы повторить ошибку прореживания
# другим способом.
LOW_GAP = timedelta(minutes=15)


def low_episodes(readings: list[tuple[datetime, float]], low: int) -> list[dict]:
    """Список эпизодов ниже ``low`` в порядке времени.

    Крайнее значение эпизода — ``min``, дно провала.
    """

    return _episodes(readings, low, below=True)


def high_episodes(readings: list[tuple[datetime, float]], high: int) -> list[dict]:
    """Список эпизодов выше ``high`` в порядке времени.

    Зеркально ``low_episodes``, вплоть до пятнадцатиминутной склейки: подъём,
    на полчаса заглянувший под порог и вернувшийся, — один эпизод, а не два.
    Крайнее значение здесь ``max``, вершина подъёма.
    """

    return _episodes(readings, high, below=False)


def _episodes(
    readings: list[tuple[datetime, float]], threshold: int, *, below: bool
) -> list[dict]:
    """Эпизоды по одну сторону ``threshold``.

    Длительность — от первого замера за порогом до первого замера обратно за
    ним, а не до последнего крайнего: сенсор отдаёт точку раз в минуту-пять, и
    без правого края одиночный выброс получал бы нулевую длину.
    """

    def outside(mgdl: float) -> bool:
        return mgdl < threshold if below else mgdl > threshold

    peak_key = "min" if below else "max"
    peak = min if below else max

    episodes: list[dict] = []
    current: list[tuple[datetime, float]] = []
    inside: datetime | None = None

    def close(right: datetime | None) -> None:
        if not current:
            return
        start, finish = current[0][0], right or current[-1][0]
        episodes.append(
            {
                "start": int(start.replace(tzinfo=timezone.utc).timestamp()),
                "end": int(finish.replace(tzinfo=timezone.utc).timestamp()),
                peak_key: round(peak(mgdl for _, mgdl in current)),
                "minutes": max(1, round((finish - start).total_seconds() / 60)),
            }
        )
        current.clear()

    for moment, mgdl in readings:
        if outside(mgdl):
            # Левый край — предыдущий замер по эту сторону порога: пересечение
            # случилось между ними, и относить его целиком к первому замеру за
            # порогом значило бы терять до пяти минут эпизода.
            if not current and inside is not None:
                current.append((inside, float(threshold)))
            current.append((moment, mgdl))
        else:
            close(moment)
            inside = moment
    close(None)

    if not episodes:
        return []

    # Склейка соседних: дребезг вокруг порога — один эпизод, а не пять.
    merged = [episodes[0]]
    for episode in episodes[1:]:
        previous = merged[-1]
        if episode["start"] - previous["end"] <= LOW_GAP.total_seconds():
            previous["end"] = episode["end"]
            previous[peak_key] = peak(previous[peak_key], episode[peak_key])
            previous["minutes"] = max(
                1, round((previous["end"] - previous["start"]) / 60)
            )
        else:
            merged.append(episode)

    return merged
