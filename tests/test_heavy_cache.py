"""Снимок из двух частей: живая каждую минуту, тяжёлая — из кеша.

Сборщик раньше пересобирал квартал на каждом опросе — около четырёх секунд CPU
в минуту. Теперь тяжёлая часть живёт в ``HeavyCache``, а живая читает только
своё окно. Проверяется главное: страница не должна заметить разницы — живая
часть на коротком срезе совпадает с собой на полном сырье, а снимок из кеша —
со снимком, собранным целиком.
"""

import json
import math
from datetime import datetime, timedelta, timezone

import pytest
from conftest import BASE

import publish as publish_module
from publish import (
    HEAVY_INTERVAL,
    HeavyCache,
    build_heavy,
    build_live,
    build_snapshot,
    full_since,
    live_since,
    merge_snapshot,
    publish,
)


def history(now: datetime, days: int = 100) -> list[tuple[datetime, float]]:
    """Сырьё с шагом в пять минут, со сдвигом в полшага от ``now``: граница
    окна ни у какого среза не ложится ровно на замер, и миллисекунды между
    двумя вызовами ``publish`` ничего не переворачивают. И с дырами — под
    полосы «нет сигнала» и полноту сенсора."""

    start = now - timedelta(days=days) + timedelta(minutes=2, seconds=30)
    points = []
    for index in range(days * 288):
        moment = start + timedelta(minutes=5 * index)
        if moment > now:
            break
        # Час тишины раз в трое суток.
        if index % 864 < 12:
            continue
        mgdl = 130 + 70 * math.sin(index / 40) + 25 * math.sin(index / 7)
        points.append((moment, round(mgdl, 1)))
    return points


def journal(now: datetime) -> list[tuple[datetime, str, float | None, float | None]]:
    entries = []
    for day in range(14):
        for hour, carbs in ((8, 45.0), (13, 70.0), (19, 60.0)):
            meal = now - timedelta(days=day, hours=hour)
            entries.append((meal - timedelta(minutes=10), "bolus", None, carbs / 10))
            entries.append((meal, "meal", carbs, None))
    return sorted(entries)


def live_args(
    now: datetime, sensor_started: datetime, readings: list[tuple[datetime, float]]
) -> dict:
    """Сверки глюкометром раз в сутки, на 20 мг/дл ниже сенсора: смещение
    обязано выйти не ``None`` — иначе потеря первых дней сенсора ничего не
    изменит в ответе, и тест на границу окна её не заметит."""

    fingersticks = []
    for day in range(16):
        moment = sensor_started + timedelta(days=day, hours=3)
        near = [item for item in readings if abs(item[0] - moment) <= timedelta(minutes=3)]
        if near and moment < now:
            fingersticks.append((near[0][0], near[0][1] - 20))
    return {
        "last_success": now.replace(tzinfo=timezone.utc).timestamp(),
        "fingersticks": fingersticks,
        "sensor_started": sensor_started,
        "sensor_ends": sensor_started + timedelta(days=15),
    }


class TestParts:
    NOW = BASE + timedelta(days=70)

    @pytest.mark.parametrize("sensor_age", [timedelta(days=3), timedelta(days=14, hours=20)])
    def test_the_live_part_needs_no_more_than_its_window(self, sensor_age):
        """Короче ``live_since`` — и живая часть даёт другой ответ: это граница,
        ниже которой резать сырьё нельзя. Сенсор старше двух недель проверяет,
        что сверка с глюкометром (``_bias``) не теряет первых дней сенсора."""

        now = self.NOW
        sensor_started = now - sensor_age
        everything = history(now)
        since = live_since(now, sensor_started)
        sliced = [item for item in everything if item[0] >= since]
        events = journal(now)
        args = live_args(now, sensor_started, everything)

        assert build_live(sliced, events, now, **args) == build_live(
            everything, events, now, **args
        )

    def test_the_parts_do_not_overlap_but_on_series_and_stats(self):
        """Ключ, оказавшийся в обеих частях, молча брался бы из живой — и
        тяжёлая считала бы его зря."""

        now = self.NOW
        everything = history(now)
        heavy = build_heavy(everything, journal(now), now)
        live = build_live(everything, journal(now), now)

        assert set(heavy) & set(live) == {"series", "stats"}
        assert set(heavy["series"]).isdisjoint(live["series"])
        assert set(heavy["stats"]).isdisjoint(live["stats"])

    def test_the_merge_is_the_snapshot(self):
        now = self.NOW
        everything = history(now)
        events = journal(now)

        merged = merge_snapshot(
            build_heavy(everything, events, now), build_live(everything, events, now)
        )

        assert merged == build_snapshot(everything, events, now)
        assert list(merged["series"]) == list(build_snapshot(everything, events, now)["series"])


class TestHeavyCache:
    def test_it_expires_after_the_interval_and_on_a_new_key(self):
        cache = HeavyCache()
        now = BASE
        cache.put(now, ("a",), {"part": 1})

        assert cache.get(now + HEAVY_INTERVAL - timedelta(seconds=1), ("a",)) == {"part": 1}
        assert cache.get(now + HEAVY_INTERVAL, ("a",)) is None
        assert cache.get(now, ("b",)) is None
        # Часы ушли назад — возраст ничего не значит.
        assert cache.get(now - timedelta(minutes=1), ("a",)) is None


