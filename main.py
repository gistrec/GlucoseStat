"""LibreLinkUp → MySQL collector.

Polls the LibreLinkUp graph endpoint, stores every new reading, and refreshes
the JSON snapshot the public page reads. Meant to run under pm2 as a
long-lived process: it never exits on a transient failure, it backs off.
"""

import logging
import os
import time

from dotenv import load_dotenv

from database.connection import init_schema
from database.queries import store_readings
from librelinkup import COOLDOWN_MAX, AuthError, LibreLinkUp, RateLimited
from publish import publish


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

    def poll(self) -> int:
        """Fetch new readings and store them. Returns rows added."""

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

        return store_readings([(item.timestamp, item.mgdl) for item in readings])


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
        token_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".llu-token.json"),
    )

    backoff = BACKOFF_MIN
    last_success: float | None = None

    while True:
        try:
            added = collector.poll()
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

        # Снимок переписывается и после неудачи: только так на странице
        # появляется отметка, что сборщик молчит. Прежде publish() стоял
        # внутри try и при недоступности Abbott не вызывался вовсе — страница
        # продолжала показывать старые данные как свежие.
        try:
            publish(last_success=last_success)
        except Exception:
            log.exception("failed to publish the snapshot")

        time.sleep(delay)


if __name__ == "__main__":
    main()
