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

# Верхние — зеркально. 180 мг/дл (10,0 ммоль/л) — это TARGET_HIGH_MGDL из
# publish.py, та самая граница, выше которой страница красит кривую жёлтым и
# считает время выше цели: тревога обязана срабатывать ровно там, где страница
# уже говорит «выше». 288 (16,0 ммоль/л) — уровень, на котором пора смотреть
# кетоны, а не ждать следующего замера.
#
# Сравнение строгое, как и у нижних: ровно 10,0 — это ещё не «выше», ровно
# 3,9 — ещё не «ниже».
HIGH_MGDL = 180
URGENT_HIGH_MGDL = 288

# Запас на возврат: эпизод закрывается не на 70, а на 80 мг/дл. Без него сахар,
# качающийся вокруг порога, закрывал бы и открывал эпизод через замер, а каждое
# открытие — это новое уведомление. Тот же запас работает и сверху, только в
# другую сторону: верхний эпизод закрывается на 170.
RECOVERY_MARGIN_MGDL = 10

# Как часто напоминать, пока сахар внизу. Съеденные углеводы поднимают его за
# 15–20 минут, так что повтор через полчаса означает ровно то, что должен: не
# помогло.
#
# Сверху интервал тот же, хотя подколка действует дольше еды. Это сознательный
# компромисс: свой, более редкий повтор для верхних эпизодов — второе число про
# «не помогло», и разъехаться им было бы нечем, кроме случая. Если верхние
# повторы окажутся назойливыми, разделять надо будет именно здесь.
REPEAT_AFTER = 30 * 60

# Насколько свежим должно быть измерение, чтобы по нему поднимать тревогу.
# Опрос идёт раз в пять минут, но в базе может лежать значение недельной
# давности — после рестарта, при снятом сенсоре или при недоступном Abbott.
MAX_AGE = 15 * 60

# Насколько свежей должна быть сверка глюкометром, чтобы снять тревогу. Своя
# константа, а не MAX_AGE: то правило про сенсор, по которому тревогу поднимают,
# это — про кровь, которой её гасят, и меняться они могут независимо. Пятнадцать
# минут потому, что столько живёт сам ответ: за это время сахар успевает уйти на
# пару ммоль, и сверка перестаёт говорить о том, что происходит сейчас.
VERIFY_FRESH_FOR = 15 * 60

# Какая сверка снимает тревогу: от границы возврата, а не от самого порога.
# Глюкометр на 3,9 не опровергает гипогликемию, а подтверждает её у самой
# черты, и молчать по такому замеру нельзя. Та же граница, по которой
# закрывается эпизод при возврате сахара, — 4,4 ммоль/л: два числа для
# «уже не низко» разъехались бы при первой правке одного из них.
#
# Сверху — зеркально, 9,4 ммоль/л. Обе границы выведены из порогов и запаса, а
# не выписаны числами: правка порога тянет их за собой сама.
VERIFY_CLEAR_MGDL = LOW_MGDL + RECOVERY_MARGIN_MGDL
VERIFY_CLEAR_HIGH_MGDL = HIGH_MGDL - RECOVERY_MARGIN_MGDL

# Priority 1 приходит со звуком даже в тихие часы. Priority 2 Pushover
# повторяет сам, пока уведомление не подтвердят в приложении: retry — интервал
# повтора, expire — когда он сдаётся.
#
# С приставкой PRIORITY_, а не просто HIGH: уровней сахара стало четыре, и
# среди них есть «высокий». HIGH, означающий приоритет 1, стоял бы в одном
# файле с HIGH, означающим гипергликемию, — и однажды одно подставили бы
# вместо другого, получив тревогу без звука или уровень-единицу.
PRIORITY_HIGH = 1
PRIORITY_EMERGENCY = 2
EMERGENCY_RETRY = 120
EMERGENCY_EXPIRE = 30 * 60

# Звук задаётся явно. Без параметра Pushover играет «тон по умолчанию» — тот,
# что выбран в приложении когда-то и для чего угодно; тревога, которую можно
# не отличить от почты, тревогой не работает. Два разных звука ещё и отделяют
# критическую гипогликемию от низкого сахара на слух, до того как достанут
# телефон. Пробьют ли они беззвучный режим, решает не этот код, а телефон:
# entitlement на Apple Critical Alerts у Pushover есть (выдан в феврале 2020),
# но обход беззвучного в iOS-приложении включается отдельным переключателем
# на каждый приоритет. Проверено живыми тестами 14.09.2026: с включёнными
# Critical Alerts для High и Emergency сквозь беззвучный звонят оба.
#
# Четыре уровня — четыре звука, и верхние выбраны не по вкусу: «climb» — прямой
# ответ «falling» из списка Pushover, а «spacealarm» отличается от «siren»
# достаточно, чтобы спросонья не перепутать, в какую сторону ушёл сахар. Список
# у Pushover закрытый, и выдуманное имя вроде «rising» он отвергает — то есть
# уведомление не ушло бы вовсе. Проверено по pushover.net/api#sounds.
LOW_SOUND = "falling"
URGENT_SOUND = "siren"
HIGH_SOUND = "climb"
URGENT_HIGH_SOUND = "spacealarm"

