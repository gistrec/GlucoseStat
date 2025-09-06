"""Helper functions for common database operations."""

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .connection import SessionLocal
from .models import GlucoseMeasurement


def add_glucose_measurement(timestamp: datetime, value: float) -> None:
    """Insert a new glucose measurement, ignoring duplicates."""

    with SessionLocal() as session:  # type: Session
        session.add(GlucoseMeasurement(timestamp=timestamp, value=value))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()

