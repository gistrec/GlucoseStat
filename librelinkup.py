"""Minimal LibreLinkUp client.

Written in place of the pylibrelinkup package, which validates TrendArrow
against an enum of 1–5 and therefore raises on ``TrendArrow: 0`` — the value
Abbott returns whenever the trend is not known yet, including the whole warm-up
window of a fresh sensor. That made the collector unable to read anything at
exactly the times it needed to.

Only what the collector needs is implemented: log in, list connections, read
the graph.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests


# Заголовки официального приложения. Без product/version Abbott отвечает 401,
# сколько бы верным ни был токен.
HEADERS = {
    "accept-encoding": "gzip",
    "cache-control": "no-cache",
    "connection": "Keep-Alive",
    "content-type": "application/json",
    "product": "llu.android",
    "version": "4.16.0",
}

TIMEOUT = 30

# Диапазон измерения сенсора Libre. Значения за его пределами — не показания,
# а служебные артефакты.
SENSOR_MIN_MGDL = 40
SENSOR_MAX_MGDL = 500


class LibreLinkUpError(Exception):
    """Any failure talking to LibreLinkUp."""


class AuthError(LibreLinkUpError):
    """Credentials rejected, or the account needs attention in the app."""


class RateLimited(LibreLinkUpError):
    """Abbott asked us to slow down."""

    def __init__(self, retry_after: int | None = None):
        self.retry_after = retry_after
        super().__init__("rate limited by LibreLinkUp")


@dataclass(frozen=True)
class Reading:
    """One glucose value, in UTC and mg/dL."""

    timestamp: datetime
    mgdl: float


def _parse_timestamp(value: str) -> datetime:
    """Parse Abbott's ``8/22/2026 12:32:02 AM`` into a naive datetime."""

    return datetime.strptime(value, "%m/%d/%Y %I:%M:%S %p")


class LibreLinkUp:
    def __init__(self, email: str, password: str, region: str = "de") -> None:
        self._email = email
        self._password = password
        self._region = region
        self._token: str | None = None
        self._account_id_hash: str | None = None
        self._patient_id: str | None = None

    @property
    def region(self) -> str:
        return self._region

    def _url(self, path: str) -> str:
        return f"https://api-{self._region}.libreview.io{path}"

    def _headers(self) -> dict:
        headers = dict(HEADERS)
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        if self._account_id_hash:
            headers["account-id"] = self._account_id_hash
        return headers

    def _request(self, method: str, path: str, **kwargs) -> dict:
        response = requests.request(
            method, self._url(path), headers=self._headers(), timeout=TIMEOUT, **kwargs
        )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            raise RateLimited(int(retry_after) if retry_after.isdigit() else None)
        if response.status_code == 401:
            raise AuthError("token rejected")

        response.raise_for_status()
        return response.json()

    def login(self) -> None:
        """Authenticate, following a regional redirect if there is one."""

        for _ in range(2):
            payload = self._request(
                "POST",
                "/llu/auth/login",
                json={"email": self._email, "password": self._password},
            )
            data = payload.get("data") or {}

            # Аккаунт живёт в конкретном регионе, и вход в чужой отвечает
            # редиректом вместо токена. Запоминаем регион и повторяем.
            if data.get("redirect"):
                self._region = str(data.get("region", self._region)).lower()
                continue

            # Abbott блокирует API, пока в приложении не приняты условия или
            # не подтверждён email. Пароль при этом верный, но ретраить нечего.
            step = (data.get("step") or {}).get("type")
            if step:
                raise AuthError(f"account needs attention in the app: {step}")

            ticket = (data.get("authTicket") or {}).get("token")
            account_id = (data.get("user") or {}).get("id")
            if not ticket or not account_id:
                raise AuthError("invalid credentials")

            self._token = ticket
            self._account_id_hash = hashlib.sha256(account_id.encode()).hexdigest()
            self._patient_id = None
            return

        raise AuthError("login kept redirecting between regions")

    def _resolve_patient(self) -> str:
        """Return the followed patient's id, fetching the list once."""

        if self._patient_id:
            return self._patient_id

        connections = self._request("GET", "/llu/connections").get("data") or []
        if not connections:
            raise LibreLinkUpError(
                "LibreLinkUp account has no connected patients — link the sensor "
                "to this account in the Libre 3 app first"
            )

        # patientId, а не id аккаунта: подписчик и пациент — разные сущности,
        # и graph принимает именно первый.
        self._patient_id = connections[0]["patientId"]
        return self._patient_id

    def readings(self) -> list[Reading]:
        """Return the graph window plus the current value, warm-up excluded."""

        patient_id = self._resolve_patient()
        data = self._request("GET", f"/llu/connections/{patient_id}/graph").get("data") or {}

        connection = data.get("connection") or {}
        entries = list(data.get("graphData") or [])

        # graphData отстаёт от сенсора на несколько минут, а свежее значение
        # лежит отдельно — без него страница всегда показывала бы прошлое.
        current = connection.get("glucoseMeasurement")
        if current:
            entries.append(current)

        valid_from = self._warmup_ends(connection)

        readings = []
        for entry in entries:
            raw_timestamp = entry.get("FactoryTimestamp")
            mgdl = entry.get("ValueInMgPerDl")
            if not raw_timestamp or mgdl is None:
                continue

            # FactoryTimestamp — единственное поле в UTC; Timestamp приходит
            # в местном времени пациента и без указания зоны.
            try:
                timestamp = _parse_timestamp(raw_timestamp)
            except ValueError:
                continue

            if not SENSOR_MIN_MGDL <= mgdl <= SENSOR_MAX_MGDL:
                continue
            if valid_from and timestamp < valid_from:
                continue

            readings.append(Reading(timestamp=timestamp, mgdl=float(mgdl)))

        return readings

    @staticmethod
    def _warmup_ends(connection: dict) -> datetime | None:
        """When the current sensor's readings start being meaningful.

        A freshly applied sensor reports for the whole warm-up window, but the
        numbers are meaningless — a just-activated sensor sits at 500 mg/dL
        with isHigh set. Storing that would poison every average on the page.
        """

        sensor = connection.get("sensor") or {}
        activated, warmup_minutes = sensor.get("a"), sensor.get("w")
        if not activated:
            return None

        started = datetime.fromtimestamp(activated, tz=timezone.utc).replace(tzinfo=None)
        return started + timedelta(minutes=warmup_minutes or 0)
