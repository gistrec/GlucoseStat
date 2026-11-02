"""Приём показаний по Nightscout API v1 и общая таблица датчиков.

Сервер поднимается настоящий, на свободном порту, а база — SQLite в файле:
так проверяется весь путь — заголовок, разбор, запись, ответ, — а не каждая
функция по отдельности. MySQL-специфику (INSERT IGNORE под параллельной
нагрузкой) тесты видят через тот же оператор: он компилируется в
``INSERT OR IGNORE`` для SQLite и в ``INSERT IGNORE`` для MySQL.
"""

import gzip
import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
import requests
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from database import nightscout as storage
from database import queries
from database.models import Base, GlucoseReading, GlucoseSource
from nightscout import (
    MAX_BODY_BYTES,
    MAX_ITEMS,
    Backend,
    api_secret_header,
    digest_of_secret,
    make_server,
    parse_entries,
    parse_entry,
)


NOW = datetime(2026, 11, 2, 12, 0, 0)
OTTAI_SECRET = "ottaiSecretValue123456"
DEXCOM_SECRET = "dexcomSecretValue12345"


def ms(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp() * 1000)


def sgv(value=120, at=NOW, direction="Flat", **extra):
    entry = {"type": "sgv", "sgv": value, "date": ms(at), "direction": direction, "device": "Ottai"}
    entry.update(extra)
    return entry


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ns.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr("database.nightscout.session", factory)
    monkeypatch.setattr("database.queries.session", factory)

    queries.ensure_libre_source(NOW)
    storage.issue_source("ottai", "Ottai", digest_of_secret(OTTAI_SECRET), NOW)
    storage.issue_source("dexcom", "Dexcom", digest_of_secret(DEXCOM_SECRET), NOW)
    return factory


def serve(backend):
    server = make_server("127.0.0.1", 0, backend)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/api/v1"


@pytest.fixture
def api(db):
    server, url = serve(
        Backend(
            find_source=storage.find_source,
            store_entries=storage.store_entries,
            list_entries=storage.list_entries,
            now=lambda: NOW,
        )
    )
    yield url
    server.shutdown()
    server.server_close()


def auth(secret):
    return {"api-secret": api_secret_header(secret)}


def readings(db, sensor=None):
    with db() as session:
        query = select(GlucoseReading).order_by(GlucoseReading.timestamp)
        if sensor is not None:
            query = query.where(GlucoseReading.sensor == sensor)
        return session.execute(query).scalars().all()


def source(db, sensor):
    with db() as session:
        return session.execute(
            select(GlucoseSource).where(GlucoseSource.sensor == sensor)
        ).scalar_one()


def set_primary(db, sensor):
    with db() as session:
        session.execute(update(GlucoseSource).values(is_primary=GlucoseSource.sensor == sensor))
        session.commit()


# ── Авторизация и изоляция ───────────────────────────────────────────────


