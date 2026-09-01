"""Local preview of the dashboard — before it goes anywhere near the server.

The page is static and reads a single ``data.json``, so previewing it needs no
database and no LibreLinkUp: this script writes a snapshot into a throwaway copy
of ``web/`` and serves that.

```bash
./venv/bin/python preview.py                 # синтетика, открыть в браузере
./venv/bin/python preview.py --real          # настоящий web/data.json
./venv/bin/python preview.py --shot shots/   # снимки обеих тем, без браузера
```

Синтетика важнее, чем кажется: в настоящем ``data.json`` событий может не быть
вовсе — журнал ведёт бот, и до его выкладки страница выглядит ровно так же, как
до всех правок. Проверять дорожки и разбор на таком снимке нечем.
"""

import argparse
import http.server
import json
import math
import os
import random
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import webbrowser
from datetime import datetime, timedelta, timezone

from publish import PUBLISH_PATH, build_snapshot


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
ASSETS = ("index.html", "app.js", "style.css", "favicon.svg", "apple-touch-icon.png")

DEFAULT_PORT = 8765

# Распорядок дня, по которому строится синтетика: час, минута, углеводы, единицы.
MEAL_PLAN = [
    (8, 0, 45.0, 5.0),
    (13, 0, 62.0, 6.5),
    (16, 30, 20.0, 2.0),
    (19, 0, 70.0, 7.0),
]

ORIGIN_PLAN = [
    {"source": "photo_estimate", "was_weighed": False, "median": 60.0, "spread": 3.0},
    {"source": "photo_estimate", "was_weighed": False, "median": 60.0, "spread": 9.0},
    {"source": "photo_estimate", "was_weighed": False, "median": 60.0, "spread": 25.0},
    {"source": "photo_estimate", "was_weighed": True, "median": 60.0, "spread": 25.0},
    {"source": "manual", "was_weighed": None, "median": None, "spread": None},
]

DAYS = 16
BASAL_HOUR = 22
BASAL_UNITS = 18.0

# Куда искать безголовый браузер. Первым — кэш Playwright: если им когда-либо
# пользовались, бинарник уже лежит и качать нечего.
BROWSER_CANDIDATES = (
    "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
)

SHOT_WIDTH = 920
# Запас по высоте для первого прохода: настоящая высота страницы измеряется
# в браузере и подставляется вторым.
SHOT_PROBE_HEIGHT = 1200
SHOT_MAX_HEIGHT = 6000


