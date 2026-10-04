"""Helper functions for common database operations."""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from .connection import session
from .models import (
    CollectorState,
    GlucoseReading,
    journal_entries,
    meal_confirmations,
    meal_estimates,
)


log = logging.getLogger("glucose.queries")


def store_readings(readings: list[tuple[datetime, float]]) -> int:
    """Insert readings, skipping the ones already stored.

    LibreLinkUp replays its whole graph window on every poll, so all but the
    newest handful of rows are duplicates by design. INSERT IGNORE lets the
    primary key do that filtering in a single round-trip instead of a SELECT
    plus a row-by-row insert. Returns the number of rows actually added.
    """

    if not readings:
        return 0

    rows = [{"timestamp": timestamp, "mgdl": mgdl} for timestamp, mgdl in readings]

    # .values(rows), а не executemany: одним multi-row INSERT сохраняется
    # rowcount, по которому видно, сколько строк реально новых.
    statement = insert(GlucoseReading).prefix_with("IGNORE").values(rows)

    with session() as db:
        result = db.execute(statement)
        db.commit()
        return result.rowcount


def _store_mark(name: str, when: datetime) -> None:
    """Записать отметку сборщика под именем ``name``."""

    statement = insert(CollectorState).values(name=name, occurred_at=when)
    statement = statement.on_duplicate_key_update(
        occurred_at=statement.inserted.occurred_at
    )

    with session() as db:
        db.execute(statement)
        db.commit()


def _read_mark(name: str) -> datetime | None:
    """Отметка сборщика, или None, если сказать нечего."""

    try:
        with session() as db:
            return db.execute(
                select(CollectorState.occurred_at).where(CollectorState.name == name)
            ).scalar_one_or_none()
    except ProgrammingError:
        # Таблицы ещё нет: её создаёт init_schema() сборщика, а рендерер на
        # реплике и не может — там запись запрещена. Пусть публикация
        # наследует отметку из прежнего снимка, как делала до этой таблицы.
        return None


def store_last_success(when: datetime) -> None:
    """Запомнить, когда сборщик последний раз достучался до LibreLinkUp."""

    _store_mark("last_success", when)


def read_last_success() -> datetime | None:
    """Отметка сборщика, или None, если сказать нечего."""

    return _read_mark("last_success")


# Сенсор и его дата установки — тоже состояние сборщика, а не отдельная
# таблица: это одна отметка времени, которую пишет один процесс и читают
# рендереры. Своя таблица ради одной строки потребовала бы миграции на обеих
# машинах, а разницы в смысле между ней и «когда сборщик последний раз
# достучался» нет никакой.
def store_sensor_start(when: datetime) -> None:
    """Запомнить, когда был установлен сенсор, передающий сейчас."""

    _store_mark("sensor_started", when)


def read_sensor_start() -> datetime | None:
    """Момент установки сенсора, или None, если сборщик его не знает."""

    return _read_mark("sensor_started")


# Конец срока хранится отметкой времени, а не сроком в днях: в этой таблице
# один столбец, и он DATETIME. Считает его сборщик — только он видит модель
# прибора; рендереру на реплике остаётся прочитать готовую дату.
def store_sensor_end(when: datetime) -> None:
    """Запомнить, когда у сенсора кончается срок."""

    _store_mark("sensor_ends", when)


def read_sensor_end() -> datetime | None:
    """Конец срока сенсора, или None, пока сборщик его не записал."""

    return _read_mark("sensor_ends")


def readings_since(start: datetime) -> list[tuple[datetime, float]]:
    """Return readings at or after ``start``, oldest first."""

    with session() as db:
        rows = db.execute(
            select(GlucoseReading.timestamp, GlucoseReading.mgdl)
            .where(GlucoseReading.timestamp >= start)
            .order_by(GlucoseReading.timestamp)
        ).all()

    return [(row.timestamp, row.mgdl) for row in rows]


def last_readings(limit: int = 10) -> list[tuple[datetime, float]]:
    """Return the newest readings, oldest first, ignoring how old they are.

    The dashboard needs the current value even when the sensor has been off
    for months — that is what lets the page say "no data since <date>"
    instead of rendering an empty panel.
    """

    with session() as db:
        rows = db.execute(
            select(GlucoseReading.timestamp, GlucoseReading.mgdl)
            .order_by(GlucoseReading.timestamp.desc())
            .limit(limit)
        ).all()

    return [(row.timestamp, row.mgdl) for row in reversed(rows)]


