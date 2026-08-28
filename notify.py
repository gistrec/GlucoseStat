"""Pushover alerts for hypoglycaemia.

The collector sees every reading five minutes after the sensor does, so
turning one into a phone alert is a single POST. The care goes into deciding
*when*: LibreLinkUp replays the same low value on every poll, and without
state the phone would buzz twelve times an hour.

This is an addition to the alarms of the Libre app itself, not a replacement.
Nothing here fires while Abbott is unreachable, the sensor is off, or the
collector is down — in all three cases there simply is no fresh reading.
"""

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

log = logging.getLogger("glucose.notify")


PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
TIMEOUT = 15

# Тот же коэффициент, что на странице: точное значение — 18,016, но Libre и
# приложения считают по 18, а расхождение меньше шага сенсора.
MGDL_PER_MMOL = 18

# Пороги тревоги. Нижний — граница целевого диапазона (3.9 ммоль/л), по
# которой на странице считается время в диапазоне. Критический — 3.0 ммоль/л,
# гипогликемия второго уровня по консенсусу ADA/ATTD: уровень, на котором
# ждать следующего замера уже нельзя.
LOW_MGDL = 70
URGENT_MGDL = 55

# Запас на возврат: эпизод закрывается не на 70, а на 80 мг/дл. Без него сахар,
# качающийся вокруг порога, закрывал бы и открывал эпизод через замер, а каждое
# открытие — это новое уведомление.
RECOVERY_MARGIN_MGDL = 10

# Как часто напоминать, пока сахар внизу. Съеденные углеводы поднимают его за
# 15–20 минут, так что повтор через полчаса означает ровно то, что должен: не
# помогло.
REPEAT_AFTER = 30 * 60

# Насколько свежим должно быть измерение, чтобы по нему поднимать тревогу.
# Опрос идёт раз в пять минут, но в базе может лежать значение недельной
# давности — после рестарта, при снятом сенсоре или при недоступном Abbott.
MAX_AGE = 15 * 60

# Priority 1 приходит со звуком даже в тихие часы. Priority 2 Pushover
# повторяет сам, пока уведомление не подтвердят в приложении: retry — интервал
# повтора, expire — когда он сдаётся.
HIGH = 1
EMERGENCY = 2
EMERGENCY_RETRY = 120
EMERGENCY_EXPIRE = 30 * 60

# Звук задаётся явно. Без параметра Pushover играет «тон по умолчанию» — тот,
# что выбран в приложении когда-то и для чего угодно; тревога, которую можно
# не отличить от почты, тревогой не работает. Два разных звука ещё и отделяют
# критическую гипогликемию от низкого сахара на слух, до того как достанут
# телефон. Обойти беззвучный режим ни один из них не может: у Pushover нет
# entitlement на Apple Critical Alerts, и переключатель на боку iPhone глушит
# в том числе priority 2.
LOW_SOUND = "falling"
URGENT_SOUND = "siren"

LOW = "low"
URGENT = "urgent"
SEVERITY = {LOW: 1, URGENT: 2}


@dataclass(frozen=True)
class Alert:
    """One message to send: what happened and how loudly to say it."""

    level: str
    title: str
    message: str
    priority: int
    sound: str


def _level(mgdl: float) -> str | None:
    """Which threshold a reading breaks, if any."""

    if mgdl < URGENT_MGDL:
        return URGENT
    if mgdl < LOW_MGDL:
        return LOW
    return None


def _mmol(mgdl: float) -> str:
    """Format a reading the way the page does: mmol/L, decimal comma."""

    return f"{mgdl / MGDL_PER_MMOL:.1f}".replace(".", ",")


