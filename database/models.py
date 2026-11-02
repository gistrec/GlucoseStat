"""SQLAlchemy models for database tables."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
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


class NightscoutUploader(Base):
    """Загрузчик, который шлёт показания по Nightscout API v1, — строкой на человека.

    Секрет в базе не лежит: загрузчик присылает SHA-1 от него, а хранится
    SHA-256 от этого SHA-1. Прочитавший таблицу не получает ни секрета, ни
    того, что можно подставить в заголовок ``api-secret``.
    """

    __tablename__ = "nightscout_uploaders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner = Column(String(64), nullable=False, unique=True)
    secret_digest = Column(String(64), nullable=False, unique=True)
    # Только показания владельца дашборда попадают в glucose_readings: кривая
    # на странице одна, и чужое в ней было бы чужими данными у него.
    feeds_dashboard = Column(Boolean, nullable=False, default=False)
    # Наивный UTC, как timestamp у показаний.
    created_at = Column(DateTime, nullable=False)
    secret_rotated_at = Column(DateTime, nullable=False)
    last_upload_at = Column(DateTime, nullable=True)


class NightscoutEntry(Base):
    """Показание sgv, принятое по Nightscout API, в том виде, как пришло.

    Ключ дедупликации — загрузчик, прибор и момент измерения в миллисекундах:
    повтор той же пачки после обрыва связи не плодит строк, а уникальный
    индекс делает это и при параллельных запросах.
    """

    __tablename__ = "nightscout_entries"
    __table_args__ = (
        UniqueConstraint("uploader_id", "device", "date_ms", name="uq_nightscout_entry"),
    )

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    uploader_id = Column(Integer, ForeignKey("nightscout_uploaders.id"), nullable=False)
    # Пустая строка, а не NULL: NULL в уникальном индексе MySQL не равен
    # самому себе, и повтор без device проходил бы мимо дедупликации.
    # Сравнение побайтовое: в collation по умолчанию «Ottai» и «ottai» — один
    # ключ, и повтор с другим регистром молча слился бы с чужим прибором.
    device = Column(
        String(64).with_variant(mysql.VARCHAR(64, collation="utf8mb4_bin"), "mysql"),
        nullable=False,
        default="",
    )
    # Момент измерения: date из запроса как есть и он же наивным UTC.
    date_ms = Column(BigInteger, nullable=False)
    measured_at = Column(DateTime, nullable=False)
    # Когда сервис его принял — отдельно от измерения: история после обрыва
    # связи приходит часами позже, чем была снята.
    received_at = Column(DateTime, nullable=False)
    mgdl = Column(Float, nullable=False)
    direction = Column(String(32), nullable=True)
