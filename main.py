"""LibreLinkUp → MySQL collector.

Polls the LibreLinkUp graph endpoint, stores every new reading, and refreshes
the JSON snapshot the public page reads. Meant to run under pm2 as a
long-lived process: it never exits on a transient failure, it backs off.
"""

import logging
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from database.connection import init_schema
from database.queries import last_readings, store_readings
from librelinkup import COOLDOWN_MAX, AuthError, LibreLinkUp, RateLimited
from notify import Notifier
from publish import publish


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Метка свежести для Netdata. Её mtime — время самого свежего измерения в базе,
# поэтому «файл не трогали полчаса» буквально означает «полчаса нет данных», и
# следить за этим можно снаружи процесса, коллектором filecheck. Пустой файл, а
# не содержимое: filecheck читает только stat, а тревога, которой нужен разбор
# JSON, — это ещё одна программа, способная сломаться молча.
FRESHNESS_PATH = os.path.join(BASE_DIR, ".last-reading")

# Полное окно ретраев — от минуты до получаса. Верхняя граница выбрана так,
# чтобы после долгой недоступности Abbott сборщик всё же догнал историю:
# LibreLinkUp отдаёт последние ~12 часов, так что пауза до 30 минут ничего
# не теряет.
BACKOFF_MIN = 60
BACKOFF_MAX = 30 * 60

# Пауза после отказа «слишком часто», когда сервер не сказал, сколько ждать.
# Минута, как при обычном сбое, тут не годится: такие отказы держатся
# десятками минут, и частые попытки только продлевают блокировку.
RATE_LIMIT_MIN = 5 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("glucose")


class Collector:
    """Owns the LibreLinkUp session and re-establishes it when it breaks."""

    def __init__(
        self,
        email: str,
        password: str,
        region: str = "de",
        token_path: str | None = None,
    ) -> None:
        self._client = LibreLinkUp(
            email, password, region=region, token_path=token_path
        )
        self._logged_in = False

    def _login(self, force: bool = False) -> None:
        self._client.login(force=force)
        self._logged_in = True

    def poll(self) -> list[tuple[datetime, float]]:
        """Fetch the current readings window, oldest first.

        Хранение — забота вызывающего: скачанные показания нужны и тогда,
        когда записать их некуда, — по ним поднимается тревога.
        """

        if not self._logged_in:
            self._login()

        try:
            readings = self._client.readings()
        except AuthError:
            # Токен живёт долго, но не вечно. Только здесь логин делается
            # принудительно — кешированная сессия уже доказала, что мертва.
            log.info("token rejected, re-authenticating")
            self._logged_in = False
            self._login(force=True)
            readings = self._client.readings()

        # Сортировка своя: порядок graphData — привычка Abbott, а не контракт,
        # а последним элементом обязан быть свежайший замер — по нему
        # поднимается тревога.
        return sorted((item.timestamp, item.mgdl) for item in readings)


def stamp_freshness(
    readings: list[tuple[datetime, float]], path: str = FRESHNESS_PATH
) -> None:
    """Point the sentinel file's mtime at the newest reading.

    Deliberately not "now": after an outage the collector backfills the whole
    graph window, and a file touched on every poll would report the data as
    fresh the moment the process came back, however old the readings were.
    """

    if not readings:
        return

    timestamp = readings[-1][0].replace(tzinfo=timezone.utc).timestamp()

    with open(path, "a", encoding="utf-8"):
        pass
    os.utime(path, (timestamp, timestamp))


def run_once(
    collector: Collector,
    notifier: "Notifier | None",
    fetch_interval: int,
    backoff: int,
    last_success: float | None,
) -> tuple[int, int, float | None]:
    """Один цикл опроса: скачать, сохранить, оповестить, отметить, опубликовать.

    Возвращает (пауза до следующего цикла, следующий backoff, last_success).
    Никогда не бросает: сборщик переживает всё — сеть, MySQL, смену пароля.
    Отдельной функцией ради тестов: цикл ``while True`` не проверить, а
    порядок «тревога не зависит от базы» — ровно то, что нельзя сломать молча.
    """

    # Показания этого опроса, пока они в руках: если скачать удалось, а
    # записать — нет, тревога всё равно должна подняться.
    fetched: list[tuple[datetime, float]] = []

    try:
        fetched = collector.poll()
        added = store_readings(fetched)
        last_success = time.time()
        log.info("stored %d new readings", added)
        backoff = BACKOFF_MIN
        delay = fetch_interval
    except RateLimited as error:
        # Тот же потолок, что у файла блокировки: разойдись они — цикл
        # спал бы дольше, чем действует запрет, и попытка не состоялась
        # бы в срок. Retry-After у Abbott доходит до суток, но на слово
        # мы ему не верим.
        delay = min(error.retry_after or RATE_LIMIT_MIN, COOLDOWN_MAX)
        log.warning("rate limited by LibreLinkUp, sleeping %ds", delay)
    except Exception:
        # Сборщик переживает всё: сеть, MySQL, смену пароля. Падение здесь
        # означало бы бесконечный цикл рестартов pm2 с логином на каждой
        # итерации — быстрый способ получить бан от Abbott.
        delay = backoff
        backoff = min(backoff * 2, BACKOFF_MAX)
        log.exception("poll failed, retrying in %ds", delay)

    # Метка свежести считается по свежайшей строке в базе, а не по ответу
    # Abbott: она отвечает за весь конвейер, включая запись, и при лежащей
    # MySQL обязана устареть — чтобы внешний мониторинг это заметил.
    try:
        latest = last_readings(1)
    except Exception:
        log.exception("failed to read the latest reading")
        latest = []

    if notifier:
        # Тревога — по показаниям, только что скачанным у Abbott: гипогликемия,
        # которая уже в руках, не должна молчать из-за недоступной MySQL. Когда
        # опрос не удался или graph пуст, остаётся свежайшая строка базы — её
        # notify отсеет по возрасту, вместо того чтобы поднять тревогу по
        # позавчерашней гипогликемии.
        try:
            notifier.check(fetched or latest)
        except Exception:
            log.exception("failed to check the alert thresholds")

    try:
        stamp_freshness(latest)
    except Exception:
        log.exception("failed to stamp the freshness file")

    # Снимок переписывается и после неудачи: только так на странице
    # появляется отметка, что сборщик молчит. Прежде publish() стоял
    # внутри try и при недоступности Abbott не вызывался вовсе — страница
    # продолжала показывать старые данные как свежие.
    try:
        publish(last_success=last_success)
    except Exception:
        log.exception("failed to publish the snapshot")

    return delay, backoff, last_success


def main() -> None:
    load_dotenv()

    email = os.getenv("EMAIL")
    password = os.getenv("PASSWORD")
    if not email or not password:
        raise RuntimeError("EMAIL and PASSWORD must be set")

    fetch_interval = int(os.getenv("FETCH_INTERVAL_MINUTES", "5")) * 60

    init_schema()
    collector = Collector(
        email,
        password,
        region=os.getenv("LLU_REGION", "de"),
        token_path=os.path.join(BASE_DIR, ".llu-token.json"),
    )
    notifier = Notifier.from_env(state_path=os.path.join(BASE_DIR, ".alerts.json"))

    backoff = BACKOFF_MIN
    last_success: float | None = None

    while True:
        delay, backoff, last_success = run_once(
            collector, notifier, fetch_interval, backoff, last_success
        )
        time.sleep(delay)


if __name__ == "__main__":
    main()