def synthetic_snapshot(now: datetime | None = None) -> dict:
    """Build a believable fortnight: a wandering curve with meals stamped on it."""

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    # Округление до пяти минут — не косметика. Сетка показаний строится шагом в
    # пять минут от «сейчас», а приёмы пищи стоят на круглых минутах: при now
    # вида 21:52 моменты еды не попадают ни в одну точку сетки, подъёмы не
    # накладываются, и синтетика выходит гладкой кривой без единого события.
    now = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    # Фиксированное зерно: снимки двух запусков должны отличаться правками в
    # коде, а не новым случайным сахаром.
    random.seed(7)

    start = now - timedelta(days=DAYS - 1)
    curve: dict[datetime, float] = {}
    for step in range(0, int((now - start).total_seconds() // 60), 5):
        moment = start + timedelta(minutes=step)
        curve[moment] = 115 + 18 * math.sin(step / 190.0) + random.gauss(0, 5)

    journal: list[tuple[datetime, str, float | None, float | None]] = []
    origins: dict[datetime, list[dict]] = {}

    for day in range(DAYS):
        midnight = (start + timedelta(days=day)).replace(hour=0, minute=0)

        for index, (hour, minute, carbs, units) in enumerate(MEAL_PLAN):
            eaten = midnight + timedelta(hours=hour, minutes=minute)
            if not (start <= eaten <= now):
                continue

            # Раз в неделю приём пищи выходит из-под контроля, раз в одиннадцать
            # дней — уводит в гипогликемию. Ровная синтетика показала бы разбор
            # с одними «в ориентире», то есть не показала бы ничего.
            wild = (day + index) % 7 == 0
            amplitude = carbs * (1.6 if wild else 0.85) + random.gauss(0, 6)
            peak_minutes = random.choice([75, 90, 105])

            journal.append((eaten, "meal", round(carbs + random.gauss(0, 6), 1), None))
            origins[eaten] = [ORIGIN_PLAN[(day + index) % len(ORIGIN_PLAN)]]
            journal.append(
                (
                    eaten - timedelta(minutes=15),
                    "bolus",
                    None,
                    round(units + random.gauss(0, 0.5), 1),
                )
            )

            for step in range(0, 300, 5):
                moment = eaten + timedelta(minutes=step)
                if moment in curve:
                    curve[moment] += amplitude * math.exp(
                        -(((step - peak_minutes) / 70.0) ** 2)
                    )

            if (day + index) % 11 == 0:
                for step in range(180, 260, 5):
                    moment = eaten + timedelta(minutes=step)
                    if moment in curve:
                        curve[moment] -= 55

        night = midnight + timedelta(hours=BASAL_HOUR)
        if start <= night <= now:
            journal.append((night, "basal", None, BASAL_UNITS))

    # Сенсор не отдаёт значений вне своей шкалы — синтетика тоже не должна.
    readings = [
        (moment, max(45.0, min(360.0, value))) for moment, value in sorted(curve.items())
    ]
    journal.sort()

    # Снимок собирает publish, а не этот файл. Своя сборка молча расходилась бы
    # с боевой при каждом новом ключе — так превью и проглядело gmi.
    return build_snapshot(
        readings,
        journal,
        now,
        last_success=now.replace(tzinfo=timezone.utc).timestamp(),
        origins=origins,
    )


def build_site(target: str, snapshot: dict) -> None:
    """Copy ``web/`` next to the snapshot, plus a dark-theme entry point."""

    os.makedirs(target, exist_ok=True)
    for name in ASSETS:
        source = os.path.join(WEB_DIR, name)
        if os.path.exists(source):
            shutil.copy(source, os.path.join(target, name))

    with open(os.path.join(target, "data.json"), "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, separators=(",", ":"), ensure_ascii=False)

    # Тёмная тема выбирается кнопкой и живёт в localStorage, которого у свежего
    # профиля браузера нет. Отдельная точка входа кладёт выбор до того, как его
    # прочитает страница, — иначе снять тёмный снимок нечем.
    with open(os.path.join(target, "index.html"), encoding="utf-8") as handle:
        html = handle.read()

    dark = html.replace(
        'const saved = localStorage.getItem("theme");',
        'localStorage.setItem("theme", "dark"); const saved = "dark";',
    )
    with open(os.path.join(target, "dark.html"), "w", encoding="utf-8") as handle:
        handle.write(dark)

    # Страница сама сообщает свою высоту: она зависит от числа разобранных
    # приёмов пищи, и угадывать её — значит либо резать таблицу, либо оставлять
    # под ней полосу пустоты.
    probe = html.replace(
        "</body>",
        "<script>setTimeout(() => {"
        "const mark = document.createElement('div');"
        "mark.id = 'page-height';"
        "mark.textContent = document.body.scrollHeight;"
        "document.body.append(mark);"
        "}, 1200);</script></body>",
    )
    with open(os.path.join(target, "probe.html"), "w", encoding="utf-8") as handle:
        handle.write(probe)


def serve(directory: str, port: int) -> socketserver.TCPServer:
    handler = type(
        "Handler",
        (http.server.SimpleHTTPRequestHandler,),
        {
            "__init__": lambda self, *a, **kw: http.server.SimpleHTTPRequestHandler.__init__(
                self, *a, directory=directory, **kw
            ),
            # Тишина в консоли: строка на каждый запрос за фоном полезного вывода.
            "log_message": lambda self, *a: None,
        },
    )

    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def find_browser() -> str | None:
    import glob

    for pattern in BROWSER_CANDIDATES:
        matches = sorted(glob.glob(os.path.expanduser(pattern)))
        if matches:
            return matches[-1]
    return shutil.which("chromium") or shutil.which("google-chrome")


def page_height(browser: str, base: str) -> int:
    """Ask the page how tall it actually is, so the shot has no dead space."""

    dumped = subprocess.run(
        [
            browser,
            "--headless",
            "--disable-gpu",
            f"--window-size={SHOT_WIDTH},{SHOT_PROBE_HEIGHT}",
            "--virtual-time-budget=6000",
            "--dump-dom",
            base + "probe.html",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    ).stdout

    found = re.search(r'id="page-height">(\d+)<', dumped)
    if found is None:
        return SHOT_MAX_HEIGHT

    # Небольшой запас снизу: scrollHeight не учитывает нижний отступ страницы.
    return min(SHOT_MAX_HEIGHT, int(found.group(1)) + 24)


def shoot(browser: str, url: str, path: str, height: int) -> None:
    subprocess.run(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={SHOT_WIDTH},{height}",
            "--virtual-time-budget=6000",
            f"--screenshot={path}",
            url,
        ],
        capture_output=True,
        timeout=90,
    )
    print(f"  {path} ({SHOT_WIDTH}×{height})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--real",
        action="store_true",
        help="взять настоящий web/data.json вместо синтетики",
    )
    parser.add_argument(
        "--shot",
        metavar="DIR",
        help="снять страницу в обеих темах и выйти, не открывая браузер",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    if args.real:
        with open(PUBLISH_PATH, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        events = snapshot.get("events") or {}
        if not any(events.values()):
            print("Внимание: в настоящем снимке событий нет — дорожки не появятся.")
    else:
        snapshot = synthetic_snapshot()

    target = tempfile.mkdtemp(prefix="glucose-preview-")
    build_site(target, snapshot)

    server = serve(target, args.port)
    url = f"http://127.0.0.1:{args.port}/"

    try:
        if args.shot:
            browser = find_browser()
            if browser is None:
                sys.exit(
                    "Безголовый браузер не найден. Открой страницу вручную: " + url
                )

            os.makedirs(args.shot, exist_ok=True)
            print(f"Снимаю через {os.path.basename(browser)}:")
            height = page_height(browser, url)
            shoot(browser, url, os.path.join(args.shot, "light.png"), height)
            shoot(browser, url + "dark.html", os.path.join(args.shot, "dark.png"), height)
            return

        print(f"Превью на {url}  (Ctrl+C — остановить)")
        print(f"Тёмная тема: {url}dark.html")
        webbrowser.open(url)
        threading.Event().wait()
    except KeyboardInterrupt:
        print()
    finally:
        server.shutdown()
        shutil.rmtree(target, ignore_errors=True)


if __name__ == "__main__":
    main()
