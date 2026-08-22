"""LibreLinkUp → MySQL collector.

Polls the LibreLinkUp graph endpoint, stores every new reading, and refreshes
the JSON snapshot the public page reads. Meant to run under pm2 as a
long-lived process: it never exits on a transient failure, it backs off.
"""

import logging
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from pylibrelinkup import APIUrl, PyLibreLinkUp
from pylibrelinkup.exceptions import LLUAPIRateLimitError, RedirectError

from database.connection import init_schema
from database.queries import store_readings
from publish import publish


# Полное окно ретраев — от минуты до получаса. Верхняя граница выбрана так,
# чтобы после долгой недоступности Abbott сборщик всё же догнал историю:
# LibreLinkUp отдаёт последние ~12 часов, так что пауза до 30 минут ничего
# не теряет.
BACKOFF_MIN = 60
BACKOFF_MAX = 30 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("glucose")


def _naive_utc(timestamp: datetime) -> datetime:
    """Convert an aware datetime to naive UTC for a MySQL DATETIME column."""

    if timestamp.tzinfo is None:
        return timestamp
    return timestamp.astimezone(timezone.utc).replace(tzinfo=None)


class Collector:
    """Owns the LibreLinkUp session and re-establishes it when it breaks."""

    def __init__(self, email: str, password: str, api_url: APIUrl) -> None:
        self._email = email
        self._password = password
        self._api_url = api_url
        self._client: PyLibreLinkUp | None = None
        self._patient = None

    def _connect(self) -> None:
        client = PyLibreLinkUp(
            email=self._email, password=self._password, api_url=self._api_url
        )
        try:
            client.authenticate()
        except RedirectError as error:
            # Аккаунт привязан к региону, и логин в чужом отвечает редиректом
            # вместо токена. Запоминаем регион на будущее — иначе каждый
            # повторный вход снова стоит лишнего запроса.
            log.info("LibreLinkUp redirected to region %s", error.region.name)
            self._api_url = error.region
            client = PyLibreLinkUp(
                email=self._email, password=self._password, api_url=self._api_url
            )
            client.authenticate()

        patients = client.get_patients()
        if not patients:
            raise RuntimeError(
                "LibreLinkUp account has no connected patients — link the sensor "
                "to this account in the Libre 3 app first"
            )

        self._client = client
        self._patient = patients[0]
        log.info("authenticated, tracking patient %s", self._patient.patient_id)

    def _fetch(self) -> list:
        """Read the graph window plus the current value.

        ``graph`` is the history — roughly 12 hours at a 15-minute step — but
        it lags the sensor, and it comes back empty whenever no sensor is
        active. ``latest`` carries the freshest single reading, so polling
        both is what keeps the page current between graph updates.
        """

        measurements = list(self._client.graph(patient_identifier=self._patient))
        latest = self._client.latest(patient_identifier=self._patient)
        if latest is not None:
            measurements.append(latest)
        return measurements

    def poll(self) -> int:
        """Fetch new readings and store them. Returns rows added."""

        if self._client is None:
            self._connect()

        try:
            measurements = self._fetch()
        except requests.HTTPError as error:
            # Токен живёт долго, но не вечно; 401 — единственный признак, что
            # он истёк. Один повторный вход, дальше пусть разбирается backoff.
            if error.response is None or error.response.status_code != 401:
                raise
            log.info("token rejected, re-authenticating")
            self._connect()
            measurements = self._fetch()

        readings = [
            (_naive_utc(measurement.factory_timestamp), measurement.value_in_mg_per_dl)
            for measurement in measurements
            # factory_timestamp — единственное поле в UTC: timestamp приходит
            # в локальном времени пациента и без зоны. Значение берём в mg/dL,
            # чтобы строки не зависели от настроек отображения аккаунта.
            if measurement.value_in_mg_per_dl > 0
        ]
        return store_readings(readings)


def main() -> None:
    load_dotenv()

    email = os.getenv("EMAIL")
    password = os.getenv("PASSWORD")
    if not email or not password:
        raise RuntimeError("EMAIL and PASSWORD must be set")

    fetch_interval = int(os.getenv("FETCH_INTERVAL_MINUTES", "5")) * 60

    init_schema()
    collector = Collector(email, password, APIUrl.DE)

    backoff = BACKOFF_MIN
    while True:
        try:
            added = collector.poll()
            publish()
            log.info("stored %d new readings", added)
            backoff = BACKOFF_MIN
            delay = fetch_interval
        except LLUAPIRateLimitError as error:
            delay = error.retry_after or backoff
            backoff = min(backoff * 2, BACKOFF_MAX)
            log.warning("rate limited by LibreLinkUp, sleeping %ds", delay)
        except Exception:
            # Сборщик переживает всё: сеть, MySQL, смену пароля. Падение здесь
            # означало бы бесконечный цикл рестартов pm2 с логином на каждой
            # итерации — быстрый способ получить бан от Abbott.
            delay = backoff
            backoff = min(backoff * 2, BACKOFF_MAX)
            log.exception("poll failed, retrying in %ds", delay)

        time.sleep(delay)


if __name__ == "__main__":
    main()
