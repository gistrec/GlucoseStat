"""Helper functions for common database operations."""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import SQLAlchemyError

from .connection import session
from .models import (
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
                "was_weighed": None if row.was_weighed is None else bool(row.was_weighed),
                "median": None if row.median_carbs_g is None else float(row.median_carbs_g),
                "spread": None if row.spread_g is None else float(row.spread_g),
                "confirmed": None
                if row.confirmed_carbs_g is None
                else float(row.confirmed_carbs_g),
            }
        )
    return origins