def _minutes(seconds: float) -> str:
    """Russian plural for a duration in whole minutes."""

    value = int(seconds // 60)
    if 11 <= value % 100 <= 14:
        word = "минут"
    else:
        word = {1: "минуту", 2: "минуты", 3: "минуты", 4: "минуты"}.get(
            value % 10, "минут"
        )
    return f"{value} {word}"


def _alert(level: str, mgdl: float, duration: float) -> Alert:
    urgent = level == URGENT

    message = f"{_mmol(mgdl)} ммоль/л"
    if duration >= 60:
        message += f", низкий уже {_minutes(duration)}"

    return Alert(
        level=level,
        title="Критически низкий сахар" if urgent else "Низкий сахар",
        message=message,
        priority=EMERGENCY if urgent else HIGH,
        sound=URGENT_SOUND if urgent else LOW_SOUND,
    )


def decide(
    mgdl: float, age: float, state: dict, now: float
) -> tuple[Alert | None, dict]:
    """Decide what to send for a reading, and what to remember afterwards.

    ``state`` describes the current episode: its worst level so far, when it
    started and when the last message went out. An empty dict means there is
    no episode. The returned state is what to store once the alert is sent.
    """

    if age > MAX_AGE:
        # По старому значению тревогу не поднимают: оно ничего не говорит о
        # том, что происходит с сахаром сейчас. Эпизод при этом не закрывается —
        # сенсор мог отвалиться посреди гипогликемии.
        return None, state

    level = _level(mgdl)
    active = state.get("level")

    if level is None:
        if active and mgdl < LOW_MGDL + RECOVERY_MARGIN_MGDL:
            return None, state
        return None, {}

    since = state.get("since", now)

    if not active:
        send = True
    elif SEVERITY[level] > SEVERITY[active]:
        # Эпизод углубился до критического — это новость, ждать повтора нельзя.
        send = True
    else:
        send = now - state.get("sent_at", 0) >= REPEAT_AFTER

    if not send:
        return None, state

    # В состоянии остаётся худший уровень эпизода: спад с критического до
    # просто низкого не должен «перезаряжать» тревогу, иначе шум сенсора у
    # 55 мг/дл поднимал бы экстренное уведомление через замер. Само оно при
    # этом никуда не денется — priority 2 повторяется до подтверждения.
    worst = active if active and SEVERITY[active] >= SEVERITY[level] else level

    return _alert(level, mgdl, now - since), {
        "level": worst,
        "since": since,
        "sent_at": now,
    }


class Notifier:
    """Sends Pushover alerts and remembers what it has already sent.

    The state lives in a file rather than in memory: pm2 restarts the
    collector on every failure, and a forgotten episode means the phone buzzes
    again about a low it already reported.
    """

    def __init__(self, token: str, user: str, state_path: str | None = None) -> None:
        self._token = token
        self._user = user
        self._state_path = state_path

    @classmethod
    def from_env(cls, state_path: str | None = None) -> "Notifier | None":
        """Build a notifier from the environment, or None if it is not set up."""

        token = os.getenv("PUSHOVER_TOKEN")
        user = os.getenv("PUSHOVER_USER")

        if not token and not user:
            log.info("Pushover is not configured, alerts are off")
            return None

        if not token or not user:
            # Не исключение: сбор данных важнее уведомлений и не должен
            # останавливаться из-за половины заполненной настройки.
            log.error(
                "PUSHOVER_TOKEN and PUSHOVER_USER must both be set, alerts are off"
            )
            return None

        return cls(token, user, state_path=state_path)

    def check(self, readings: list[tuple[datetime, float]]) -> None:
        """Alert on the newest reading, if it warrants one."""

        if not readings:
            return

        timestamp, mgdl = readings[-1]
        reading_at = timestamp.replace(tzinfo=timezone.utc).timestamp()
        now = time.time()

        state = self._load()
        alert, updated = decide(mgdl, now - reading_at, state, now)

        if alert and not self._send(alert, reading_at):
            # Состояние не трогаем: недоставленное уведомление должно уйти на
            # следующем опросе, а не остаться записанным как отправленное.
            return

        if updated != state:
            self._save(updated)

    def _send(self, alert: Alert, timestamp: float) -> bool:
        payload = {
            "token": self._token,
            "user": self._user,
            "title": alert.title,
            "message": alert.message,
            "priority": alert.priority,
            "sound": alert.sound,
            # Время замера, а не отправки: Pushover покажет его в часовом поясе
            # телефона, а сам процесс живёт в UTC.
            "timestamp": int(timestamp),
        }

        if alert.priority == EMERGENCY:
            payload["retry"] = EMERGENCY_RETRY
            payload["expire"] = EMERGENCY_EXPIRE

        try:
            response = requests.post(PUSHOVER_URL, data=payload, timeout=TIMEOUT)
            response.raise_for_status()
        except requests.RequestException:
            log.exception("Pushover did not accept the alert")
            return False

        log.info("alerted: %s, %s", alert.title, alert.message)
        return True

    def _load(self) -> dict:
        if not self._state_path or not os.path.isfile(self._state_path):
            return {}

        try:
            with open(self._state_path, encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            # Испорченный файл — не повод молчать: пустое состояние означает
            # «эпизода нет», то есть следующий низкий замер поднимет тревогу.
            return {}

        return state if isinstance(state, dict) else {}

    def _save(self, state: dict) -> None:
        if not self._state_path:
            return

        directory = os.path.dirname(os.path.abspath(self._state_path))
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
        ) as handle:
            json.dump(state, handle)
            temp_path = handle.name

        os.replace(temp_path, self._state_path)