class TestAuth:
    def test_no_secret_is_refused_and_nothing_is_stored(self, api, db):
        response = requests.post(f"{api}/entries", json=[sgv()])

        assert response.status_code == 401
        assert response.json() == {"status": 401, "message": "Unauthorized", "description": "Invalid/Missing"}
        assert readings(db) == []

    def test_the_plain_secret_is_not_accepted(self, api):
        # Nightscout сравнивает только хеши: секрет как есть — не пароль.
        response = requests.post(f"{api}/entries", json=[sgv()], headers={"api-secret": OTTAI_SECRET})

        assert response.status_code == 401

    def test_a_wrong_secret_is_refused(self, api):
        response = requests.post(f"{api}/entries", json=[sgv()], headers=auth("not-a-real-secret-x"))

        assert response.status_code == 401

    def test_sha1_is_case_insensitive(self, api):
        header = {"api-secret": api_secret_header(OTTAI_SECRET).upper()}

        assert requests.post(f"{api}/entries", json=[sgv()], headers=header).status_code == 200

    def test_the_secret_query_parameter_works(self, api):
        response = requests.post(
            f"{api}/entries.json", params={"secret": api_secret_header(OTTAI_SECRET)}, json=[sgv()]
        )

        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_a_rotated_secret_stops_working_at_once(self, api):
        new = "rotatedSecretValue1234"
        assert storage.rotate_secret("ottai", digest_of_secret(new), NOW)

        assert requests.post(f"{api}/entries", json=[sgv()], headers=auth(OTTAI_SECRET)).status_code == 401
        assert requests.post(f"{api}/entries", json=[sgv()], headers=auth(new)).status_code == 200

    def test_libre_cannot_be_given_a_secret(self, db):
        # LibreLinkUp-источник секрета не имеет: rotate по нему — отказ, а не
        # тихо открытая запись в кривую Libre.
        assert not storage.rotate_secret("libre", digest_of_secret("whatever12345"), NOW)

    def test_issuing_the_same_sensor_twice_is_refused(self, db):
        with pytest.raises(storage.SourceExists):
            storage.issue_source("ottai", "Ottai", digest_of_secret("anotherSecret123"), NOW)

    def test_the_database_holds_neither_the_secret_nor_its_sha1(self, db):
        stored = source(db, "ottai").secret_digest

        assert stored != OTTAI_SECRET
        assert stored != api_secret_header(OTTAI_SECRET)
        assert stored == hashlib.sha256(api_secret_header(OTTAI_SECRET).encode()).hexdigest()

    def test_reading_needs_the_secret(self, api):
        assert requests.get(f"{api}/entries.json").status_code == 401
        assert requests.get(f"{api}/entries/current.json").status_code == 401

    def test_each_sensor_reads_only_its_own_entries(self, api, db):
        requests.post(f"{api}/entries", json=[sgv(100, NOW - timedelta(minutes=5))], headers=auth(OTTAI_SECRET))
        requests.post(f"{api}/entries", json=[sgv(200, NOW)], headers=auth(DEXCOM_SECRET))
        queries.store_readings([(NOW - timedelta(minutes=1), 150.0)], sensor="libre")

        ottai = requests.get(f"{api}/entries.json", headers=auth(OTTAI_SECRET)).json()
        dexcom = requests.get(f"{api}/entries/sgv.json", headers=auth(DEXCOM_SECRET)).json()

        assert [entry["sgv"] for entry in ottai] == [100]
        assert [entry["sgv"] for entry in dexcom] == [200]

    def test_a_sensor_writes_only_under_its_own_name(self, api, db):
        requests.post(f"{api}/entries", json=[sgv(100)], headers=auth(OTTAI_SECRET))

        assert [(row.sensor, row.mgdl) for row in readings(db)] == [("ottai", 100.0)]

    def test_the_same_moment_from_two_sensors_is_two_readings(self, api, db):
        queries.store_readings([(NOW, 150.0)], sensor="libre")
        requests.post(f"{api}/entries", json=[sgv(100)], headers=auth(OTTAI_SECRET))

        assert sorted((row.sensor, row.mgdl) for row in readings(db)) == [("libre", 150.0), ("ottai", 100.0)]

    def test_status_answers_without_a_secret(self, api):
        body = requests.get(f"{api}/status.json").json()

        assert body["status"] == "ok"
        assert body["apiEnabled"] is True
        assert isinstance(body["serverTimeEpoch"], int)

    def test_verifyauth_tells_a_good_secret_from_a_bad_one(self, api):
        good = requests.get(f"{api}/verifyauth", headers=auth(OTTAI_SECRET)).json()["message"]
        bad = requests.get(f"{api}/verifyauth", headers=auth("nope-nope-nope")).json()["message"]

        assert good["message"] == "OK" and good["canWrite"] is True
        assert bad["message"] == "UNAUTHORIZED" and bad["canWrite"] is False

    def test_neither_the_secret_nor_its_hash_reaches_the_log(self, api, caplog):
        # Только журнал сервера: urllib3 клиента пишет свой URL целиком.
        caplog.set_level(logging.DEBUG, logger="glucose.nightscout")
        header = api_secret_header(OTTAI_SECRET)
        requests.post(f"{api}/entries", params={"secret": header}, json=[sgv()])
        requests.post(f"{api}/entries", params={"secret": "wrongwrongwrong"}, json=[sgv()])

        text = "\n".join(
            record.getMessage() for record in caplog.records if record.name.startswith("glucose")
        )
        assert header not in text
        assert OTTAI_SECRET not in text
        assert "wrongwrongwrong" not in text
        assert "POST /api/v1/entries -> 200" in text


