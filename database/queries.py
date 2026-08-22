"""Helper functions for common database operations."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from .connection import session
from .models import GlucoseReading


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
