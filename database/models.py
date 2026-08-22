"""SQLAlchemy models for database tables."""

from sqlalchemy import Column, DateTime, Float
from sqlalchemy.orm import declarative_base


Base = declarative_base()


class GlucoseReading(Base):
    """Single glucose reading as published by LibreLinkUp.

    ``timestamp`` is naive UTC. MySQL DATETIME carries no zone, so the
    collector normalises to UTC before writing — see ``main.py``.

    ``mgdl`` is always mg/dL. The API's ``Value`` follows the account's
    display units (mmol/L for a European account), ``ValueInMgPerDl`` does
    not; storing the latter keeps rows comparable regardless of how the
    LibreLinkUp profile is configured.
    """

    __tablename__ = "glucose_readings"

    timestamp = Column(DateTime, primary_key=True)
    mgdl = Column(Float, nullable=False)