# ── Единицы и время ──────────────────────────────────────────────────────


class TestUnitsAndTime:
    def test_sgv_is_stored_as_mgdl_without_conversion(self, api, db):
        # 99 мг/дл = 5,5 ммоль/л. Ни угадывания, ни деления: база в мг/дл.
        requests.post(f"{api}/entries", json=[sgv(99)], headers=auth(OTTAI_SECRET))

        assert readings(db)[0].mgdl == 99.0

    def test_a_small_value_is_not_taken_for_mmol(self):
        # 5.5 — это 5,5 мг/дл, то есть не сахар, а не 5,5 ммоль/л.
        assert parse_entries([sgv(5.5)], NOW) == ([], {"sgv": 1})

    def test_date_is_unix_milliseconds_in_utc(self):
        at = datetime(2026, 11, 2, 9, 30, 15, 750000)
        entry, _ = parse_entry(sgv(at=at), NOW)

        # DATETIME в MySQL без долей: секунда отрезана, а не округлена.
        assert entry.measured_at == datetime(2026, 11, 2, 9, 30, 15)

    def test_date_in_seconds_is_refused_not_guessed(self):
        entry, reason = parse_entry({"type": "sgv", "sgv": 120, "date": ms(NOW) // 1000}, NOW)

        assert entry is None and reason == "date"

    def test_date_string_with_an_offset_is_converted_to_utc(self):
        entry, _ = parse_entry({"type": "sgv", "sgv": 120, "dateString": "2026-11-02T14:00:00.000+03:00"}, NOW)

        assert entry.measured_at == datetime(2026, 11, 2, 11, 0, 0)

    def test_date_string_without_a_zone_is_refused(self):
        entry, reason = parse_entry({"type": "sgv", "sgv": 120, "dateString": "2026-11-02T14:00:00"}, NOW)

        assert entry is None and reason == "date"

    def test_date_wins_over_date_string(self):
        entry, _ = parse_entry(sgv(at=NOW, dateString="2020-01-01T00:00:00Z"), NOW)

        assert entry.measured_at == NOW

    def test_the_far_future_is_refused_and_small_skew_is_not(self):
        assert parse_entry(sgv(at=NOW + timedelta(minutes=5)), NOW)[0] is not None
        assert parse_entry(sgv(at=NOW + timedelta(hours=1)), NOW)[1] == "date"

    def test_measured_and_received_times_are_kept_apart(self, api, db):
        at = NOW - timedelta(hours=3)
        requests.post(f"{api}/entries", json=[sgv(at=at)], headers=auth(OTTAI_SECRET))

        row = readings(db)[0]
        assert row.timestamp == at
        assert row.received_at == NOW
        assert source(db, "ottai").last_received_at == NOW

    def test_the_answer_echoes_nightscout_fields(self, api):
        at = datetime(2026, 11, 2, 11, 55, 0)
        entry = requests.post(f"{api}/entries", json=[sgv(123, at)], headers=auth(OTTAI_SECRET)).json()[0]

        assert len(entry["_id"]) == 24
        assert entry["type"] == "sgv"
        assert entry["sgv"] == 123
        assert entry["date"] == ms(at)
        assert entry["dateString"] == "2026-11-02T11:55:00.000Z"
        assert entry["direction"] == "Flat"

    def test_an_unknown_direction_is_dropped_not_stored(self):
        assert parse_entry(sgv(direction="Sideways"), NOW)[0].direction is None

    def test_numeric_strings_are_read_as_numbers(self):
        entry, _ = parse_entry({"type": "sgv", "sgv": "140", "date": str(ms(NOW))}, NOW)

        assert entry.mgdl == 140.0 and entry.measured_at == NOW


# ── История и повторы ────────────────────────────────────────────────────


def history(hours=24, step=5):
    start = NOW - timedelta(hours=hours)
    return [sgv(100 + i % 50, start + timedelta(minutes=step * i)) for i in range(hours * 60 // step)]


class TestHistoryAndRepeats:
    def test_a_day_of_history_after_an_outage_lands_whole(self, api, db):
        batch = history()
        response = requests.post(f"{api}/entries", json=batch, headers=auth(OTTAI_SECRET))

        assert response.status_code == 200
        assert len(response.json()) == len(batch) == 288
        assert len(readings(db, "ottai")) == 288

    def test_reads_come_newest_first_and_respect_count(self, api):
        requests.post(f"{api}/entries", json=history(), headers=auth(OTTAI_SECRET))

        latest = requests.get(f"{api}/entries.json", params={"count": 3}, headers=auth(OTTAI_SECRET)).json()
        current = requests.get(f"{api}/entries/current.json", headers=auth(OTTAI_SECRET)).json()
        default = requests.get(f"{api}/entries.json", headers=auth(OTTAI_SECRET)).json()

        dates = [entry["date"] for entry in latest]
        assert dates == sorted(dates, reverse=True) and len(dates) == 3
        assert current[0]["date"] == dates[0]
        assert len(default) == 10

    @pytest.mark.parametrize(
        ("operator", "offset_ms", "expected"),
        [
            ("$gt", 0, 2),  # строго после 11:50 — 11:55 и 12:00
            ("$gte", 0, 3),
            ("$gt", 500, 2),  # после 11:50:00.500 — тоже с 11:55
            ("$gte", 500, 2),
        ],
    )
    def test_a_date_filter_narrows_the_read(self, api, operator, offset_ms, expected):
        requests.post(f"{api}/entries", json=history(hours=1), headers=auth(OTTAI_SECRET))
        # history(1) — точки с 11:00 по 11:55; добавим 12:00.
        requests.post(f"{api}/entries", json=[sgv(at=NOW)], headers=auth(OTTAI_SECRET))
        since = ms(NOW - timedelta(minutes=10)) + offset_ms

        found = requests.get(
            f"{api}/entries.json",
            params={f"find[date][{operator}]": since, "count": 1000},
            headers=auth(OTTAI_SECRET),
        ).json()

        assert len(found) == expected

    def test_sending_the_same_batch_again_adds_nothing(self, api, db):
        batch = history(hours=2)
        first = requests.post(f"{api}/entries", json=batch, headers=auth(OTTAI_SECRET)).json()
        second = requests.post(f"{api}/entries", json=batch, headers=auth(OTTAI_SECRET)).json()

        assert len(readings(db)) == len(batch)
        # Повтор отвечает теми же записями — с теми же _id.
        assert [entry["_id"] for entry in first] == [entry["_id"] for entry in second]

    def test_a_resend_keeps_the_first_value(self, api, db):
        requests.post(f"{api}/entries", json=[sgv(100)], headers=auth(OTTAI_SECRET))
        requests.post(f"{api}/entries", json=[sgv(180)], headers=auth(OTTAI_SECRET))

        assert [row.mgdl for row in readings(db)] == [100.0]

    def test_overlapping_batches_store_the_union(self, api, db):
        requests.post(f"{api}/entries", json=history(hours=2), headers=auth(OTTAI_SECRET))
        requests.post(f"{api}/entries", json=history(hours=3), headers=auth(OTTAI_SECRET))

        assert len(readings(db)) == 36

    def test_parallel_uploads_of_the_same_batch_store_it_once(self, api, db):
        batch = history(hours=6)

        def send(_):
            return requests.post(f"{api}/entries", json=batch, headers=auth(OTTAI_SECRET)).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(send, range(8)))

        assert codes == [200] * 8
        assert len(readings(db)) == len(batch)

    def test_gzip_bodies_are_accepted(self, api, db):
        body = gzip.compress(json.dumps(history(hours=1)).encode())
        headers = {**auth(OTTAI_SECRET), "Content-Encoding": "gzip", "Content-Type": "application/json"}

        assert requests.post(f"{api}/entries", data=body, headers=headers).status_code == 200
        assert len(readings(db)) == 12


# ── Негодные данные ──────────────────────────────────────────────────────


class TestInvalidInput:
    @pytest.mark.parametrize(
        "body",
        [b"not json", b"42", b'"sgv"', b"[1, 2]", b'[{"type": "sgv"}, "x"]', b'[{"type":"sgv","sgv":NaN}]'],
    )
    def test_a_malformed_body_is_a_400(self, api, db, body):
        response = requests.post(f"{api}/entries", data=body, headers=auth(OTTAI_SECRET))

        assert response.status_code == 400
        assert readings(db) == []

    def test_too_many_entries_is_a_400(self, api, db):
        body = [sgv(at=NOW - timedelta(seconds=i)) for i in range(MAX_ITEMS + 1)]
        response = requests.post(f"{api}/entries", json=body, headers=auth(OTTAI_SECRET))

        assert response.status_code == 400
        assert readings(db) == []

    def test_an_oversized_body_is_a_413(self, api):
        body = b"[" + b" " * MAX_BODY_BYTES + b"]"

        assert requests.post(f"{api}/entries", data=body, headers=auth(OTTAI_SECRET)).status_code == 413

    def test_a_gzip_bomb_is_a_413(self, api):
        body = gzip.compress(b"[" + b" " * (MAX_BODY_BYTES * 2) + b"]")
        headers = {**auth(OTTAI_SECRET), "Content-Encoding": "gzip"}

        assert requests.post(f"{api}/entries", data=body, headers=headers).status_code == 413

    def test_a_huge_integer_is_skipped_not_a_500(self, api, db):
        # Codex P2: float() от целого в сотни цифр бросает OverflowError. Это
        # пропуск записи, а не 500 на всю пачку, которую загрузчик повторял бы.
        huge = "9" * 400
        body = (
            f'[{{"type":"sgv","sgv":{huge},"date":{ms(NOW)}}},'
            f'{{"type":"sgv","sgv":120,"date":{huge}}},'
            f'{{"type":"sgv","sgv":110,"date":{ms(NOW - timedelta(minutes=5))}}}]'
        )
        response = requests.post(f"{api}/entries", data=body, headers=auth(OTTAI_SECRET))

        assert response.status_code == 200
        assert [row.mgdl for row in readings(db)] == [110.0]

    def test_other_entry_types_never_become_readings(self, api, db):
        body = [
            {"type": "mbg", "mbg": 120, "date": ms(NOW)},
            {"type": "cal", "slope": 1000, "date": ms(NOW)},
            {"type": "sensor", "sgv": 120, "date": ms(NOW)},
            {"sgv": 120, "date": ms(NOW)},
        ]
        response = requests.post(f"{api}/entries", json=body, headers=auth(OTTAI_SECRET))

        assert response.status_code == 200
        assert response.json() == []
        assert readings(db) == []
        # Принято нечего — значит, и «последнего приёма» не было.
        assert source(db, "ottai").last_received_at is None

    @pytest.mark.parametrize("value", [None, "abc", True, 0, 12, 601, 1e9, [], {}])
    def test_a_bad_sgv_value_is_skipped(self, value):
        assert parse_entries([sgv(value)], NOW) == ([], {"sgv": 1})

    @pytest.mark.parametrize("date", [None, "yesterday", True, 0, -1, 1e30, 10**400])
    def test_a_bad_date_is_skipped(self, date):
        assert parse_entries([{"type": "sgv", "sgv": 120, "date": date}], NOW) == ([], {"date": 1})

    def test_good_entries_survive_bad_neighbours(self, api):
        body = [sgv(110), sgv("bad"), {"type": "mbg", "mbg": 90, "date": ms(NOW)}]
        response = requests.post(f"{api}/entries", json=body, headers=auth(OTTAI_SECRET))

        assert response.status_code == 200
        assert [entry["sgv"] for entry in response.json()] == [110]

    def test_unknown_paths_and_methods(self, api):
        assert requests.get(f"{api}/treatments.json", headers=auth(OTTAI_SECRET)).status_code == 404
        assert requests.post(f"{api}/status.json", json={}).status_code == 405

    def test_a_bad_count_is_a_400(self, api):
        response = requests.get(f"{api}/entries.json", params={"count": "-5"}, headers=auth(OTTAI_SECRET))

        assert response.status_code == 400


def test_storage_errors_answer_500_without_leaking(db):
    def broken(*_args, **_kwargs):
        raise RuntimeError("mysql password=hunter2")

    server, url = serve(
        Backend(
            find_source=storage.find_source,
            store_entries=broken,
            list_entries=storage.list_entries,
            now=lambda: NOW,
        )
    )
    try:
        response = requests.post(f"{url}/entries", json=[sgv()], headers=auth(OTTAI_SECRET))
    finally:
        server.shutdown()
        server.server_close()

    assert response.status_code == 500
    assert "hunter2" not in response.text


# ── Реестр датчиков и основной ───────────────────────────────────────────


class TestPrimarySensor:
    def test_libre_is_primary_on_a_fresh_registry(self, db):
        assert queries.primary_sensor() == "libre"

    def test_ensure_libre_does_not_steal_a_choice_made_in_the_bot(self, db):
        set_primary(db, "ottai")
        with db() as session:
            session.execute(GlucoseSource.__table__.delete().where(GlucoseSource.sensor == "libre"))
            session.commit()

        queries.ensure_libre_source(NOW)

        assert queries.primary_sensor() == "ottai"
        assert source(db, "libre").is_primary is False

    def test_switching_primary_switches_what_is_read(self, api, db):
        queries.store_readings([(NOW, 150.0)], sensor="libre")
        requests.post(f"{api}/entries", json=[sgv(100)], headers=auth(OTTAI_SECRET))

        assert queries.last_readings(1, sensor=queries.primary_sensor()) == [(NOW, 150.0)]
        set_primary(db, "ottai")
        assert queries.last_readings(1, sensor=queries.primary_sensor()) == [(NOW, 100.0)]

    def test_without_the_registry_libre_is_assumed(self, monkeypatch):
        empty = sessionmaker(bind=create_engine("sqlite://"))
        monkeypatch.setattr("database.queries.session", empty)

        assert queries.primary_sensor() == "libre"
        assert queries.sources_overview() == []

    def test_the_overview_lists_every_sensor_with_its_last_upload(self, api, db):
        requests.post(f"{api}/entries", json=[sgv()], headers=auth(OTTAI_SECRET))

        overview = {sensor: (label, primary, received) for sensor, label, primary, received in queries.sources_overview()}
        assert overview["libre"] == ("Libre", True, None)
        assert overview["ottai"] == ("Ottai", False, NOW)


def test_libre_is_reserved_even_before_its_row_exists(db):
    with db() as session:
        session.execute(GlucoseSource.__table__.delete().where(GlucoseSource.sensor == "libre"))
        session.commit()

    with pytest.raises(storage.SourceExists):
        storage.issue_source("libre", "Libre", digest_of_secret("sneakySecret12345"), NOW)
