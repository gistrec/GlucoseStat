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
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime

import requests

log = logging.getLogger("glucose.llu")


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

# Потолок ожидания после 476. Abbott присылает Retry-After почти на сутки, но
# на слово ему не верим: блокировка нередко снимается раньше. Десять минут —
# компромисс: серия быстрых рестартов всё ещё не превращается в серию логинов,
# а простой после снятия бана втрое короче получаса.
COOLDOWN_MAX = 10 * 60


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
    def __init__(
        self,
        email: str,
        password: str,
        region: str = "de",
        token_path: str | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._region = region
        self._token: str | None = None
        self._account_id_hash: str | None = None
        self._patient_id: str | None = None
        self._token_path = token_path

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

        # 476 — недокументированный код Abbott: приходит на /llu/auth/login
        # после серии входов подряд и держится десятки минут. Обращаться с ним
        # как с 429 — единственный способ не усугублять: повторный логин по
        # такому ответу только продлевает блокировку.
        if response.status_code in (429, 476):
            retry_after = response.headers.get("Retry-After", "")
            raise RateLimited(int(retry_after) if retry_after.isdigit() else None)
        if response.status_code == 401:
            raise AuthError("token rejected")

        response.raise_for_status()
        return response.json()

    @property
    def _cooldown_path(self) -> str | None:
        return f"{self._token_path}.cooldown" if self._token_path else None

    def _cooldown_remaining(self) -> int:
        """Seconds left of a rate-limit ban, 0 if none is in effect."""

        path = self._cooldown_path
        if not path or not os.path.isfile(path):
            return 0

        try:
            with open(path, encoding="utf-8") as handle:
                until = float(handle.read().strip())
        except (OSError, ValueError):
            return 0

        return max(0, int(until - time.time()))

    def _start_cooldown(self, seconds: int) -> None:
        """Remember a ban across restarts.

        Abbott's 476 comes with a Retry-After measured in hours. Kept only in
        memory, a pm2 restart would wipe it and log in early — which restarts
        the ban instead of waiting it out.
        """

        path = self._cooldown_path
        if not path or seconds <= 0:
            return

        seconds = min(seconds, COOLDOWN_MAX)

        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write(str(time.time() + seconds))
        os.replace(temp_path, path)

    def _clear_cooldown(self) -> None:
        path = self._cooldown_path
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    def _load_session(self) -> bool:
        """Restore a cached session. Returns True if one was usable."""

        if not self._token_path or not os.path.isfile(self._token_path):
            return False

        try:
            with open(self._token_path, encoding="utf-8") as handle:
                cached = json.load(handle)
        except (OSError, ValueError):
            return False

        # Минута форы: токен, истекающий на лету, стоил бы лишнего запроса и
        # всё равно привёл бы к логину.
        if cached.get("expires", 0) <= time.time() + 60:
            return False

        self._token = cached.get("token")
        self._account_id_hash = cached.get("account_id_hash")
        self._region = cached.get("region", self._region)
        return bool(self._token and self._account_id_hash)

    def _save_session(self, expires: int) -> None:
        if not self._token_path:
            return

        payload = {
            "token": self._token,
            "account_id_hash": self._account_id_hash,
            "region": self._region,
            "expires": expires,
        }

        # Токен — это доступ к аккаунту, файл создаётся сразу с правами 600.
        temp_path = f"{self._token_path}.tmp"
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(temp_path, self._token_path)

    def login(self, force: bool = False) -> None:
        """Authenticate, following a regional redirect if there is one.

        Reuses the cached session unless ``force`` is set. Abbott answers a
        burst of logins with HTTP 476 and keeps refusing for a while, so a
        restart must not cost a fresh login — the token is good for months.
        """

        if not force and self._load_session():
            log.info("reusing cached session, region %s", self._region)
            return

        remaining = self._cooldown_remaining()
        if remaining:
            # Отличать этот отказ от настоящего важно: он означает «ждём сами»,
            # а не «Abbott всё ещё блокирует». По одинаковой записи в логе эти
            # состояния не различить, а выводы из них разные.
            log.info("login held back locally for %ds (cached ban)", remaining)
            raise RateLimited(remaining)

        for _ in range(2):
            try:
                payload = self._request(
                    "POST",
                    "/llu/auth/login",
                    json={"email": self._email, "password": self._password},
                )
            except RateLimited as error:
                log.warning(
                    "LibreLinkUp refused the login (Retry-After %ss)",
                    error.retry_after if error.retry_after is not None else "absent",
                )
                self._start_cooldown(error.retry_after or 3600)
                raise
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

            ticket = data.get("authTicket") or {}
            token = ticket.get("token")
            account_id = (data.get("user") or {}).get("id")
            if not token or not account_id:
                raise AuthError("invalid credentials")

            self._token = token
            self._account_id_hash = hashlib.sha256(account_id.encode()).hexdigest()
            self._patient_id = None
            self._save_session(int(ticket.get("expires", 0)))
            self._clear_cooldown()
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

            # Единственный отсев — за пределами шкалы сенсора: там уже не
            # показания, а служебные значения. Данные прогрева сохраняются
            # как есть, хотя свежий сенсор час держится на потолке шкалы.
            if not SENSOR_MIN_MGDL <= mgdl <= SENSOR_MAX_MGDL:
                continue

            readings.append(Reading(timestamp=timestamp, mgdl=float(mgdl)))

        return readings
