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
    # Сахар по глюкометру, мг/дл — заполнен только у kind='fingerstick'. В тех
    # же единицах, что и glucose_readings.mgdl: сверка рисуется на той же
    # кривой и в том же масштабе, и переводить её отдельно значило бы однажды
    # перевести дважды.
    Column("mgdl", Numeric(5, 1)),
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

# Прогноз модели — тоже боту: он учит её на журнале и кривой (ml/live.py) и
# пишет сюда на каждую новую точку. Страница читает последнюю и рисует хвост
# вместо линейного продолжения по скорости. Та же оговорка, что у журнала:
# таблицы может не быть, и снимок обязан собраться без неё.
glucose_forecasts = Table(
    "glucose_forecasts",
    journal_metadata,
    # Точка кривой, от которой считан прогноз. Наивный UTC, как timestamp.
    Column("made_at", DateTime, primary_key=True),
    Column("horizon_min", Integer, primary_key=True),
    # мг/дл, как и glucose_readings: переводит страница, тем же делителем.
    Column("mgdl", Float, nullable=False),
    Column("model", String(32), nullable=False),
    Column("created_at", DateTime, nullable=False),
)

# Нижняя граница того же прогноза — «в девяти случаях из десяти не ниже»
# (ml.boost.ConformalLower в боте). Своя таблица, потому что ключ прогноза —
# точка и горизонт без модели. Страница рисует её предупреждением о падении
# вместо прямой по скорости. Таблицы может не быть — снимок соберётся без неё.
glucose_forecast_bounds = Table(
    "glucose_forecast_bounds",
    journal_metadata,
    Column("made_at", DateTime, primary_key=True),
    Column("horizon_min", Integer, primary_key=True),
    Column("mgdl", Float, nullable=False),
    Column("model", String(32), nullable=False),
    Column("created_at", DateTime, nullable=False),
)

# История часовых поясов человека — бот пишет строку на каждый /tz. Нужна
# коэффициенту: «утро» приёма — по часам там, где человек ел, а не по поясу,
# в котором страница режет сутки.
user_timezones = Table(
    "user_timezones",
    journal_metadata,
    Column("id", BigInteger, primary_key=True),
    Column("tg_user_id", BigInteger, nullable=False),
    Column("tz", String(64), nullable=False),
    # С какого момента пояс действует. Наивный UTC, как occurred_at.
    Column("effective_from", DateTime, nullable=False),
)


class CollectorState(Base):
    """Состояние сборщика, видимое рендереру на другой машине.

    Пишет сюда только сборщик, он один на парк; рендереры читают. Без этой
    таблицы ``last_success`` оставался бы в памяти процесса, и страница,
    собранная на реплике, не могла бы сказать «данные не идут».
    """

    __tablename__ = "collector_state"

    # `key` — зарезервированное слово MySQL.
    name = Column(String(32), primary_key=True)
    # Наивный UTC, как timestamp у показаний.
    occurred_at = Column(DateTime, nullable=False)


class SensorRecord(Base):
    """Каждый сенсор, который видел сборщик, — строкой на установку.

    ``collector_state`` держит только текущий: отметка перезаписывается, и
    на графике за неделю прошлую замену было бы нечем подписать. Здесь
    история — для линий замены на графике и для сверок по сенсорам.
    """

    __tablename__ = "sensors"

    # Момент установки, наивный UTC. Ключ: установка у сенсора одна, и
    # повторный опрос того же сенсора обновляет строку, а не множит её.
    started = Column(DateTime, primary_key=True)
    ends = Column(DateTime, nullable=True)
    # Номер модели из ответа Abbott (``pt``), см. SENSOR_MODELS.
    kind = Column(Integer, nullable=True)
    # Откуда известна установка: ``abbott`` — из ответа LibreLinkUp, ``gap`` —
    # восстановлена по разрыву прогрева в показаниях (сенсоры до этой таблицы).
    source = Column(String(16), nullable=False)


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
