"""Хранение того, что принимает Nightscout API v1 (см. ``nightscout.py``).

Отдельно от ``queries.py``: там чтение для снимка и запись сборщика, здесь —
загрузчики, их секреты и сырые показания, пришедшие по HTTP.
"""

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from .connection import session
from .models import GlucoseReading, NightscoutEntry, NightscoutUploader


log = logging.getLogger("glucose.nightscout")

# Многострочный INSERT кусками: пачка истории доходит до 10 000 записей, и
# один запрос такого размера упирался бы в max_allowed_packet.
CHUNK = 1000

# Взаимоблокировка и ожидание блокировки — у InnoDB это нормальный исход двух
# параллельных INSERT IGNORE по одному уникальному ключу, а не ошибка данных.
RETRYABLE_MYSQL_ERRORS = (1205, 1213)
ATTEMPTS = 4


@dataclass(frozen=True)
class Sgv:
    """Показание sgv, уже проверенное и приведённое к единицам базы."""

    device: str
    date_ms: int
    # Наивный UTC, с точностью до секунды — как DATETIME в MySQL.
    measured_at: datetime
    mgdl: float
    direction: str | None


@dataclass(frozen=True)
class Uploader:
    id: int
    owner: str
    feeds_dashboard: bool


@dataclass(frozen=True)
class StoredSgv:
    id: int
    sgv: Sgv


@dataclass(frozen=True)
class UploaderInfo:
    owner: str
    feeds_dashboard: bool
    created_at: datetime
    secret_rotated_at: datetime
    last_upload_at: datetime | None


def find_uploader(secret_digest: str) -> Uploader | None:
    """Загрузчик по дайджесту секрета, или None, если такого нет."""

    with session() as db:
        row = db.execute(
            select(
                NightscoutUploader.id,
                NightscoutUploader.owner,
                NightscoutUploader.feeds_dashboard,
            ).where(NightscoutUploader.secret_digest == secret_digest)
        ).first()

    if row is None:
        return None
    return Uploader(id=row.id, owner=row.owner, feeds_dashboard=bool(row.feeds_dashboard))


def _insert_ignore(table):
    # Один оператор на обе базы: MySQL в бою, SQLite в тестах.
    return insert(table).prefix_with("IGNORE", dialect="mysql").prefix_with(
        "OR IGNORE", dialect="sqlite"
    )


def _retryable(error: OperationalError) -> bool:
    code = getattr(error.orig, "args", (None,))[0]
    return code in RETRYABLE_MYSQL_ERRORS


def store_entries(
    uploader: Uploader, entries: list[Sgv], received_at: datetime
) -> tuple[list[StoredSgv], int]:
    """Записать показания загрузчика, пропустив уже записанные.

    Возвращает записи с их id (и новые, и найденные повторы — как Nightscout
    отвечает на повтор той же записью) и число реально добавленных строк.
    Отметка последнего приёма ставится в той же транзакции.
    """

    if not entries:
        return [], 0

    # Один и тот же порядок ключей у параллельных запросов — меньше шансов,
    # что InnoDB поймает их на взаимной блокировке.
    ordered = sorted(entries, key=lambda item: (item.device, item.date_ms))
    rows = [
        {
            "uploader_id": uploader.id,
            "device": item.device,
            "date_ms": item.date_ms,
            "measured_at": item.measured_at,
            "received_at": received_at,
            "mgdl": item.mgdl,
            "direction": item.direction,
        }
        for item in ordered
    ]

    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _store_once(uploader, ordered, rows, received_at)
        except OperationalError as error:
            if attempt == ATTEMPTS or not _retryable(error):
                raise
            log.warning("nightscout insert conflicted, retrying (%d)", attempt)
            time.sleep(0.05 * attempt + random.random() * 0.05)

    raise AssertionError("unreachable")


