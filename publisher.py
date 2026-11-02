"""Публикатор: снимок страницы, тревога и метка свежести — по основному датчику.

Источники только пишут в базу: ``libre.py`` опрашивает LibreLinkUp,
``nightscout.py`` принимает показания по Nightscout API. Всё, что считается по
основному датчику (``glucose_sources.is_primary``, выбирается в боте), живёт
здесь и идёт раз в минуту, не дожидаясь ни одного из источников: пауза Abbott
в полчаса не прячет со страницы показания второго датчика.

Тревога — из базы. Цена решения названа прямо: пока MySQL недоступна, тревог
нет. Прежде Libre-тревога поднималась по свежему ответу Abbott и без базы, но
показания второго датчика есть только в базе, а две логики тревоги на два
процесса — два файла состояния и двойные звонки на переключении. MySQL живёт
на том же хосте, а молчание данных снаружи ловит Netdata по ``.last-reading``.

Процесс один на парк: состояние эпизода тревоги лежит в ``.alerts.json``, и
второй публикатор будил бы телефон о той же гипогликемии вторично.
"""

import logging
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from database.connection import init_schema
from database.models import LIBRE
from database.queries import last_readings, latest_fingerstick, primary_sensor
from notify import Notifier
from publish import HeavyCache, publish


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Метка свежести для Netdata. Её mtime — время самого свежего измерения в базе,
# поэтому «файл не трогали полчаса» буквально означает «полчаса нет данных», и
# следить за этим можно снаружи процесса, коллектором filecheck. Пустой файл, а
# не содержимое: filecheck читает только stat, а тревога, которой нужен разбор
# JSON, — это ещё одна программа, способная сломаться молча.
FRESHNESS_PATH = os.path.join(BASE_DIR, ".last-reading")

# Шаг цикла: живая часть снимка обновляется раз в минуту, тяжёлая — по
# ``HeavyCache``, тревога смотрит на свежайшую точку тем же шагом.
TICK = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("glucose.publisher")


def stamp_freshness(
    readings: list[tuple[datetime, float]], path: str = FRESHNESS_PATH
) -> None:
    """Point the sentinel file's mtime at the newest reading.

    Deliberately not "now": after an outage a source backfills its history,
    and a file touched on every step would report the data as fresh the moment
    the process came back, however old the readings were.
    """

    if not readings:
        return

    timestamp = readings[-1][0].replace(tzinfo=timezone.utc).timestamp()

    with open(path, "a", encoding="utf-8"):
        pass
    os.utime(path, (timestamp, timestamp))


def run_once(notifier: "Notifier | None", cache: HeavyCache | None = None) -> None:
    """Один шаг: тревога, метка свежести, снимок. Никогда не бросает.

    Каждая часть — своим try: упавшая тревога не должна стоить странице
    снимка, а упавший снимок — тревоги. Отдельной функцией ради тестов: цикл
    ``while True`` не проверить.
    """

    # Основной датчик — тот, что выбран в боте (/sensor). Не прочитали выбор —
    # Libre, как до второго датчика.
    try:
        primary = primary_sensor()
    except Exception:
        log.exception("failed to read the primary sensor")
        primary = LIBRE

    # Метка свежести считается по свежайшей строке в базе: она отвечает за
    # весь конвейер, включая запись, и при лежащей MySQL обязана устареть —
    # чтобы внешний мониторинг это заметил.
    try:
        latest = last_readings(1, sensor=primary)
    except Exception:
        log.exception("failed to read the latest reading")
        latest = []

    if notifier:
        # Сверка глюкометром — своим try и с None по умолчанию: не прочитали —
        # значит сверки нет, и тревога поднимается как раньше. Это безопасная
        # сторона отказа: молчать по причине «не смогли спросить» нельзя, а
        # лишний звонок будит.
        try:
            verified = latest_fingerstick()
        except Exception:
            log.exception("failed to read the latest fingerstick")
            verified = None

        # Старую строку notify отсеет по возрасту: тревоги по позавчерашней
        # гипогликемии не будет, даже если датчик с тех пор молчит.
        try:
            notifier.check(latest, verified=verified)
        except Exception:
            log.exception("failed to check the alert thresholds")

    try:
        stamp_freshness(latest)
    except Exception:
        log.exception("failed to stamp the freshness file")

    # last_success не передаётся: publish() прочтёт отметку LibreLinkUp из
    # базы, куда её пишет libre.py.
    try:
        publish(cache=cache)
    except Exception:
        log.exception("failed to publish the snapshot")


def main() -> None:
    load_dotenv()
    init_schema()

    notifier = Notifier.from_env(state_path=os.path.join(BASE_DIR, ".alerts.json"))
    # Тяжёлая часть снимка живёт между шагами здесь — см. HeavyCache.
    cache = HeavyCache()

    while True:
        started = time.monotonic()
        run_once(notifier, cache)
        time.sleep(max(1.0, TICK - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