def journal_since(start: datetime) -> list[tuple[datetime, str, float | None, float | None]]:
    """Return journal events at or after ``start``, oldest first.

    Возвращает пустой список, если таблицы нет. Журнал заводит бот, а
    коллектор с дашбордом работали задолго до него и обязаны продолжать
    работать без него: график глюкозы не должен пропадать оттого, что бота ещё
    не развернули или его схему переименовали.
    """

    try:
        with session() as db:
            rows = db.execute(
                select(
                    journal_entries.c.occurred_at,
                    journal_entries.c.kind,
                    journal_entries.c.carbs_g,
                    journal_entries.c.units,
                )
                .where(journal_entries.c.occurred_at >= start)
                .order_by(journal_entries.c.occurred_at)
            ).all()
    except SQLAlchemyError as error:
        log.warning("journal unavailable, publishing without events: %s", error)
        return []

    return [
        (
            row.occurred_at,
            str(row.kind),
            None if row.carbs_g is None else float(row.carbs_g),
            None if row.units is None else float(row.units),
        )
        for row in rows
    ]


def fingersticks_since(start: datetime) -> list[tuple[datetime, float]]:
    """Замеры глюкометром не раньше ``start``, старые первыми, в мг/дл.

    Отдельным запросом от ``journal_since``, а не пятым полем в его кортеже —
    по той же причине, по какой отдельно живут ``meal_origins_since``: журнал
    читают разбор, ночные сводки и дорожки событий, и расширение его кортежа
    задевало бы всех троих ради ряда, который нужен одному графику.

    Возвращает пустой список, если таблицы или колонки нет: журнал заводит
    бот, и кривая глюкозы не должна пропадать со страницы оттого, что на той
    стороне ещё не выложили версию с этой колонкой.
    """

    try:
        with session() as db:
            rows = db.execute(
                select(journal_entries.c.occurred_at, journal_entries.c.mgdl)
                .where(journal_entries.c.kind == "fingerstick")
                .where(journal_entries.c.mgdl.is_not(None))
                .where(journal_entries.c.occurred_at >= start)
                .order_by(journal_entries.c.occurred_at)
            ).all()
    except SQLAlchemyError as error:
        log.warning("fingersticks unavailable, publishing without them: %s", error)
        return []

    return [(row.occurred_at, float(row.mgdl)) for row in rows]


def latest_fingerstick() -> tuple[datetime, float] | None:
    """Самая свежая сверка глюкометром, наивным UTC и в мг/дл. ``None`` — нет.

    Без окна: свежесть — правило тревоги, и живёт она в ``notify`` рядом с
    порогами, которые эта сверка снимает. Здесь окно было бы вторым числом про
    то же самое, и однажды разъехалось бы с первым — причём в сторону молчания,
    которую по логам не видно.

    Одна строка и без фильтра по пользователю, как и весь журнал здесь: сенсор
    один, и кровь сверяют с ним же.
    """

    try:
        with session() as db:
            row = db.execute(
                select(journal_entries.c.occurred_at, journal_entries.c.mgdl)
                .where(journal_entries.c.kind == "fingerstick")
                .where(journal_entries.c.mgdl.is_not(None))
                .order_by(journal_entries.c.occurred_at.desc())
                .limit(1)
            ).one_or_none()
    except SQLAlchemyError as error:
        log.warning("latest fingerstick unavailable, alerting without it: %s", error)
        return None

    return None if row is None else (row.occurred_at, float(row.mgdl))


def meal_origins_since(start: datetime) -> dict[datetime, list[dict]]:
    """Чем подтверждено число углеводов у каждой записи еды, по её метке.

    Отдельным запросом от ``journal_since``: подтверждения и оценки — таблицы
    бота, и их отсутствие стоит одного значка, а не всех отметок еды на
    графике. Уровень отсюда не считается, это делает ``analysis.trust_level``.
    """

    try:
        with session() as db:
            rows = db.execute(
                select(
                    journal_entries.c.occurred_at,
                    journal_entries.c.source,
                    journal_entries.c.confidence,
                    meal_confirmations.c.was_weighed,
                    meal_confirmations.c.confirmed_carbs_g,
                    meal_estimates.c.median_carbs_g,
                    meal_estimates.c.spread_g,
                )
                .select_from(
                    journal_entries.outerjoin(
                        meal_confirmations,
                        meal_confirmations.c.journal_entry_id == journal_entries.c.id,
                    ).outerjoin(
                        meal_estimates,
                        meal_estimates.c.id == meal_confirmations.c.estimate_id,
                    )
                )
                .where(journal_entries.c.kind == "meal")
                .where(journal_entries.c.occurred_at >= start)
            ).all()
    except SQLAlchemyError as error:
        log.warning("meal origins unavailable, publishing without them: %s", error)
        return {}

    origins: dict[datetime, list[dict]] = {}
    for row in rows:
        origins.setdefault(row.occurred_at, []).append(
            {
                "source": None if row.source is None else str(row.source),
                "confidence": None if row.confidence is None else int(row.confidence),
                "was_weighed": None if row.was_weighed is None else bool(row.was_weighed),
                "median": None if row.median_carbs_g is None else float(row.median_carbs_g),
                "spread": None if row.spread_g is None else float(row.spread_g),
                "confirmed": None
                if row.confirmed_carbs_g is None
                else float(row.confirmed_carbs_g),
            }
        )
    return origins