LOW = "low"
URGENT = "urgent"
HIGH = "high"
URGENT_HIGH = "urgent_high"

#: Насколько громко. Внутри одного направления уровень может углубиться, и
#: тогда тревога не ждёт повтора; между направлениями сравнивать нечего.
SEVERITY = {LOW: 1, URGENT: 2, HIGH: 1, URGENT_HIGH: 2}

#: Куда ушёл сахар. Эпизоды разных направлений не продолжают друг друга: выйдя
#: из гипогликемии в гипергликемию, человек пережил два разных события, а не
#: одно углубившееся, — и второе обязано заговорить сразу, а не досиживать
#: получасовую паузу от первого.
DOWN, UP = "down", "up"
DIRECTION = {LOW: DOWN, URGENT: DOWN, HIGH: UP, URGENT_HIGH: UP}

#: Заголовок уведомления и то же слово в строке длительности: «низкий уже
#: 20 минут». Вместе, а не двумя словарями, — одна строка на уровень, и
#: забыть половину при добавлении пятого не выйдет.
TITLES = {
    LOW: ("Низкий сахар", "низкий"),
    URGENT: ("Критически низкий сахар", "низкий"),
    HIGH: ("Высокий сахар", "высокий"),
    URGENT_HIGH: ("Критически высокий сахар", "высокий"),
}

SOUNDS = {
    LOW: LOW_SOUND,
    URGENT: URGENT_SOUND,
    HIGH: HIGH_SOUND,
    URGENT_HIGH: URGENT_HIGH_SOUND,
}

#: Какие уровни требуют экстренного приоритета — те, на которых ждать
#: следующего замера уже нельзя, по обе стороны.
EMERGENCY_LEVELS = frozenset({URGENT, URGENT_HIGH})


@dataclass(frozen=True)
class Alert:
    """One message to send: what happened and how loudly to say it."""

    level: str
    title: str
    message: str
    priority: int
    sound: str


def _level(mgdl: float) -> str | None:
    """Which threshold a reading breaks, if any.

    Порядок проверок — от края к середине с обеих сторон: критический уровень
    удовлетворяет и обычному порогу, и спрошенный вторым он никогда бы не
    вернулся.
    """

    if mgdl < URGENT_MGDL:
        return URGENT
    if mgdl < LOW_MGDL:
        return LOW
    if mgdl > URGENT_HIGH_MGDL:
        return URGENT_HIGH
    if mgdl > HIGH_MGDL:
        return HIGH
    return None


def _recovered(mgdl: float, direction: str) -> bool:
    """Вернулся ли сахар настолько, чтобы закрыть эпизод этого направления.

    С запасом и всегда внутрь диапазона: снизу нужно подняться выше 80, сверху
    — опуститься ниже 170. Без запаса сахар, качающийся у самого порога,
    закрывал бы и открывал эпизод через замер, и каждое открытие звонило бы.
    """

    if direction == DOWN:
        return mgdl >= LOW_MGDL + RECOVERY_MARGIN_MGDL
    return mgdl <= HIGH_MGDL - RECOVERY_MARGIN_MGDL


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
    title, word = TITLES[level]

    message = f"{_mmol(mgdl)} ммоль/л"
    if duration >= 60:
        message += f", {word} уже {_minutes(duration)}"

    return Alert(
        level=level,
        title=title,
        message=message,
        priority=(
            PRIORITY_EMERGENCY if level in EMERGENCY_LEVELS else PRIORITY_HIGH
        ),
        sound=SOUNDS[level],
    )