class TestPublishWithCache:
    @pytest.fixture
    def db(self, monkeypatch):
        """База в памяти теста: часы в ``publish`` стоят: иначе тяжёлая часть
        из кеша, собранная секундой раньше, честно отличалась бы от полной
        границей обрезанного дня. Журнал читается из ``state`` — его тест меняет."""

        now = BASE + timedelta(days=70)

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.replace(tzinfo=timezone.utc).astimezone(tz)

        monkeypatch.setattr("publish.datetime", Frozen)
        sensor_started = now - timedelta(days=15, hours=2)
        everything = history(now + timedelta(minutes=1))
        state = {"journal": journal(now), "origins": {}, "reads": [], "builds": 0}

        def readings_since(since):
            state["reads"].append(since)
            return [item for item in everything if item[0] >= since]

        real_build_heavy = publish_module.build_heavy

        def build_heavy(*args, **kwargs):
            state["builds"] += 1
            return real_build_heavy(*args, **kwargs)

        fingersticks = live_args(now, sensor_started, everything)["fingersticks"]
        monkeypatch.setattr("publish.readings_since", readings_since)
        monkeypatch.setattr("publish.journal_since", lambda since: list(state["journal"]))
        monkeypatch.setattr("publish.meal_origins_since", lambda since: dict(state["origins"]))
        monkeypatch.setattr("publish.build_heavy", build_heavy)
        monkeypatch.setattr(
            "publish.fingersticks_since",
            lambda since: [item for item in fingersticks if item[0] >= since],
        )
        monkeypatch.setattr("publish.last_readings", lambda limit=10: everything[-limit:])
        monkeypatch.setattr("publish.read_sensor_start", lambda: state["sensor_started"])
        monkeypatch.setattr("publish.read_sensor_end", lambda: None)
        monkeypatch.setattr("publish.latest_forecast", list)
        monkeypatch.setattr("publish.timezone_history", list)
        monkeypatch.setattr("publish.sensor_starts_since", lambda since: [sensor_started])
        state["sensor_started"] = sensor_started
        state["now"] = now
        return state

    @staticmethod
    def snapshot(tmp_path, name: str, cache: HeavyCache | None) -> dict:
        path = tmp_path / name / "data.json"
        path.parent.mkdir()
        publish(path=str(path), last_success=1.0, cache=cache)
        snapshot = json.loads(path.read_text())
        snapshot.pop("generated_at")
        return snapshot

    def test_the_second_publish_reads_only_the_live_window(self, tmp_path, db):
        cache = HeavyCache()

        self.snapshot(tmp_path, "first", cache)
        self.snapshot(tmp_path, "second", cache)

        assert db["reads"] == [
            full_since(db["now"]),
            live_since(db["now"], db["sensor_started"]),
        ]
        assert db["builds"] == 1

    def test_the_cached_snapshot_is_the_full_one(self, tmp_path, db):
        cache = HeavyCache()
        self.snapshot(tmp_path, "warm", cache)

        cached = self.snapshot(tmp_path, "cached", cache)
        full = self.snapshot(tmp_path, "full", None)

        assert cached == full

    def test_a_journal_change_rebuilds_at_once(self, tmp_path, db):
        """Еду, записанную или удалённую в боте, разбор обязан увидеть сразу."""

        cache = HeavyCache()
        self.snapshot(tmp_path, "first", cache)

        db["journal"] = db["journal"][:-1]
        self.snapshot(tmp_path, "second", cache)

        assert db["builds"] == 2

    def test_a_new_confidence_rebuilds_at_once(self, tmp_path, db):
        """Уверенность и взвешенность бот меняет, не трогая журнал, — а разбор
        ставит по ним еде доверие. Ждать четверть часа он не должен."""

        cache = HeavyCache()
        self.snapshot(tmp_path, "first", cache)

        meal = next(moment for moment, kind, _, _ in db["journal"] if kind == "meal")
        db["origins"] = {meal: [{"source": "manual", "confidence": 3}]}
        self.snapshot(tmp_path, "second", cache)

        assert db["builds"] == 2

    def test_a_long_gone_sensor_does_not_widen_the_live_read(self, tmp_path, db):
        """Отметка установки не стирается со снятием сенсора. Живая часть
        не должна от неё читать всё больше истории — не дальше полного окна."""

        db["sensor_started"] = db["now"] - timedelta(days=400)
        cache = HeavyCache()

        self.snapshot(tmp_path, "first", cache)
        self.snapshot(tmp_path, "second", cache)

        assert db["reads"] == [full_since(db["now"])] * 2

    def test_an_old_part_is_rebuilt(self, tmp_path, db):
        cache = HeavyCache()
        self.snapshot(tmp_path, "first", cache)

        cache.built_at -= HEAVY_INTERVAL
        self.snapshot(tmp_path, "second", cache)

        assert db["builds"] == 2
