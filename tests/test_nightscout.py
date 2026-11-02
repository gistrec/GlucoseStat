"""Приём показаний по Nightscout API v1.

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
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database import nightscout as storage
from database.models import Base, GlucoseReading, NightscoutEntry, NightscoutUploader
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
NOW_MS = int(NOW.replace(tzinfo=timezone.utc).timestamp() * 1000)
ALICE_SECRET = "aliceSecretValue123456"
BOB_SECRET = "bobSecretValue12345678"


def ms(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp() * 1000)


def sgv(value=120, at=NOW, device="ottai", direction="Flat", **extra):
    entry = {"type": "sgv", "sgv": value, "date": ms(at), "direction": direction, "device": device}
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

    storage.issue_uploader("alice", digest_of_secret(ALICE_SECRET), True, NOW)
    storage.issue_uploader("bob", digest_of_secret(BOB_SECRET), False, NOW)
    return factory


@pytest.fixture
def api(db):
    backend = Backend(
        find_uploader=storage.find_uploader,
        store_entries=storage.store_entries,
        list_entries=storage.list_entries,
        now=lambda: NOW,
    )
    server = make_server("127.0.0.1", 0, backend)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/v1"
    server.shutdown()
    server.server_close()


def auth(secret):
    return {"api-secret": api_secret_header(secret)}


def rows(db, model, **where):
    with db() as session:
        query = select(model)
        for column, value in where.items():
            query = query.where(getattr(model, column) == value)
        return session.execute(query).scalars().all()


def uploader_id(db, owner):
    return rows(db, NightscoutUploader, owner=owner)[0].id


# ── Авторизация и изоляция ───────────────────────────────────────────────


class TestAuth:
    def test_no_secret_is_refused_and_nothing_is_stored(self, api, db):
        response = requests.post(f"{api}/entries", json=[sgv()])

        assert response.status_code == 401
        assert response.json() == {"status": 401, "message": "Unauthorized", "description": "Invalid/Missing"}
        assert rows(db, NightscoutEntry) == []

    def test_the_plain_secret_is_not_accepted(self, api, db):
        # Nightscout сравнивает только хеши: секрет как есть — не пароль.
        response = requests.post(f"{api}/entries", json=[sgv()], headers={"api-secret": ALICE_SECRET})

        assert response.status_code == 401

    def test_a_wrong_secret_is_refused(self, api):
        response = requests.post(f"{api}/entries", json=[sgv()], headers=auth("not-a-real-secret-x"))

        assert response.status_code == 401

    def test_sha1_is_case_insensitive(self, api):
        header = {"api-secret": api_secret_header(ALICE_SECRET).upper()}
        response = requests.post(f"{api}/entries", json=[sgv()], headers=header)

        assert response.status_code == 200

    def test_the_secret_query_parameter_works(self, api):
        response = requests.post(
            f"{api}/entries.json", params={"secret": api_secret_header(ALICE_SECRET)}, json=[sgv()]
        )

        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_a_rotated_secret_stops_working_at_once(self, api):
        new = "rotatedSecretValue1234"
        assert storage.rotate_secret("alice", digest_of_secret(new), NOW)

        assert requests.post(f"{api}/entries", json=[sgv()], headers=auth(ALICE_SECRET)).status_code == 401
        assert requests.post(f"{api}/entries", json=[sgv()], headers=auth(new)).status_code == 200

    def test_rotating_an_unknown_owner_changes_nothing(self, db):
        assert not storage.rotate_secret("mallory", digest_of_secret("whatever12345"), NOW)

    def test_the_database_holds_neither_the_secret_nor_its_sha1(self, db):
        stored = rows(db, NightscoutUploader, owner="alice")[0].secret_digest

        assert stored != ALICE_SECRET
        assert stored != api_secret_header(ALICE_SECRET)
        assert stored == hashlib.sha256(api_secret_header(ALICE_SECRET).encode()).hexdigest()

    def test_reading_needs_the_secret(self, api):
        assert requests.get(f"{api}/entries.json").status_code == 401
        assert requests.get(f"{api}/entries/current.json").status_code == 401

    def test_each_uploader_reads_only_its_own_entries(self, api, db):
        requests.post(f"{api}/entries", json=[sgv(100, NOW - timedelta(minutes=5))], headers=auth(ALICE_SECRET))
        requests.post(f"{api}/entries", json=[sgv(200, NOW)], headers=auth(BOB_SECRET))

        alice = requests.get(f"{api}/entries.json", headers=auth(ALICE_SECRET)).json()
        bob = requests.get(f"{api}/entries/sgv.json", headers=auth(BOB_SECRET)).json()

        assert [entry["sgv"] for entry in alice] == [100]
        assert [entry["sgv"] for entry in bob] == [200]

    def test_only_the_dashboard_owner_reaches_the_dashboard_curve(self, api, db):
        requests.post(f"{api}/entries", json=[sgv(100, NOW - timedelta(minutes=5))], headers=auth(ALICE_SECRET))
        requests.post(f"{api}/entries", json=[sgv(200, NOW)], headers=auth(BOB_SECRET))

        curve = [(row.timestamp, row.mgdl) for row in rows(db, GlucoseReading)]
        assert curve == [(NOW - timedelta(minutes=5), 100.0)]

    def test_the_same_moment_from_two_uploaders_stays_with_each(self, api, db):
        requests.post(f"{api}/entries", json=[sgv(100)], headers=auth(ALICE_SECRET))
        requests.post(f"{api}/entries", json=[sgv(200)], headers=auth(BOB_SECRET))

        assert [row.mgdl for row in rows(db, NightscoutEntry, uploader_id=uploader_id(db, "alice"))] == [100.0]
        assert [row.mgdl for row in rows(db, NightscoutEntry, uploader_id=uploader_id(db, "bob"))] == [200.0]

    def test_status_answers_without_a_secret(self, api):
        response = requests.get(f"{api}/status.json")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["apiEnabled"] is True
        assert isinstance(body["serverTimeEpoch"], int)

    def test_verifyauth_tells_a_good_secret_from_a_bad_one(self, api):
        good = requests.get(f"{api}/verifyauth", headers=auth(ALICE_SECRET)).json()["message"]
        bad = requests.get(f"{api}/verifyauth", headers=auth("nope-nope-nope")).json()["message"]

        assert good["message"] == "OK" and good["canWrite"] is True
        assert bad["message"] == "UNAUTHORIZED" and bad["canWrite"] is False

    def test_neither_the_secret_nor_its_hash_reaches_the_log(self, api, caplog):
        # Только журнал сервера: urllib3 клиента пишет свой URL целиком.
        caplog.set_level(logging.DEBUG, logger="glucose.nightscout")
        header = api_secret_header(ALICE_SECRET)
        requests.post(f"{api}/entries", params={"secret": header}, json=[sgv()])
        requests.post(f"{api}/entries", params={"secret": "wrongwrongwrong"}, json=[sgv()])

        text = "\n".join(
            record.getMessage() for record in caplog.records if record.name.startswith("glucose")
        )
        assert header not in text
        assert ALICE_SECRET not in text
        assert "wrongwrongwrong" not in text
        assert "POST /api/v1/entries -> 200" in text


# ── Единицы и время ──────────────────────────────────────────────────────


class TestUnitsAndTime:
    def test_sgv_is_stored_as_mgdl_without_conversion(self, api, db):
        # 99 мг/дл = 5,5 ммоль/л. Ни угадывания, ни деления: база в мг/дл.
        requests.post(f"{api}/entries", json=[sgv(99)], headers=auth(ALICE_SECRET))

        assert rows(db, NightscoutEntry)[0].mgdl == 99.0
        assert rows(db, GlucoseReading)[0].mgdl == 99.0

    def test_a_small_value_is_not_taken_for_mmol(self):
        # 5.5 — это 5,5 мг/дл, то есть не сахар, а не 5,5 ммоль/л.
        accepted, skipped = parse_entries([sgv(5.5)], NOW)

        assert accepted == []
        assert skipped == {"sgv": 1}

    def test_date_is_unix_milliseconds_in_utc(self):
        at = datetime(2026, 11, 2, 9, 30, 15, 750000)
        entry, _ = parse_entry(sgv(at=at), NOW)

        assert entry.date_ms == ms(at)
        # DATETIME в MySQL без долей: секунда отрезана, а не округлена.
        assert entry.measured_at == datetime(2026, 11, 2, 9, 30, 15)

    def test_date_in_seconds_is_refused_not_guessed(self):
        entry, reason = parse_entry({"type": "sgv", "sgv": 120, "date": ms(NOW) // 1000}, NOW)

        assert entry is None and reason == "date"

    def test_date_string_with_an_offset_is_converted_to_utc(self):
        entry, _ = parse_entry({"type": "sgv", "sgv": 120, "dateString": "2026-11-02T14:00:00.000+03:00"}, NOW)

        assert entry.measured_at == datetime(2026, 11, 2, 11, 0, 0)
        assert entry.date_ms == ms(datetime(2026, 11, 2, 11, 0, 0))

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
        requests.post(f"{api}/entries", json=[sgv(at=at)], headers=auth(ALICE_SECRET))

        row = rows(db, NightscoutEntry)[0]
        assert row.measured_at == at
        assert row.received_at == NOW

    def test_the_answer_echoes_nightscout_fields(self, api):
        at = datetime(2026, 11, 2, 11, 55, 0)
        body = requests.post(f"{api}/entries", json=[sgv(123, at)], headers=auth(ALICE_SECRET)).json()

        entry = body[0]
        assert len(entry["_id"]) == 24
        assert entry["type"] == "sgv"
        assert entry["sgv"] == 123
        assert entry["date"] == ms(at)
        assert entry["dateString"] == "2026-11-02T11:55:00.000Z"
        assert entry["direction"] == "Flat"
        assert entry["device"] == "ottai"

    def test_an_unknown_direction_is_dropped_not_stored(self):
        entry, _ = parse_entry(sgv(direction="Sideways"), NOW)

        assert entry.direction is None

    def test_numeric_strings_are_read_as_numbers(self):
        entry, _ = parse_entry({"type": "sgv", "sgv": "140", "date": str(ms(NOW))}, NOW)

        assert entry.mgdl == 140.0 and entry.measured_at == NOW


# ── История и повторы ────────────────────────────────────────────────────


def history(hours=24, step=5, device="ottai"):
    start = NOW - timedelta(hours=hours)
    return [sgv(100 + i % 50, start + timedelta(minutes=step * i), device) for i in range(hours * 60 // step)]


class TestHistoryAndRepeats:
    def test_a_day_of_history_after_an_outage_lands_whole(self, api, db):
        batch = history()
        response = requests.post(f"{api}/entries", json=batch, headers=auth(ALICE_SECRET))

        assert response.status_code == 200
        assert len(response.json()) == len(batch) == 288
        assert len(rows(db, NightscoutEntry)) == 288
        assert len(rows(db, GlucoseReading)) == 288
        assert rows(db, NightscoutUploader, owner="alice")[0].last_upload_at == NOW

    def test_reads_come_newest_first_and_respect_count(self, api):
        requests.post(f"{api}/entries", json=history(), headers=auth(ALICE_SECRET))

        latest = requests.get(f"{api}/entries.json", params={"count": 3}, headers=auth(ALICE_SECRET)).json()
        current = requests.get(f"{api}/entries/current.json", headers=auth(ALICE_SECRET)).json()
        default = requests.get(f"{api}/entries.json", headers=auth(ALICE_SECRET)).json()

        dates = [entry["date"] for entry in latest]
        assert dates == sorted(dates, reverse=True) and len(dates) == 3
        assert current[0]["date"] == dates[0]
        assert len(default) == 10

    def test_a_date_filter_narrows_the_read(self, api):
        requests.post(f"{api}/entries", json=history(), headers=auth(ALICE_SECRET))
        since = ms(NOW - timedelta(minutes=30))

        found = requests.get(
            f"{api}/entries.json",
            params={"find[date][$gt]": since, "count": 1000},
            headers=auth(ALICE_SECRET),
        ).json()

        assert found and all(entry["date"] > since for entry in found)

    def test_sending_the_same_batch_again_adds_nothing(self, api, db):
        batch = history(hours=2)
        first = requests.post(f"{api}/entries", json=batch, headers=auth(ALICE_SECRET)).json()
        second = requests.post(f"{api}/entries", json=batch, headers=auth(ALICE_SECRET)).json()

        assert len(rows(db, NightscoutEntry)) == len(batch)
        assert len(rows(db, GlucoseReading)) == len(batch)
        # Повтор отвечает теми же записями — с теми же _id.
        assert [entry["_id"] for entry in first] == [entry["_id"] for entry in second]

    def test_overlapping_batches_store_the_union(self, api, db):
        requests.post(f"{api}/entries", json=history(hours=2), headers=auth(ALICE_SECRET))
        requests.post(f"{api}/entries", json=history(hours=3), headers=auth(ALICE_SECRET))

        assert len(rows(db, NightscoutEntry)) == 36

    def test_parallel_uploads_of_the_same_batch_store_it_once(self, api, db):
        batch = history(hours=6)

        def send(_):
            return requests.post(f"{api}/entries", json=batch, headers=auth(ALICE_SECRET)).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(send, range(8)))

        assert codes == [200] * 8
        assert len(rows(db, NightscoutEntry)) == len(batch)
        assert len(rows(db, GlucoseReading)) == len(batch)

    def test_the_same_moment_from_two_devices_is_two_entries_but_one_point(self, api, db):
        requests.post(
            f"{api}/entries",
            json=[sgv(120, device="phone"), sgv(121, device="watch")],
            headers=auth(ALICE_SECRET),
        )

        assert len(rows(db, NightscoutEntry)) == 2
        assert [row.mgdl for row in rows(db, GlucoseReading)] == [120.0]

    def test_a_missing_device_still_deduplicates(self, api, db):
        entry = {"type": "sgv", "sgv": 110, "date": ms(NOW)}
        requests.post(f"{api}/entries", json=entry, headers=auth(ALICE_SECRET))
        requests.post(f"{api}/entries", json=entry, headers=auth(ALICE_SECRET))

        assert len(rows(db, NightscoutEntry)) == 1

    def test_gzip_bodies_are_accepted(self, api, db):
        body = gzip.compress(json.dumps(history(hours=1)).encode())
        headers = {**auth(ALICE_SECRET), "Content-Encoding": "gzip", "Content-Type": "application/json"}

        assert requests.post(f"{api}/entries", data=body, headers=headers).status_code == 200
        assert len(rows(db, NightscoutEntry)) == 12


# ── Негодные данные ──────────────────────────────────────────────────────


class TestInvalidInput:
    @pytest.mark.parametrize(
        "body",
        [b"not json", b"42", b'"sgv"', b"[1, 2]", b'[{"type": "sgv"}, "x"]', b'[{"type":"sgv","sgv":NaN}]'],
    )
    def test_a_malformed_body_is_a_400(self, api, db, body):
        response = requests.post(f"{api}/entries", data=body, headers=auth(ALICE_SECRET))

        assert response.status_code == 400
        assert rows(db, NightscoutEntry) == []

    def test_too_many_entries_is_a_400(self, api, db):
        body = [sgv(at=NOW - timedelta(seconds=i)) for i in range(MAX_ITEMS + 1)]
        response = requests.post(f"{api}/entries", json=body, headers=auth(ALICE_SECRET))

        assert response.status_code == 400
        assert rows(db, NightscoutEntry) == []

    def test_an_oversized_body_is_a_413(self, api):
        body = b"[" + b" " * MAX_BODY_BYTES + b"]"
        response = requests.post(f"{api}/entries", data=body, headers=auth(ALICE_SECRET))

        assert response.status_code == 413

    def test_a_gzip_bomb_is_a_413(self, api):
        body = gzip.compress(b"[" + b" " * (MAX_BODY_BYTES * 2) + b"]")
        headers = {**auth(ALICE_SECRET), "Content-Encoding": "gzip"}

        assert requests.post(f"{api}/entries", data=body, headers=headers).status_code == 413

    def test_other_entry_types_never_become_readings(self, api, db):
        body = [
            {"type": "mbg", "mbg": 120, "date": ms(NOW)},
            {"type": "cal", "slope": 1000, "date": ms(NOW)},
            {"type": "sensor", "sgv": 120, "date": ms(NOW)},
            {"sgv": 120, "date": ms(NOW)},
        ]
        response = requests.post(f"{api}/entries", json=body, headers=auth(ALICE_SECRET))

        assert response.status_code == 200
        assert response.json() == []
        assert rows(db, NightscoutEntry) == []
        assert rows(db, GlucoseReading) == []
        # Принято нечего — значит, и «последнего приёма» не было.
        assert rows(db, NightscoutUploader, owner="alice")[0].last_upload_at is None

    @pytest.mark.parametrize("value", [None, "abc", True, 0, 12, 601, 1e9, [], {}])
    def test_a_bad_sgv_value_is_skipped(self, value):
        assert parse_entries([sgv(value)], NOW) == ([], {"sgv": 1})

    @pytest.mark.parametrize("date", [None, "yesterday", True, 0, -1, 1e30])
    def test_a_bad_date_is_skipped(self, date):
        entry = {"type": "sgv", "sgv": 120, "date": date}

        assert parse_entries([entry], NOW) == ([], {"date": 1})

    def test_good_entries_survive_bad_neighbours(self, api, db):
        body = [sgv(110), sgv("bad"), {"type": "mbg", "mbg": 90, "date": ms(NOW)}]
        response = requests.post(f"{api}/entries", json=body, headers=auth(ALICE_SECRET))

        assert response.status_code == 200
        assert [entry["sgv"] for entry in response.json()] == [110]

    def test_a_long_device_name_is_cut(self):
        entry, _ = parse_entry(sgv(device="x" * 500), NOW)

        assert entry.device == "x" * 64

    def test_unknown_paths_and_methods(self, api):
        assert requests.get(f"{api}/treatments.json", headers=auth(ALICE_SECRET)).status_code == 404
        assert requests.post(f"{api}/status.json", json={}).status_code == 405

    def test_a_bad_count_is_a_400(self, api):
        response = requests.get(f"{api}/entries.json", params={"count": "-5"}, headers=auth(ALICE_SECRET))

        assert response.status_code == 400

    def test_dashboard_last_upload_is_the_owners(self, api, db):
        requests.post(f"{api}/entries", json=[sgv()], headers=auth(BOB_SECRET))
        assert storage.read_dashboard_last_upload() is None

        requests.post(f"{api}/entries", json=[sgv()], headers=auth(ALICE_SECRET))
        assert storage.read_dashboard_last_upload() == NOW


def test_storage_errors_answer_500_without_leaking(db, caplog):
    def broken(*_args, **_kwargs):
        raise RuntimeError("mysql password=hunter2")

    backend = Backend(
        find_uploader=storage.find_uploader,
        store_entries=broken,
        list_entries=storage.list_entries,
        now=lambda: NOW,
    )
    server = make_server("127.0.0.1", 0, backend)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/v1/entries"
        response = requests.post(url, json=[sgv()], headers=auth(ALICE_SECRET))
    finally:
        server.shutdown()
        server.server_close()

    assert response.status_code == 500
    assert "hunter2" not in response.text

