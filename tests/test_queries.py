"""Tests for the query helpers that must survive a half-deployed fleet.

Ни один тест здесь не открывает соединения: проверяется обработка отказа, а
не сам запрос.
"""

import pytest
from sqlalchemy.exc import ProgrammingError

from database.queries import read_last_success


class TestReadLastSuccess:
    def test_a_missing_table_reads_as_nothing_to_report(self, monkeypatch):
        # Рендерер обновляется раньше сборщика, и таблицы ещё нет: создаёт её
        # init_schema() сборщика, а на реплике это и невозможно. Упади запрос —
        # публикация встала бы вместо того, чтобы взять отметку из снимка.
        def missing_table():
            raise ProgrammingError(
                "SELECT occurred_at FROM collector_state",
                {},
                Exception("1146 (42S02): Table 'libre.collector_state' doesn't exist"),
            )

        monkeypatch.setattr("database.queries.session", missing_table)

        assert read_last_success() is None

    def test_other_failures_still_surface(self, monkeypatch):
        # Глушится ровно отсутствующая таблица. Недоступная база обязана
        # уронить публикацию: снимок всё равно строится из неё же.
        def down():
            raise RuntimeError("mysql is down")

        monkeypatch.setattr("database.queries.session", down)

        with pytest.raises(RuntimeError):
            read_last_success()