def cleared_by_blood(
    verified: tuple[float, float] | None, now: float, direction: str
) -> bool:
    """Снимает ли сверка глюкометром тревогу по сенсору.

    Сенсор меряет межклеточную жидкость и отстаёт от крови, а если на него
    лечь во сне — рисует провал, которого не было вовсе: компрессионная
    гипогликемия, и по ночам это самая частая ложная тревога. Капля крови в
    норме означает, что провала нет, чем бы его ни объяснял сенсор.

    ``direction`` обязателен, а не выведен из самой сверки: 16,6 ммоль/л по
    крови «не низко», и без направления такая капля сняла бы тревогу о высоком
    сахаре, который сама же подтверждает. Проверяется поэтому не «в норме
    вообще», а возврат с той стороны, с которой звонит тревога, — тем же
    ``_recovered`` и с тем же запасом, каким эпизод закрывается по сенсору.

    Свежесть и порог проверяются здесь, а не в ``decide``, ровно потому, что
    это единственное правило, по которому тревога снимается не показанием
    прибора, а внешним фактом: собранное в одном предикате, оно читается
    целиком и проверяется тестом на трёх числах, а не на сценарии.
    """

    if verified is None:
        return False

    measured_at, mgdl = verified
    return now - measured_at <= VERIFY_FRESH_FOR and _recovered(mgdl, direction)


def decide(
    mgdl: float,
    age: float,
    state: dict,
    now: float,
    verified: tuple[float, float] | None = None,
) -> tuple[Alert | None, dict]:
    """Decide what to send for a reading, and what to remember afterwards.

    ``state`` describes the current episode: its worst level so far, when it
    started and when the last message went out. An empty dict means there is
    no episode. The returned state is what to store once the alert is sent.

    ``verified`` — последняя сверка глюкометром как ``(момент, мг/дл)``, или
    ``None``, если её нет или база недоступна. ``None`` — безопасная сторона:
    без сверки тревога поднимается, как поднималась всегда.
    """

    level = _level(mgdl)
    active = state.get("level")

    # Сверять кровь имеет смысл с той стороной, о которой идёт речь: сначала
    # берём направление текущего показания, а если оно в норме — направление
    # открытого эпизода, который ещё не закрылся.
    direction = DIRECTION.get(level) or DIRECTION.get(active)

    # Первым делом, до проверки возраста: свежая капля крови, вернувшаяся с той
    # же стороны, говорит, что беды нет, — и когда сенсор молчит или отвалился
    # посреди мнимого провала, это тем более так.
    #
    # Эпизод при этом закрывается, а не замирает: сверка стареет через четверть
    # часа, и если сенсор всё ещё за порогом, следующий опрос обязан заговорить
    # сразу, а не досиживать получасовую паузу до повтора. Это и есть нужное
    # поведение — сверили, помолчали, через пятнадцать минут спросили снова.
    if direction and cleared_by_blood(verified, now, direction):
        return None, {}

    if age > MAX_AGE:
        # По старому значению тревогу не поднимают: оно ничего не говорит о
        # том, что происходит с сахаром сейчас. Эпизод при этом не закрывается —
        # сенсор мог отвалиться посреди гипогликемии.
        return None, state

    if level is None:
        # Возврат считается с той стороны, с которой эпизод открыт: поднявшись
        # из гипогликемии до 175, сахар вышел из беды, а опустившись до неё же
        # из гипергликемии — ещё нет, хотя показание одно и то же.
        if active and not _recovered(mgdl, DIRECTION[active]):
            return None, state
        return None, {}

    # Смена направления — это новый эпизод, а не продолжение: между низким и
    # высоким сравнивать нечего, и «уже 40 минут» от чужого начала соврало бы.
    turned = bool(active) and DIRECTION[level] != DIRECTION[active]
    since = now if turned or not active else state.get("since", now)

    if not active or turned:
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
    # этом никуда не денется — priority 2 повторяется до подтверждения. После
    # разворота сравнивать не с чем: худшим становится сам новый уровень.
    keeps_worst = bool(active) and not turned and SEVERITY[active] >= SEVERITY[level]
    worst = active if keeps_worst else level

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

    def check(
        self,
        readings: list[tuple[datetime, float]],
        verified: tuple[datetime, float] | None = None,
    ) -> None:
        """Alert on the newest reading, if it warrants one.

        ``verified`` — последняя сверка глюкометром из журнала, наивным UTC, как
        и показания. Необязательна: журнал ведёт бот, его может не быть вовсе, а
        база — лежать. Тревога в этих случаях работает как работала.
        """

        if not readings:
            return

        timestamp, mgdl = readings[-1]
        reading_at = timestamp.replace(tzinfo=timezone.utc).timestamp()
        now = time.time()

        blood = (
            None
            if verified is None
            else (
                verified[0].replace(tzinfo=timezone.utc).timestamp(),
                verified[1],
            )
        )

        state = self._load()
        alert, updated = decide(mgdl, now - reading_at, state, now, verified=blood)

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

        if alert.priority == PRIORITY_EMERGENCY:
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
