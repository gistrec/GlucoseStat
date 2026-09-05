"""SQLAlchemy models for database tables."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
)
from sqlalchemy.orm import declarative_base


Base = declarative_base()

# Журнал событий принадлежит боту GlucoseBot — он его создаёт и в него пишет.
# Здесь только чтение, и метаданные намеренно отдельные: попади таблица в
# Base, init_schema() коллектора создавал бы её сам, и версия схемы зависела
# бы от того, кто из двух процессов стартовал первым.
journal_metadata = MetaData()

journal_entries = Table(
    "journal_entries",
    journal_metadata,
    Column("id", BigInteger, primary_key=True),
    Column("occurred_at", DateTime, nullable=False),
    Column("tg_user_id", BigInteger, nullable=False),
    # В боте это ENUM('meal','bolus','basal'); читаем строкой, чтобы новое
    # значение на той стороне не роняло выкладку снимка здесь.
    Column("kind", String(16), nullable=False),
    Column("carbs_g", Numeric(6, 1)),
    Column("units", Numeric(5, 2)),
    Column("note", Text),
    Column("source", String(32)),
    # Ответ человека боту: насколько он верит числу углеводов, от 1 до 3. Пусто
    # у записей, сделанных до появления вопроса, — тогда уверенность выводится
    # из способа и разброса прогонов, как выводилась раньше.
    Column("confidence", Integer),
)

meal_confirmations = Table(
    "meal_confirmations",
    journal_metadata,
    Column("estimate_id", BigInteger, primary_key=True),
    Column("confirmed_carbs_g", Numeric(6, 1)),
    Column("was_weighed", Boolean),
    Column("journal_entry_id", BigInteger),
)

meal_estimates = Table(
    "meal_estimates",
    journal_metadata,
    Column("id", BigInteger, primary_key=True),
    Column("median_carbs_g", Numeric(6, 1)),
    Column("spread_g", Numeric(6, 1)),
)


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
