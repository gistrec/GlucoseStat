"""Приём показаний по Nightscout API v1 — без отдельного сервера Nightscout.

Приложение, читающее сенсор, получает адрес сервиса и секрет и шлёт показания
сюда, как шлёт их в Nightscout. Секрет называет датчик (``glucose_sources``),
и показания ложатся в общую ``glucose_readings`` с этим датчиком в ``sensor``.
Метрики страницы считаются по основному датчику; остальные — вторая кривая.

Поведение сверено с cgm-remote-monitor (lib/api/entries, lib/server/entries.js,
lib/authorization, lib/server/enclave.js), а не только со Swagger:

* ``api-secret`` — SHA-1 от секрета в hex, регистр не важен; так же
  принимается ``?secret=``. Сам секрет в заголовке Nightscout не принимает —
  и мы тоже. JWT-токены (``?token=``, ``Authorization: Bearer``) не
  поддерживаются.
* ``sgv`` — всегда мг/дл, независимо от единиц, в которых Nightscout
  показывает сахар; ``date`` — Unix-время в миллисекундах. Единицы по величине
  числа не угадываются: секунды вместо миллисекунд дают 1970 год и отказ.
* Тело — объект или массив объектов, не больше 10 000 записей.
* ``device`` принимается и не хранится: какой это датчик, говорит секрет.

Записи других типов (``mbg``, ``cal``…) принимаются без ошибки, чтобы
загрузчик не повторял пачку вечно, но показаниями не становятся.

Сервер — stdlib ``ThreadingHTTPServer`` за nginx: зависимостей у проекта и
так две, а запросов здесь — один в несколько минут.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import secrets
import string
import sys
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from database.nightscout import Sgv, Source, StoredReading


log = logging.getLogger("glucose.nightscout")

API_PREFIX = "/api/v1"

# Как MAX_BATCH_ITEMS в Nightscout (lib/api/shared/write-payload.js).
MAX_ITEMS = 10_000
# Запись sgv в JSON — около 200 байт, 10 000 записей — около 2 МБ. Запас
# вдвое, но не 50 МБ, как у bodyParser в Nightscout: нам больше и не нужно.
MAX_BODY_BYTES = 4 * 1024 * 1024

# Значения ниже 39 у Dexcom — коды ошибок сенсора, а не сахар; выше 600 не
# показывает ни один прибор.
SGV_MIN = 39
SGV_MAX = 600

# Раньше этой даты CGM-загрузчиков не было: число меньше почти наверняка
# секунды вместо миллисекунд, и честнее отказать, чем угадывать.
EARLIEST = datetime(2015, 1, 1)
# Часы телефона могут спешить; дальше этого — уже не спешка.
FUTURE_SKEW = timedelta(minutes=10)

DIRECTIONS = frozenset(
    {
        "NONE",
        "DoubleUp",
        "SingleUp",
        "FortyFiveUp",
        "Flat",
        "FortyFiveDown",
        "SingleDown",
        "DoubleDown",
        "NOT COMPUTABLE",
        "RATE OUT OF RANGE",
    }
)

DEFAULT_COUNT = 10
MAX_COUNT = 1000

SHA1_HEX = re.compile(r"^[0-9a-f]{40}$")

SECRET_ALPHABET = string.ascii_letters + string.digits
SECRET_LENGTH = 24  # ~143 бита; Nightscout требует не короче 12 символов.

DEFAULT_PUBLIC_URL = "https://glucose.gistrec.cloud"


class BadRequest(Exception):
    def __init__(self, status: int, message: str, description: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.description = description


# ── Секрет ───────────────────────────────────────────────────────────────


def new_secret() -> str:
    return "".join(secrets.choice(SECRET_ALPHABET) for _ in range(SECRET_LENGTH))


def api_secret_header(secret: str) -> str:
    """То, что загрузчик кладёт в ``api-secret``: SHA-1 от секрета в hex."""

    return hashlib.sha1(secret.encode("utf-8")).hexdigest()


def digest_of_header(header_value: str) -> str | None:
    """Дайджест для поиска в базе, или None, если это не SHA-1.

    Хранится SHA-256 от SHA-1: сам SHA-1 — уже действующий пароль к API, и
    класть его в таблицу значило бы держать там пароль открытым текстом.
    """

    value = header_value.strip().lower()
    if not SHA1_HEX.match(value):
        return None
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def digest_of_secret(secret: str) -> str:
    digest = digest_of_header(api_secret_header(secret))
    assert digest is not None
    return digest


# ── Разбор показаний ─────────────────────────────────────────────────────


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            number = float(value.strip())
        else:
            return None
    except (ValueError, OverflowError):
        # OverflowError — целое на сотни цифр: оно не число датчика, а пропуск
        # записи, и не 500 на всю пачку, которую загрузчик повторял бы вечно.
        return None
    return number if math.isfinite(number) else None


def _date_ms(entry: dict) -> int | None:
    """Момент измерения в миллисекундах UTC.

    Первым — ``date``: по нему Nightscout ищет и сортирует, и его же
    показывает. ``dateString`` — запасной путь, когда ``date`` нет или он негоден; в нём
    обязана быть зона, иначе неизвестно, чьё это время.
    """

    number = _number(entry.get("date"))
    if number is not None:
        return int(round(number))

    text = entry.get("dateString")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return int(round(moment.timestamp() * 1000))


def parse_entry(entry: dict, now: datetime) -> tuple[Sgv | None, str | None]:
    """Одна запись: (показание, None) или (None, причина пропуска)."""

    if entry.get("type") != "sgv":
        return None, "type"

    mgdl = _number(entry.get("sgv"))
    if mgdl is None or not SGV_MIN <= mgdl <= SGV_MAX:
        return None, "sgv"

    date_ms = _date_ms(entry)
    if date_ms is None:
        return None, "date"
    try:
        measured = datetime.fromtimestamp(date_ms / 1000, timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None, "date"
    if measured < EARLIEST or measured > now + FUTURE_SKEW:
        return None, "date"

    direction = entry.get("direction")
    if not isinstance(direction, str) or direction not in DIRECTIONS:
        direction = None

    return (
        Sgv(
            measured_at=measured.replace(microsecond=0),
            mgdl=mgdl,
            direction=direction,
        ),
        None,
    )


def parse_entries(body: Any, now: datetime) -> tuple[list[Sgv], dict[str, int]]:
    """Тело POST /entries → показания и счёт пропущенного по причинам.

    Форма тела — как в Nightscout: объект или массив объектов. Неверная форма
    отвергает весь запрос; негодная запись внутри верной пачки только
    пропускается — загрузчик иначе повторял бы пачку вечно, а повтор её не
    исправит.
    """

    if isinstance(body, dict):
        items = [body]
    elif isinstance(body, list):
        items = body
    else:
        raise BadRequest(400, "Invalid entries payload", "Expected an object or array of objects")

    if len(items) > MAX_ITEMS:
        raise BadRequest(400, "Too many entries", f"Maximum records per request: {MAX_ITEMS}")
    if any(not isinstance(item, dict) for item in items):
        raise BadRequest(400, "Invalid entries payload", "Expected an object or array of objects")

    accepted: list[Sgv] = []
    skipped: dict[str, int] = {}
    for item in items:
        sgv, reason = parse_entry(item, now)
        if sgv is None:
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            accepted.append(sgv)
    return accepted, skipped


def entry_json(sensor: str, reading: Sgv | StoredReading) -> dict:
    """Запись в форме Nightscout.

    Время в базе — до секунды, поэтому и ``date`` кратна тысяче. ``_id`` —
    24 hex-символа, как ObjectId у Nightscout: датчик и секунда измерения,
    то есть ключ строки, — повтор той же записи получает тот же ``_id``.
    """

    moment = reading.measured_at.replace(tzinfo=timezone.utc)
    seconds = int(moment.timestamp())
    iso = moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    value: int | float = int(reading.mgdl) if float(reading.mgdl).is_integer() else reading.mgdl
    prefix = hashlib.sha1(sensor.encode("utf-8")).hexdigest()[:12]
    result = {
        "_id": f"{prefix}{seconds:012x}",
        "type": "sgv",
        "sgv": value,
        "date": seconds * 1000,
        "dateString": iso,
        "sysTime": iso,
        "utcOffset": 0,
    }
    if reading.direction is not None:
        result["direction"] = reading.direction
    return result


# ── HTTP ─────────────────────────────────────────────────────────────────


@dataclass
class Backend:
    """Всё, что обработчику нужно от внешнего мира, — чтобы тесты могли подменить."""

    find_source: Callable[[str], Source | None]
    store_entries: Callable[[Source, list[Sgv], datetime], int]
    list_entries: Callable[..., list[StoredReading]]
    now: Callable[[], datetime]

    @classmethod
    def database(cls) -> "Backend":
        from database import nightscout as storage

        return cls(
            find_source=storage.find_source,
            store_entries=storage.store_entries,
            list_entries=storage.list_entries,
            now=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        )


def _route(path: str) -> str | None:
    """Путь запроса → имя маршрута. ``.json`` и завершающий / — как у Nightscout."""

    if not path.startswith(API_PREFIX + "/"):
        return None
    rest = path[len(API_PREFIX) :].rstrip("/")
    if rest.endswith(".json"):
        rest = rest[: -len(".json")]
    return {
        "/status": "status",
        "/verifyauth": "verifyauth",
        "/entries": "entries",
        "/entries/sgv": "entries",
        "/entries/current": "current",
    }.get(rest)


def _date_filter(query: dict[str, list[str]]) -> dict[str, Any]:
    """``find[date][$gt]=…`` и родня — то, чем загрузчики ищут, что уже есть."""

    result: dict[str, Any] = {}
    for operator, (key, inclusive) in {
        "$gt": ("after_ms", False),
        "$gte": ("after_ms", True),
        "$lt": ("before_ms", False),
        "$lte": ("before_ms", True),
    }.items():
        values = query.get(f"find[date][{operator}]")
        if not values:
            continue
        number = _number(values[0])
        if number is None:
            raise BadRequest(400, "Invalid query", f"find[date][{operator}] must be a number")
        result[key] = int(number)
        result["inclusive_after" if key == "after_ms" else "inclusive_before"] = inclusive
    return result


def _count(query: dict[str, list[str]]) -> int:
    values = query.get("count")
    if not values or not values[0].strip():
        return DEFAULT_COUNT
    # oref0 приклеивает учётные данные ко count вторым «?» — см. count.js.
    raw = values[0].split("?", 1)[0]
    if not raw.isdigit():
        raise BadRequest(400, "Invalid query", "count must be a non-negative integer")
    return min(int(raw), MAX_COUNT)


class Handler(BaseHTTPRequestHandler):
    server_version = "GlucoseStat-Nightscout"
    # Медленный клиент не держит поток дольше этого.
    timeout = 30
    backend: Backend  # задаётся в make_server

    # ── Журнал: никогда не пишем строку запроса — в ней бывает ?secret= ──

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        log.info("%s %s -> %s", self.command, urlsplit(self.path).path[:200], code)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        log.warning("malformed request rejected by http.server")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    # ── Ответы ──

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_status(self, status: int, message: str, description: str) -> None:
        # Форма ошибок — sendJSONStatus из Nightscout.
        self._send_json(status, {"status": status, "message": message, "description": description})

    # ── Авторизация ──

    def _source(self, query: dict[str, list[str]]) -> Source | None:
        supplied = self.headers.get("api-secret")
        if not supplied and query.get("secret"):
            supplied = query["secret"][0]
        if not supplied:
            return None
        digest = digest_of_header(supplied)
        if digest is None:
            return None
        return self.backend.find_source(digest)

    def _require_source(self, query: dict[str, list[str]]) -> Source | None:
        source = self._source(query)
        if source is None:
            self._send_status(401, "Unauthorized", "Invalid/Missing")
        return source

    # ── Тело ──

    def _read_json(self) -> Any:
        if self.headers.get("Transfer-Encoding"):
            raise BadRequest(411, "Length Required", "Send Content-Length")
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise BadRequest(411, "Length Required", "Send Content-Length")
        try:
            length = int(length_header)
        except ValueError:
            raise BadRequest(400, "Bad Request", "Invalid Content-Length") from None
        if length < 0:
            raise BadRequest(400, "Bad Request", "Invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise BadRequest(413, "Payload Too Large", f"Maximum body size: {MAX_BODY_BYTES} bytes")

        raw = self.rfile.read(length)
        if len(raw) != length:
            raise BadRequest(400, "Bad Request", "Body shorter than Content-Length")

        encoding = (self.headers.get("Content-Encoding") or "identity").strip().lower()
        if encoding == "gzip":
            raw = _gunzip(raw)
        elif encoding != "identity":
            raise BadRequest(415, "Unsupported Media Type", "Only gzip encoding is supported")

        try:
            return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise BadRequest(400, "Bad Request", "Body is not valid JSON") from None

    # ── Маршруты ──

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        route = _route(parts.path)
        query = parse_qs(parts.query, keep_blank_values=True)
        try:
            if route == "status" and method == "GET":
                self._status()
            elif route == "verifyauth" and method == "GET":
                self._verifyauth(query)
            elif route in ("entries", "current") and method == "GET":
                self._list(query, current=route == "current")
            elif route == "entries" and method == "POST":
                self._post(query)
            elif route is None:
                self._send_status(404, "Not Found", "No such endpoint")
            else:
                self._send_status(405, "Method Not Allowed", f"{method} is not supported here")
        except BadRequest as error:
            self._send_status(error.status, error.message, error.description)
        except Exception:
            log.exception("nightscout request failed")
            self._send_status(500, "Internal Server Error", "Storage error")

    def _status(self) -> None:
        now = datetime.now(timezone.utc)
        self._send_json(
            200,
            {
                "status": "ok",
                "name": "GlucoseStat",
                "version": "15.0.0",
                "serverTime": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "serverTimeEpoch": int(now.timestamp() * 1000),
                "apiEnabled": True,
                "careportalEnabled": False,
                "boluscalcEnabled": False,
                # Единицы показа, не хранения: sgv по API — всегда мг/дл.
                "settings": {"units": "mmol", "enable": [], "authDefaultRoles": ""},
                "extendedSettings": {},
                "authorized": None,
            },
        )

    def _verifyauth(self, query: dict[str, list[str]]) -> None:
        ok = self._source(query) is not None
        self._send_json(
            200,
            {
                "status": 200,
                "message": {
                    "canRead": ok,
                    "canWrite": ok,
                    "isAdmin": ok,
                    "message": "OK" if ok else "UNAUTHORIZED",
                    "rolefound": "NOTFOUND",
                    "permissions": "ROLE" if ok else "DEFAULT",
                },
            },
        )

    def _list(self, query: dict[str, list[str]], current: bool) -> None:
        source = self._require_source(query)
        if source is None:
            return
        wanted = query.get("find[type]")
        if wanted and wanted[0] != "sgv":
            self._send_json(200, [])
            return
        count = 1 if current else _count(query)
        if count == 0:
            self._send_json(200, [])
            return
        rows = self.backend.list_entries(source.sensor, count, **_date_filter(query))
        self._send_json(200, [entry_json(source.sensor, row) for row in rows])

    def _post(self, query: dict[str, list[str]]) -> None:
        # Авторизация до чтения тела: чужой запрос не стоит нам разбора 4 МБ.
        source = self._require_source(query)
        if source is None:
            return
        body = self._read_json()
        now = self.backend.now()
        accepted, skipped = parse_entries(body, now)
        added = self.backend.store_entries(source, accepted, now) if accepted else 0
        log.info(
            "sensor %s: %d sgv received, %d new, skipped %s",
            source.sensor,
            len(accepted),
            added,
            skipped or "nothing",
        )
        self._send_json(200, [entry_json(source.sensor, item) for item in accepted])


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def _gunzip(raw: bytes) -> bytes:
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = inflater.decompress(raw, MAX_BODY_BYTES + 1)
    except zlib.error:
        raise BadRequest(400, "Bad Request", "Invalid gzip body") from None
    if len(data) > MAX_BODY_BYTES or inflater.unconsumed_tail:
        raise BadRequest(413, "Payload Too Large", f"Maximum body size: {MAX_BODY_BYTES} bytes")
    return data


def make_server(host: str, port: int, backend: Backend) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"backend": backend})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


# ── CLI ──────────────────────────────────────────────────────────────────


def public_url() -> str:
    return os.getenv("NIGHTSCOUT_PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _print_credentials(secret: str) -> None:
    print(f"URL:        {public_url()}")
    print(f"API secret: {secret}")
    print("Секрет показан один раз и в базе не хранится. Потеряли — перевыпустите.")


def _serve() -> None:
    from database.connection import init_schema

    init_schema()
    host = os.getenv("NIGHTSCOUT_HOST", "127.0.0.1")
    port = int(os.getenv("NIGHTSCOUT_PORT", "8098"))
    server = make_server(host, port, Backend.database())
    log.info("nightscout api listening on %s:%d", host, port)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()

    parser = argparse.ArgumentParser(description="Nightscout API v1 для GlucoseStat")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="запустить HTTP-сервер (по умолчанию)")
    issue = commands.add_parser("issue", help="завести датчик и выдать ему секрет")
    issue.add_argument("sensor", type=_sensor_name, help="имя датчика: ottai, dexcom…")
    issue.add_argument("--label", help="подпись на графике, по умолчанию — имя с заглавной")
    issue.add_argument(
        "--secret-stdin",
        action="store_true",
        help="взять секрет из stdin вместо нового случайного",
    )
    rotate = commands.add_parser("rotate", help="перевыпустить секрет; прежний перестаёт работать")
    rotate.add_argument("sensor", type=_sensor_name)
    commands.add_parser("list", help="датчики и время последнего приёма")
    mark = commands.add_parser(
        "sensor", help="установка и срок датчика, который их не сообщает (Ottai)"
    )
    mark.add_argument("sensor", type=_sensor_name)
    mark.add_argument(
        "--started",
        required=True,
        type=_moment,
        help="когда поставлен, с поясом: 2026-11-02T19:53+03:00",
    )
    mark.add_argument("--days", required=True, type=int, help="срок в днях: 14")
    args = parser.parse_args(argv)

    if args.command in (None, "serve"):
        _serve()
        return 0

    from database import nightscout as storage
    from database.connection import init_schema

    init_schema()
    now = _utc_now()

    if args.command == "issue":
        secret = _secret_from_stdin() if args.secret_stdin else new_secret()
        label = args.label or args.sensor.capitalize()
        try:
            storage.issue_source(args.sensor, label, digest_of_secret(secret), now)
        except storage.SourceExists:
            print(f"Датчик {args.sensor!r} уже есть. Новый секрет: nightscout.py rotate {args.sensor}")
            return 1
        if args.secret_stdin:
            print(f"URL: {public_url()}\nСекрет принят, датчик {args.sensor!r} заведён.")
        else:
            _print_credentials(secret)
        return 0

    if args.command == "sensor":
        from database import queries

        if args.sensor not in {info.sensor for info in storage.list_sources()}:
            print(f"Датчика {args.sensor!r} нет — см. nightscout.py list")
            return 1
        if args.sensor == "libre":
            print("Срок Libre пишет сборщик по ответу Abbott — руками его не задают.")
            return 1
        ends = args.started + timedelta(days=args.days)
        queries.store_sensor_marks(args.sensor, args.started, ends)
        print(
            f"{args.sensor}: поставлен {args.started:%Y-%m-%d %H:%M} UTC, "
            f"срок до {ends:%Y-%m-%d %H:%M} UTC ({args.days} дн)"
        )
        return 0

    if args.command == "rotate":
        secret = new_secret()
        if not storage.rotate_secret(args.sensor, digest_of_secret(secret), now):
            print(f"Датчика {args.sensor!r} с приёмом по Nightscout нет — см. nightscout.py list")
            return 1
        _print_credentials(secret)
        return 0

    print(f"URL: {public_url()}")
    for info in storage.list_sources():
        last = info.last_received_at.isoformat(sep=" ") + " UTC" if info.last_received_at else "ещё не было"
        flag = " [основной]" if info.is_primary else ""
        print(f"{info.sensor} ({info.label}, {info.kind}){flag}: последний приём — {last}")
    return 0


SENSOR_NAME = re.compile(r"^[a-z0-9_-]{1,16}$")


def _sensor_name(value: str) -> str:
    if not SENSOR_NAME.match(value):
        raise argparse.ArgumentTypeError("имя датчика: a-z, 0-9, _ и -, до 16 символов")
    return value


def _moment(value: str) -> datetime:
    """ISO-время с поясом → наивный UTC, как всё в базе. Без пояса — отказ:
    «19:53» по Москве и по Белграду — разные часы."""

    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("нужно ISO-время: 2026-11-02T19:53+03:00") from None
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError("укажите пояс: 2026-11-02T19:53+03:00")
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _secret_from_stdin() -> str:
    secret = sys.stdin.readline().strip()
    if len(secret) < 12:
        raise SystemExit("Секрет короче 12 символов — Nightscout такой не примет.")
    return secret

if __name__ == "__main__":
    sys.exit(main())
