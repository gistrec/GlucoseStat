"""Эпизоды ниже нормы: когда, как долго, как глубоко.

Отдельным модулем, а не функцией внутри ``publish``, потому что считают их
двое: сам снимок — чтобы нарисовать полосу под кривой, — и ночная сводка,
которой нужны минуты внутри каждой ночи. Держать эту арифметику у одного из
них значило бы либо завести круг из импортов, либо посчитать эпизоды дважды
разными способами, и однажды разойтись в ответе на «сколько их было».

Считается по сырым замерам. Прореженная кривая на такие вопросы не отвечает:
два провала в соседних корзинах выглядят на ней одним, а минута на недельной
панели занимает четверть пикселя.
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

    Длительность — от первого замера ниже порога до первого замера выше него,
    а не до последнего низкого: сенсор отдаёт точку раз в минуту-пять, и без
    правого края одиночный провал получал бы нулевую длину.
    """

    episodes: list[dict] = []
    current: list[tuple[datetime, float]] = []
    left: datetime | None = None

    def close(right: datetime | None) -> None:
        if not current:
            return
        start, finish = current[0][0], right or current[-1][0]
        episodes.append(
            {
                "start": int(start.replace(tzinfo=timezone.utc).timestamp()),
                "end": int(finish.replace(tzinfo=timezone.utc).timestamp()),
                "min": round(min(mgdl for _, mgdl in current)),
                "minutes": max(1, round((finish - start).total_seconds() / 60)),
            }
        )
        current.clear()

    for moment, mgdl in readings:
        if mgdl < low:
            # Левый край — предыдущий замер над порогом: пересечение случилось
            # между ними, и относить его целиком к первому низкому замеру
            # значило бы терять до пяти минут эпизода.
            if not current and left is not None:
                current.append((left, float(low)))
            current.append((moment, mgdl))
        else:
            close(moment)
            left = moment
    close(None)

    if not episodes:
        return []

    # Склейка соседних: дребезг вокруг порога — один эпизод, а не пять.
    merged = [episodes[0]]
    for episode in episodes[1:]:
        previous = merged[-1]
        if episode["start"] - previous["end"] <= LOW_GAP.total_seconds():
            previous["end"] = episode["end"]
            previous["min"] = min(previous["min"], episode["min"])
            previous["minutes"] = max(
                1, round((previous["end"] - previous["start"]) / 60)
            )
        else:
            merged.append(episode)

    return merged
