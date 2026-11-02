"""Хранение того, что принимает Nightscout API v1 (см. ``nightscout.py``).

Показания ложатся в ту же ``glucose_readings``, что и у LibreLinkUp, с
датчиком источника в ``sensor``; источники и их секреты — в
``glucose_sources``. Своей таблицы у транспорта нет: показание остаётся
показанием, откуда бы оно ни пришло.
"""

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from .connection import session
from .models import LIBRE, GlucoseReading, GlucoseSource


log = logging.getLogger("glucose.nightscout")

# Многострочный INSERT кусками: пачка истории доходит до 10 000 записей, и
# один запрос такого размера упирался бы в max_allowed_packet.
CHUNK = 1000

# Взаимоблокировка и ожидание блокировки — у InnoDB это нормальный исход двух
# параллельных INSERT IGNORE по одному ключу, а не ошибка данных.
RETRYABLE_MYSQL_ERRORS = (1205, 1213)
ATTEMPTS = 4

KIND = "nightscout"


@dataclass(frozen=True)
class Sgv:
    """Показание sgv, уже проверенное и приведённое к единицам базы."""

    # Наивный UTC, с точностью до секунды — как DATETIME в MySQL и ключ
    # glucose_readings.
    measured_at: datetime
    mgdl: float
    direction: str | None


@dataclass(frozen=True)
class Source:
    sensor: str
    label: str


@dataclass(frozen=True)
class SourceInfo:
    sensor: str
    label: str
    kind: str
    is_primary: bool
    secret_rotated_at: datetime | None
    last_received_at: datetime | None


class SourceExists(Exception):
    pass


def find_source(secret_digest: str) -> Source | None:
    """Датчик по дайджесту секрета, или None, если такого нет."""

    with session() as db:
        row = db.execute(
            select(GlucoseSource.sensor, GlucoseSource.label)
            .where(GlucoseSource.secret_digest == secret_digest)
            .where(GlucoseSource.kind == KIND)
        ).first()

    if row is None:
        return None
    return Source(sensor=row.sensor, label=row.label)


def _insert_ignore(table):
    # Один оператор на обе базы: MySQL в бою, SQLite в тестах.
    return insert(table).prefix_with("IGNORE", dialect="mysql").prefix_with(
        "OR IGNORE", dialect="sqlite"
    )


def _retryable(error: OperationalError) -> bool:
    code = getattr(error.orig, "args", (None,))[0]
    return code in RETRYABLE_MYSQL_ERRORS


def store_entries(source: Source, entries: list[Sgv], received_at: datetime) -> int:
    """Записать показания датчика, пропустив уже записанные.

    Повтор того же момента — первая запись остаётся. Возвращает число реально
    добавленных строк. Отметка приёма ставится в той же транзакции.
    """

    if not entries:
        return 0

    rows = {}
    for item in sorted(entries, key=lambda entry: entry.measured_at):
        rows.setdefault(
            item.measured_at,
            {
                "sensor": source.sensor,
                "timestamp": item.measured_at,
                "mgdl": item.mgdl,
                "received_at": received_at,
                "direction": item.direction,
            },
        )
    ordered = list(rows.values())

    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _store_once(source, ordered, received_at)
        except OperationalError as error:
            if attempt == ATTEMPTS or not _retryable(error):
                raise
            log.warning("nightscout insert conflicted, retrying (%d)", attempt)
            time.sleep(0.05 * attempt + random.random() * 0.05)

    raise AssertionError("unreachable")


def _store_once(source: Source, rows: list[dict], received_at: datetime) -> int:
    added = 0
    with session() as db:
        # Пачки одного датчика пишутся по очереди. Без этого параллельные
        # INSERT IGNORE по одним и тем же ключам берут разделяемые блокировки
        # на дубликатах и ловят друг друга на взаимной блокировке — на живом
        # MySQL это случалось на каждой второй гонке из восьми запросов.
        db.execute(
            select(GlucoseSource.sensor)
            .where(GlucoseSource.sensor == source.sensor)
            .with_for_update()
        )
        for start in range(0, len(rows), CHUNK):
            result = db.execute(_insert_ignore(GlucoseReading).values(rows[start : start + CHUNK]))
            added += max(result.rowcount, 0)

        db.execute(
            update(GlucoseSource)
            .where(GlucoseSource.sensor == source.sensor)
            .values(last_received_at=received_at)
        )
        db.commit()
    return added


@dataclass(frozen=True)
class StoredReading:
    measured_at: datetime
    mgdl: float
    direction: str | None


def list_entries(
    sensor: str,
    count: int,
    after_ms: int | None = None,
    before_ms: int | None = None,
    inclusive_after: bool = False,
    inclusive_before: bool = False,
) -> list[StoredReading]:
    """Показания одного датчика, свежие первыми, как отдаёт Nightscout."""

    def moment(value_ms: int) -> datetime:
        return datetime.fromtimestamp(value_ms / 1000, timezone.utc).replace(tzinfo=None)

    column = GlucoseReading.timestamp
    query = select(GlucoseReading).where(GlucoseReading.sensor == sensor)
    # Время в базе — до секунды, граница в миллисекундах: «после 12:00:00.500»
    # значит «с 12:00:01», «до 12:00:00.500» — «по 12:00:00».
    if after_ms is not None:
        bound = moment(after_ms)
        whole = after_ms % 1000 == 0
        if inclusive_after and whole:
            query = query.where(column >= bound)
        else:
            query = query.where(column > bound.replace(microsecond=0))
    if before_ms is not None:
        bound = moment(before_ms)
        whole = before_ms % 1000 == 0
        if inclusive_before or not whole:
            query = query.where(column <= bound.replace(microsecond=0))
        else:
            query = query.where(column < bound)
    query = query.order_by(column.desc()).limit(count)

    with session() as db:
        rows = db.execute(query).scalars().all()

    return [
        StoredReading(measured_at=row.timestamp, mgdl=row.mgdl, direction=row.direction)
        for row in rows
    ]


def issue_source(sensor: str, label: str, secret_digest: str, now: datetime) -> None:
    """Завести датчик, шлющий по Nightscout. Существующий — ошибка: это ``rotate``.

    Имя Libre занято LibreLinkUp, даже если его строки ещё нет: секрет на нём
    писал бы в кривую сборщика и читал бы её.
    """

    if sensor == LIBRE:
        raise SourceExists(sensor)
    try:
        with session() as db:
            db.add(
                GlucoseSource(
                    sensor=sensor,
                    label=label,
                    kind=KIND,
                    secret_digest=secret_digest,
                    is_primary=False,
                    created_at=now,
                    secret_rotated_at=now,
                )
            )
            db.commit()
    except IntegrityError:
        raise SourceExists(sensor) from None


def rotate_secret(sensor: str, secret_digest: str, now: datetime) -> bool:
    """Заменить секрет датчика; прежний перестаёт работать сразу."""

    with session() as db:
        result = db.execute(
            update(GlucoseSource)
            .where(GlucoseSource.sensor == sensor)
            .where(GlucoseSource.kind == KIND)
            .values(secret_digest=secret_digest, secret_rotated_at=now)
        )
        db.commit()
        return result.rowcount == 1


def list_sources() -> list[SourceInfo]:
    with session() as db:
        rows = db.execute(select(GlucoseSource).order_by(GlucoseSource.sensor)).scalars()
        return [
            SourceInfo(
                sensor=row.sensor,
                label=row.label,
                kind=row.kind,
                is_primary=bool(row.is_primary),
                secret_rotated_at=row.secret_rotated_at,
                last_received_at=row.last_received_at,
            )
            for row in rows
        ]