def _store_once(uploader, ordered, rows, received_at):
    added = 0
    with session() as db:
        # Пачки одного загрузчика пишутся по очереди. Без этого параллельные
        # INSERT IGNORE по одним и тем же ключам берут разделяемые блокировки
        # на дубликатах и ловят друг друга на взаимной блокировке — на живом
        # MySQL это случалось на каждой второй гонке из восьми запросов.
        db.execute(
            select(NightscoutUploader.id)
            .where(NightscoutUploader.id == uploader.id)
            .with_for_update()
        )
        for start in range(0, len(rows), CHUNK):
            result = db.execute(_insert_ignore(NightscoutEntry).values(rows[start : start + CHUNK]))
            added += max(result.rowcount, 0)

        if uploader.feeds_dashboard:
            # Кривая дашборда: первая запись на секунду остаётся, повтор того
            # же момента из LibreLinkUp или отсюда — пропускается.
            readings = {}
            for item in ordered:
                readings.setdefault(item.measured_at, item.mgdl)
            dashboard = [{"timestamp": moment, "mgdl": mgdl} for moment, mgdl in readings.items()]
            for start in range(0, len(dashboard), CHUNK):
                db.execute(_insert_ignore(GlucoseReading).values(dashboard[start : start + CHUNK]))

        db.execute(
            update(NightscoutUploader)
            .where(NightscoutUploader.id == uploader.id)
            .values(last_upload_at=received_at)
        )

        low = min(item.date_ms for item in ordered)
        high = max(item.date_ms for item in ordered)
        ids = {
            (row.device, row.date_ms): row.id
            for row in db.execute(
                select(NightscoutEntry.id, NightscoutEntry.device, NightscoutEntry.date_ms)
                .where(NightscoutEntry.uploader_id == uploader.id)
                .where(NightscoutEntry.date_ms.between(low, high))
            )
        }
        db.commit()

    stored = [StoredSgv(id=ids[(item.device, item.date_ms)], sgv=item) for item in ordered]
    return stored, added


def list_entries(
    uploader_id: int,
    count: int,
    after_ms: int | None = None,
    before_ms: int | None = None,
    inclusive_after: bool = False,
    inclusive_before: bool = False,
) -> list[StoredSgv]:
    """Показания одного загрузчика, свежие первыми, как отдаёт Nightscout."""

    query = select(NightscoutEntry).where(NightscoutEntry.uploader_id == uploader_id)
    if after_ms is not None:
        column = NightscoutEntry.date_ms
        query = query.where(column >= after_ms if inclusive_after else column > after_ms)
    if before_ms is not None:
        column = NightscoutEntry.date_ms
        query = query.where(column <= before_ms if inclusive_before else column < before_ms)
    query = query.order_by(NightscoutEntry.date_ms.desc(), NightscoutEntry.id.desc()).limit(count)

    with session() as db:
        rows = db.execute(query).scalars().all()

    return [
        StoredSgv(
            id=row.id,
            sgv=Sgv(
                device=row.device,
                date_ms=row.date_ms,
                measured_at=row.measured_at,
                mgdl=row.mgdl,
                direction=row.direction,
            ),
        )
        for row in rows
    ]


def issue_uploader(owner: str, secret_digest: str, feeds_dashboard: bool, now: datetime) -> None:
    """Завести загрузчика. Существующий — ошибка: перевыпуск это ``rotate``."""

    with session() as db:
        db.add(
            NightscoutUploader(
                owner=owner,
                secret_digest=secret_digest,
                feeds_dashboard=feeds_dashboard,
                created_at=now,
                secret_rotated_at=now,
            )
        )
        db.commit()


def rotate_secret(owner: str, secret_digest: str, now: datetime) -> bool:
    """Заменить секрет загрузчика; прежний перестаёт работать сразу."""

    with session() as db:
        result = db.execute(
            update(NightscoutUploader)
            .where(NightscoutUploader.owner == owner)
            .values(secret_digest=secret_digest, secret_rotated_at=now)
        )
        db.commit()
        return result.rowcount == 1


def list_uploaders() -> list[UploaderInfo]:
    with session() as db:
        rows = db.execute(select(NightscoutUploader).order_by(NightscoutUploader.id)).scalars()
        return [
            UploaderInfo(
                owner=row.owner,
                feeds_dashboard=bool(row.feeds_dashboard),
                created_at=row.created_at,
                secret_rotated_at=row.secret_rotated_at,
                last_upload_at=row.last_upload_at,
            )
            for row in rows
        ]


def read_dashboard_last_upload() -> datetime | None:
    """Последний приём по Nightscout у владельца дашборда, или None.

    None и тогда, когда таблицы нет: её создаёт init_schema() сборщика или
    сервера приёма, а рендерер на реплике и не может.
    """

    try:
        with session() as db:
            return db.execute(
                select(func.max(NightscoutUploader.last_upload_at)).where(
                    NightscoutUploader.feeds_dashboard.is_(True)
                )
            ).scalar_one_or_none()
    except SQLAlchemyError as error:
        log.warning("nightscout uploads unavailable, publishing without them: %s", error)
        return None
