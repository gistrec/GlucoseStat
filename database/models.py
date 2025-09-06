"""SQLAlchemy models for database tables."""

from sqlalchemy import Column, DateTime, Float
from sqlalchemy.orm import declarative_base


Base = declarative_base()


class GlucoseMeasurement(Base):
    """Single glucose measurement."""

    __tablename__ = "glucose_measurements"

    timestamp = Column(DateTime, primary_key=True)
    value = Column(Float, nullable=False)
