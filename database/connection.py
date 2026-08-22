"""SQLAlchemy engine and session configuration.

The engine is built on first use, not at import time: ``main.py`` calls
``load_dotenv()`` inside ``main()``, so anything reading os.environ during
import would see an empty config.
"""
import os
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


@lru_cache(maxsize=1)
def _engine() -> Engine:
    user = os.getenv("MYSQL_USER")
    password = os.getenv("MYSQL_PASSWORD")
    host = os.getenv("MYSQL_HOST")
    port = os.getenv("MYSQL_PORT")
    database = os.getenv("MYSQL_DB")

    if not all([user, password, host, port, database]):
        raise RuntimeError(
            "MYSQL_USER, MYSQL_PASSWORD, MYSQL_HOST, MYSQL_PORT and MYSQL_DB must be set"
        )

    url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"

    # Сертификат нужен только managed-базам, которые требуют TLS; для MySQL на
    # том же хосте переменная не задаётся. Сертификат Yandex Cloud:
    #   mkdir -p ~/.mysql
    #   curl -o ~/.mysql/root.crt https://storage.yandexcloud.net/cloud-certs/CA.pem
    ssl_ca = os.getenv("MYSQL_SSL_CA")
    if ssl_ca:
        ssl_ca_path = os.path.expanduser(ssl_ca)
        if not os.path.isfile(ssl_ca_path):
            raise RuntimeError(f"MYSQL_SSL_CA points to a missing file: {ssl_ca_path}")
        url += f"?ssl_ca={ssl_ca_path}"

    # pool_recycle: сборщик живёт неделями, а MySQL рвёт простаивающие
    # соединения по wait_timeout (8 часов по умолчанию). pool_pre_ping ловит
    # уже мёртвые, recycle не даёт им дожить до этого состояния.
    return create_engine(url, pool_pre_ping=True, pool_recycle=3600)


def session() -> Session:
    """Open a session. Use as a context manager."""

    return sessionmaker(autocommit=False, autoflush=False, bind=_engine())()


def init_schema() -> None:
    """Create missing tables. Safe to call on every start."""

    Base.metadata.create_all(_engine())
