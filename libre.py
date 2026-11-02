"""LibreLinkUp → MySQL: источник показаний Libre.

Опрашивает graph LibreLinkUp и пишет новые показания в ``glucose_readings``
с ``sensor='libre'``, а с ними — отметку удачного опроса и срок сенсора.
Снимок страницы, тревогу и метку свежести ведёт ``publisher.py``: источник
только пишет в базу, как и ``nightscout.py``. Живёт под pm2 и не выходит на
временном сбое — ждёт с нарастающей паузой.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from database.connection import init_schema
from database.models import LIBRE
from database.queries import (
    ensure_libre_source,
    mark_received,
    store_last_success,
    store_readings,
    store_sensor,
    store_sensor_end,
    store_sensor_start,
)
from librelinkup import COOLDOWN_MAX, AuthError, LibreLinkUp, RateLimited, Sensor


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
log = logging.getLogger("glucose.libre")


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
        """Fetch the current readings window, oldest first. Хранение — забота
        вызывающего."""

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

        # Сортировка своя: порядок graphData — привычка Abbott, а не контракт.
        return sorted((item.timestamp, item.mgdl) for item in readings)

    @property
    def sensor(self) -> Sensor | None:
        """Сенсор из последнего ответа, если Abbott назвал дату установки."""

        return self._client.sensor


def run_once(
    collector: Collector,
    fetch_interval: int,
    backoff: int,
    last_success: float | None,
) -> tuple[int, int, float | None]:
    """Один опрос: скачать, сохранить, отметить.

    Возвращает (пауза до следующего опроса, следующий backoff, last_success).
    Никогда не бросает: источник переживает всё — сеть, MySQL, смену пароля.
    Отдельной функцией ради тестов: цикл ``while True`` не проверить.
    """

    try:
        fetched = collector.poll()
        received = datetime.now(timezone.utc).replace(tzinfo=None)
        added = store_readings(fetched, sensor=LIBRE, received_at=received)
        last_success = time.time()
        log.info("stored %d new readings", added)
        backoff = BACKOFF_MIN
        delay = fetch_interval
        # Отметка приёма для футера — своим try: без неё теряется подпись,
        # а не показания и не сброс паузы.
        try:
            mark_received(LIBRE, received)
        except Exception:
            log.exception("failed to mark the libre source")
    except RateLimited as error:
        # Тот же потолок, что у файла блокировки: разойдись они — цикл
        # спал бы дольше, чем действует запрет, и попытка не состоялась
        # бы в срок. Retry-After у Abbott доходит до суток, но на слово
        # мы ему не верим.
        delay = min(error.retry_after or RATE_LIMIT_MIN, COOLDOWN_MAX)
        log.warning("rate limited by LibreLinkUp, sleeping %ds", delay)
    except Exception:
        # Падение здесь означало бы бесконечный цикл рестартов pm2 с логином
        # на каждой итерации — быстрый способ получить бан от Abbott.
        delay = backoff
        backoff = min(backoff * 2, BACKOFF_MAX)
        log.exception("poll failed, retrying in %ds", delay)

    # Дата установки сенсора — своим try и после показаний: страница без неё
    # теряет одну карточку, а показания важнее. Пишется на каждом удачном
    # опросе, как и отметка успеха: отслеживать «менялось ли» в памяти процесса
    # значило бы потерять смену сенсора, случившуюся при перезапуске.
    sensor = collector.sensor
    if sensor is not None:
        ends = sensor.started + timedelta(days=sensor.lifetime_days)
        try:
            store_sensor_start(sensor.started)
            # Конец срока считает источник: модель прибора видна только ему,
            # а публикатору достаётся готовая дата.
            store_sensor_end(ends)
        except Exception:
            log.exception("failed to store the sensor start")
        # История — своим try: без неё пропадёт лишь линия замены на
        # графике, и сбой не должен стоить карточки текущего сенсора.
        try:
            store_sensor(sensor.started, ends, sensor.kind)
        except Exception:
            log.exception("failed to store the sensor history")

    # Своим try, а не внутри опроса: упавшая запись не должна выглядеть
    # неудачным опросом и включать backoff. По этой отметке публикатор
    # говорит на странице «нет связи с LibreLinkUp».
    if last_success is not None:
        try:
            store_last_success(
                datetime.fromtimestamp(last_success, timezone.utc).replace(tzinfo=None)
            )
        except Exception:
            log.exception("failed to store the last-success mark")

    return delay, backoff, last_success


def main() -> None:
    load_dotenv()

    email = os.getenv("EMAIL")
    password = os.getenv("PASSWORD")
    if not email or not password:
        raise RuntimeError("EMAIL and PASSWORD must be set")

    fetch_interval = int(os.getenv("FETCH_INTERVAL_MINUTES", "5")) * 60

    init_schema()
    try:
        ensure_libre_source(datetime.now(timezone.utc).replace(tzinfo=None))
    except Exception:
        log.exception("failed to register the libre source")
    collector = Collector(
        email,
        password,
        region=os.getenv("LLU_REGION", "de"),
        token_path=os.path.join(BASE_DIR, ".llu-token.json"),
    )

    backoff = BACKOFF_MIN
    last_success: float | None = None

    while True:
        delay, backoff, last_success = run_once(collector, fetch_interval, backoff, last_success)
        time.sleep(delay)


if __name__ == "__main__":
    main()
