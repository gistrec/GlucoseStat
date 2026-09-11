"""Tests for the snapshot maths.

Almost everything here is a pure function over a list of readings — no
database, no network. Importing ``publish`` is safe without configuration
because the engine is built lazily on first use.

The exception is ``TestStandaloneEntryPoint``, which starts ``publish.py`` as a
process, because the wiring it guards is not reachable any other way. It still
reaches no database: the address it passes points at a port nothing listens on.
"""

import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from conftest import BASE
from publish import (
    COMPARE_MIN_AVG_MGDL,
    COMPARE_MIN_TIR_PP,
    DAY_MIN_COVERAGE,
    TARGET_HIGH_MGDL,
    TARGET_LOW_MGDL,
    CGM_READINGS_PER_DAY,
    _compare,
    _daily,
    _downsample,
    _events,
    _gmi,
    _stats,
    _stored_last_success,
    _trend,
    build_snapshot,
    publish,
)


def readings(*values, step_minutes=5, start=BASE):
    """Build readings spaced evenly apart."""

    return [(start + timedelta(minutes=step_minutes * i), v) for i, v in enumerate(values)]


def unix(moment):
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


class TestDownsample:
    def test_empty_input_gives_no_points(self):
        assert _downsample([], 5) == []

    def test_averages_within_a_bucket(self):
        # Три замера в пределах одного 15-минутного окна складываются в один.
        data = readings(100, 110, 120, step_minutes=1)
        points = _downsample(data, 15)

        assert len(points) == 1
        assert points[0][1] == 110

    def test_separate_buckets_stay_separate(self):
        data = readings(100, 200, step_minutes=60)
        points = _downsample(data, 15)

        assert [p[1] for p in points] == [100, 200]

    def test_points_are_sorted_by_time(self):
        data = readings(100, 110, 120, step_minutes=30)
        points = _downsample(data, 15)

        assert [p[0] for p in points] == sorted(p[0] for p in points)

    def test_bucket_timestamps_align_to_the_step(self):
        step = 15
        points = _downsample(readings(100, 110, step_minutes=7), step)

        assert all(p[0] % (step * 60) == 0 for p in points)


class TestStats:
    def test_no_readings_gives_nothing(self):
        assert _stats([]) is None

    def test_counts_time_in_range(self):
        # Ровно половина внутри 70–180, четверть ниже, четверть выше.
        data = readings(100, 150, 50, 250)
        stats = _stats(data)

        assert stats["tir"] == 50.0
        assert stats["below"] == 25.0
        assert stats["above"] == 25.0
        assert stats["tir"] + stats["below"] + stats["above"] == 100.0

    def test_range_boundaries_count_as_in_range(self):
        stats = _stats(readings(TARGET_LOW_MGDL, TARGET_HIGH_MGDL))

        assert stats["tir"] == 100.0

    def test_reports_average_and_extremes(self):
        stats = _stats(readings(80, 100, 120))

        assert stats["avg"] == 100.0
        assert stats["min"] == 80
        assert stats["max"] == 120
        assert stats["count"] == 3

    def test_constant_readings_have_no_variation(self):
        assert _stats(readings(120, 120, 120))["cv"] == 0.0

    def test_variation_grows_with_spread(self):
        steady = _stats(readings(110, 120, 130))["cv"]
        jumpy = _stats(readings(60, 120, 180))["cv"]

        assert jumpy > steady


class TestDaily:
    """Подневная сводка месяца: сутки нарезаются по местной полуночи.

    ``BASE`` из conftest — ровно местная полночь Белграда, поэтому окна,
    кратные суткам от него, ложатся на календарные дни без обрезков.
    """

    def full_day(self, start, hours=24):
        """Замер каждые 5 минут — номинальная плотность сенсора."""

        return [
            (start + timedelta(minutes=5 * i), 120.0) for i in range(int(hours * 12))
        ]

    def test_the_array_is_dense_an_empty_day_stays_in_it(self):
        # Три дня, средний — без единого показания: сенсор был снят.
        data = self.full_day(BASE) + self.full_day(BASE + timedelta(days=2))

        days = _daily(data, BASE + timedelta(days=3), timedelta(days=3))

        assert len(days) == 3
        assert days[0]["partial"] is False
        # Пустой день — только границы и нулевой счёт, без выдуманных сводок.
        assert days[1] == {
            "start": unix(BASE + timedelta(days=1)),
            "end": unix(BASE + timedelta(days=2)),
            "count": 0,
        }

    def test_window_edges_clip_the_first_and_last_day(self):
        now = BASE + timedelta(hours=12)
        span = timedelta(days=2)
        data = [(now - span + timedelta(minutes=5 * i), 120.0) for i in range(2 * 288)]

        days = _daily(data, now, span)

        assert len(days) == 3
        # start/end — наблюдаемый кусок суток, а не календарные границы.
        assert days[0]["start"] == unix(now - span)
        assert days[0]["partial"] is True
        assert days[1]["partial"] is False
        assert days[-1]["end"] == unix(now)
        assert days[-1]["partial"] is True

    def test_thin_coverage_marks_a_full_day_partial(self):
        # Календарные сутки целиком в окне, но замеров — треть ожидания.
        data = [(BASE + timedelta(minutes=5 * i), 120.0) for i in range(100)]

        day = _daily(data, BASE + timedelta(days=1), timedelta(days=1))[0]

        assert day["coverage"] < 100 * DAY_MIN_COVERAGE
        assert day["partial"] is True

    def test_a_25_hour_day_is_not_partial(self):
        # Белград, 25 октября 2026 — 25-часовые сутки перехода на зимнее
        # время. now — местная полночь 26-го (23:00 UTC: зона уже UTC+1),
        # окно в 49 часов начинается ровно в местную полночь 24-го.
        now = datetime(2026, 10, 25, 23, 0, 0)
        span = timedelta(hours=49)
        data = [(now - span + timedelta(minutes=5 * i), 120.0) for i in range(49 * 12)]

        days = _daily(data, now, span)

        assert len(days) == 2
        long_day = days[1]
        assert long_day["end"] - long_day["start"] == 25 * 3600
        # Ожидание растянуто по фактической длине суток: полный длинный день
        # не наказывается пометкой partial за то, что он длинный.
        assert long_day["count"] == 300
        assert long_day["coverage"] == 100
        assert long_day["partial"] is False

    def test_a_single_reading_day_collapses_its_percentiles(self):
        # Смена сенсора: один замер за сутки. День виден, но не притворяется
        # разбросом — p25 = p50 = p75.
        data = [(BASE + timedelta(hours=12), 137.0)]

        day = _daily(data, BASE + timedelta(days=1), timedelta(days=1))[0]

        assert day["p25"] == day["p50"] == day["p75"] == 137
        assert day["count"] == 1
        assert day["partial"] is True

    def test_a_reading_stamped_exactly_at_a_midnight_now_is_left_out(self):
        # «Сейчас» ровно в местную полночь: замер с меткой в неё принадлежит
        # дню нулевой длины, которого в массиве нет. Он отбрасывается явно,
        # а не теряется в дне, до которого не доходит цикл.
        data = self.full_day(BASE) + [(BASE + timedelta(days=1), 120.0)]

        days = _daily(data, BASE + timedelta(days=1), timedelta(days=1))

        assert len(days) == 1
        assert days[0]["count"] == 288

    def test_a_day_entry_carries_stats_and_percentiles(self):
        day = _daily(self.full_day(BASE), BASE + timedelta(days=1), timedelta(days=1))[0]

        for key in (
            "start", "end", "count", "avg", "min", "max",
            "tir", "below", "above", "cv",
            "p25", "p50", "p75", "coverage", "partial",
        ):
            assert key in day, key

    def test_snapshot_names_the_series_kinds(self):
        # Старая страница на новом снимке и новая на старом различаются по
        # kind — он обязан быть у всех серий.
        snapshot = build_snapshot(readings(120, 130), [], BASE + timedelta(hours=1))

        assert snapshot["series"]["day"]["kind"] == "points"
        assert snapshot["series"]["two_days"]["kind"] == "points"
        assert snapshot["series"]["week"]["kind"] == "points"
        assert snapshot["series"]["month"]["kind"] == "daily"
        assert "points" not in snapshot["series"]["month"]

    def test_snapshot_carries_the_day_profile_key(self):
        # Проводка профиля — одна строка в build_snapshot, но без неё он не
        # существует ни для кого. Двух замеров на профиль мало — честный null.
        snapshot = build_snapshot(readings(120, 130), [], BASE + timedelta(hours=1))

        assert "profile" in snapshot
        assert snapshot["profile"] is None


class TestCompare:
    """Окно против предыдущего такого же: дельты, оценка, значимость."""

    WEEK = timedelta(days=7)

    def dense_week(self, value, end=BASE):
        """Неделя с полной плотностью замеров — покрытие заведомо выше порога."""

        return [
            (end - self.WEEK + timedelta(minutes=5 * i), value)
            for i in range(7 * 288)
        ]

    def test_no_previous_period_names_the_reason(self):
        compare = _compare(_stats(readings(120)), None, self.WEEK)

        assert compare == {"days": 7, "count": 0, "reason": "no_data"}

    def test_a_thin_previous_period_is_not_compared(self):
        # «Предыдущего периода нет» и «в нём мало измерений» — разные фразы,
        # и выбирает между ними сборщик, который знает счёт.
        compare = _compare(_stats(readings(120)), _stats(readings(120, 130)), self.WEEK)

        assert compare["reason"] == "thin"
        assert compare["count"] == 2
        assert "avg" not in compare and "tir" not in compare

    def test_a_fallen_average_above_the_target_is_better(self):
        current = _stats(self.dense_week(150.0))
        previous = _stats(self.dense_week(160.0))

        compare = _compare(current, previous, self.WEEK)

        assert compare["reason"] is None
        assert compare["avg"]["was"] == 160.0
        assert compare["avg"]["delta"] == -10.0
        assert compare["avg"]["better"] is True
        assert compare["avg"]["significant"] is True

    def test_an_average_fallen_below_the_target_gets_no_verdict(self):
        # «Меньше — лучше» верно ровно до порога гипогликемии.
        current = _stats(self.dense_week(65.0))
        previous = _stats(self.dense_week(90.0))

        assert _compare(current, previous, self.WEEK)["avg"]["better"] is None

    def test_a_risen_average_gets_no_verdict_either(self):
        # Подъём из гипогликемии — движение к цели, а не провал: оценка
        # ставится только там, где она честная.
        current = _stats(self.dense_week(160.0))
        previous = _stats(self.dense_week(150.0))

        assert _compare(current, previous, self.WEEK)["avg"]["better"] is None

    def test_noise_is_published_but_not_significant(self):
        current = _stats(self.dense_week(150.0))
        previous = _stats(self.dense_week(150.0 + COMPARE_MIN_AVG_MGDL - 1))

        compare = _compare(current, previous, self.WEEK)

        assert compare["avg"]["significant"] is False
        assert compare["tir"]["significant"] is False

    def test_time_in_range_judges_both_directions(self):
        # 150 — в диапазоне, 200 — выше него: сдвиг доли виден в обе стороны.
        good = _stats(self.dense_week(150.0))
        bad = _stats(
            [
                (moment, 200.0 if i % 2 else 150.0)
                for i, (moment, _) in enumerate(self.dense_week(150.0))
            ]
        )

        assert _compare(good, bad, self.WEEK)["tir"]["better"] is True
        assert _compare(bad, good, self.WEEK)["tir"]["better"] is False
        assert (
            abs(_compare(good, bad, self.WEEK)["tir"]["delta"]) >= COMPARE_MIN_TIR_PP
        )

    def test_variability_is_not_compared(self):
        # См. комментарий в _compare: при падающем среднем cv растёт чисто
        # арифметически, и стрелка на нём врала бы.
        current = _stats(self.dense_week(150.0))

        compare = _compare(current, current, self.WEEK)

        assert "cv" not in compare

    def test_snapshot_compares_week_and_month_but_not_hourly_windows(self):
        data = [
            (BASE - timedelta(days=15) + timedelta(minutes=5 * i), 120.0)
            for i in range(15 * 288)
        ]

        snapshot = build_snapshot(data, [], BASE)

        assert "prev" not in snapshot["stats"]["day"]
        assert "prev" not in snapshot["stats"]["two_days"]
        assert snapshot["stats"]["week"]["prev"]["reason"] is None
        # Данных 15 дней: месяц есть, а предыдущего месяца ещё нет.
        assert snapshot["stats"]["month"]["prev"]["reason"] == "no_data"


class TestPublishWindow:
    def test_publish_reads_a_doubled_window(self, tmp_path, monkeypatch):
        """Единственная правка проводки во всех трёх задачах: publish обязан
        читать два максимальных окна, иначе месячное сравнение навсегда
        превращается в «сравнивать не с чем» — без единого внешнего признака.
        """

        captured = {}

        def spy(since):
            captured["since"] = since
            return []

        monkeypatch.setattr("publish.readings_since", spy)
        monkeypatch.setattr("publish.journal_since", lambda since: [])
        monkeypatch.setattr("publish.meal_origins_since", lambda since: {})
        monkeypatch.setattr("publish.last_readings", list)

        publish(path=str(tmp_path / "data.json"), last_success=1.0)

        window = datetime.now(timezone.utc).replace(tzinfo=None) - captured["since"]
        assert abs(window - timedelta(days=60)) < timedelta(minutes=5)


class TestTrend:
    def test_no_readings_gives_nothing(self):
        assert _trend([]) is None

    def test_uses_the_latest_reading(self):
        latest = _trend(readings(100, 150, 200))

        assert latest["mgdl"] == 200

    def test_rate_is_per_minute_over_the_trend_window(self):
        # 15 минут, +30 мг/дл на всём окне -> 2 мг/дл в минуту.
        data = readings(100, 130, step_minutes=15)
        latest = _trend(data)

        assert latest["rate"] == pytest.approx(2.0)

    def test_falling_glucose_gives_a_negative_rate(self):
        latest = _trend(readings(200, 100, step_minutes=20))

        assert latest["rate"] < 0

    def test_a_lone_reading_has_no_rate(self):
        # Не с чем сравнивать: скорость неизвестна, а не равна нулю.
        assert _trend(readings(120))["rate"] is None

    def test_readings_inside_the_window_do_not_set_a_rate(self):
        # Оба замера моложе окна тренда — сравнивать по-прежнему не с чем.
        assert _trend(readings(100, 110, step_minutes=1))["rate"] is None


class TestEvents:
    def entry(self, kind, minutes_ago, carbs=None, units=None):
        return (BASE - timedelta(minutes=minutes_ago), kind, carbs, units)

    def test_splits_into_three_lanes(self):
        journal = [
            self.entry("meal", 60, carbs=62.0),
            self.entry("bolus", 75, units=6.0),
            self.entry("basal", 600, units=18.0),
        ]

        lanes = _events(journal, BASE - timedelta(days=1))

        assert len(lanes["meals"]) == 1
        assert len(lanes["bolus"]) == 1
        assert len(lanes["basal"]) == 1
        assert lanes["meals"][0][1] == 62.0

    def test_older_than_the_window_is_dropped(self):
        # Панель событий рисуется только на суточном окне: сотня отметок за
        # месяц сливается в сплошную полосу.
        journal = [self.entry("meal", 60 * 30, carbs=62.0)]

        assert _events(journal, BASE - timedelta(days=1))["meals"] == []

    def test_entry_without_an_amount_is_skipped(self):
        # Столбик нулевой высоты неотличим от отсутствия столбика.
        journal = [self.entry("meal", 60), self.entry("bolus", 60)]

        lanes = _events(journal, BASE - timedelta(days=1))

        assert lanes["meals"] == [] and lanes["bolus"] == []

    def test_unknown_kind_does_not_break_publishing(self):
        # Бот может завести новый вид записи раньше, чем дашборд про него узнает.
        journal = [self.entry("exercise", 60, units=30.0)]

        lanes = _events(journal, BASE - timedelta(days=1))

        assert lanes == {"meals": [], "bolus": [], "basal": []}

    def test_snapshot_events_reach_back_two_days(self):
        # Окно «48 часов» заведено ради «а что было ровно сутки назад»:
        # события в снимке обязаны покрывать оба дня, а не только последний.
        # Откат EVENT_WINDOW к суткам оставил бы кнопку на месте, но стёр бы
        # с её холста именно вчерашние еду и дозы.
        journal = [
            self.entry("meal", 60 * 40, carbs=45.0),
            self.entry("meal", 60 * 50, carbs=30.0),
        ]

        events = build_snapshot([], journal, BASE)["events"]

        assert events["meals"] == [[unix(BASE - timedelta(hours=40)), 45.0]]


class TestJournalAbsent:
    """Дашборд обязан пережить отсутствие журнала.

    Таблицу заводит бот, и до его первой выкладки её на сервере нет. Если
    запрос к ней уронит publish(), страница перестанет обновляться — то есть
    выкладка коллектора сломает то, что работало годами.
    """

    def test_missing_table_yields_no_events(self, monkeypatch):
        from sqlalchemy import create_engine

        from database import connection
        from database.queries import journal_since

        # База без единой таблицы — ровно то, что увидит коллектор до бота
        empty = create_engine("sqlite://")
        monkeypatch.setattr(connection, "_engine", lambda: empty)

        assert journal_since(BASE - timedelta(days=1)) == []


class TestGmi:
    """GMI считается по своим двум неделям, а не по выбранному на странице окну."""

    EXPECTED_AT_100 = 3.31 + 0.02392 * 100

    def fortnight(self, value=100.0):
        count = CGM_READINGS_PER_DAY * 14
        start = BASE - timedelta(days=14)
        return [(start + timedelta(minutes=5 * i), value) for i in range(count)]

    def test_follows_the_published_formula(self):
        # Bergenstal et al. 2018: GMI = 3.31 + 0.02392 × среднее в мг/дл.
        result = _gmi(self.fortnight(100.0), BASE)

        assert result["value"] == pytest.approx(self.EXPECTED_AT_100, abs=0.05)
        assert result["days"] == 14

    def test_thin_coverage_yields_nothing(self):
        """Расчётный HbA1c по трём дням выглядит так же солидно, как по
        четырнадцати, а означает совсем другое."""

        sparse = self.fortnight()[: CGM_READINGS_PER_DAY * 3]

        assert _gmi(sparse, BASE) is None

    def test_readings_older_than_the_window_do_not_count(self):
        # Тысяча измерений по 400 мг/дл сдвинула бы среднее, попади они в расчёт
        old = [
            (BASE - timedelta(days=30) + timedelta(minutes=5 * i), 400.0)
            for i in range(1000)
        ]

        result = _gmi(old + self.fortnight(100.0), BASE)

        assert result["value"] == pytest.approx(self.EXPECTED_AT_100, abs=0.05)

    def test_no_readings(self):
        assert _gmi([], BASE) is None


class TestStoredLastSuccess:
    """The collector's memory dies with its process; the snapshot's does not."""

    def test_inherits_the_mark_from_the_previous_snapshot(self, tmp_path):
        path = tmp_path / "data.json"
        path.write_text('{"collector": {"last_success": 1756500000}}', encoding="utf-8")

        assert _stored_last_success(str(path)) == 1756500000.0

    def test_missing_file_means_nothing_to_inherit(self, tmp_path):
        assert _stored_last_success(str(tmp_path / "data.json")) is None

    def test_corrupt_or_alien_json_means_nothing_to_inherit(self, tmp_path):
        # Включая истинные, но нечисловые значения: чужой файл не должен
        # ронять каждую публикацию, пока его не поправят руками.
        path = tmp_path / "data.json"
        alien = [
            "{not json",
            '{"collector": null}',
            '{"collector": {"last_success": "yesterday"}}',
            '{"collector": {"last_success": [1, 2]}}',
        ]

        for content in alien:
            path.write_text(content, encoding="utf-8")
            assert _stored_last_success(str(path)) is None, content

    def test_null_mark_stays_null(self, tmp_path):
        # «Сборщик ещё не получал данные» — честное состояние свежей установки,
        # его наследование не должно превращать null в ошибку.
        path = tmp_path / "data.json"
        path.write_text('{"collector": {"last_success": null}}', encoding="utf-8")

        assert _stored_last_success(str(path)) is None


class TestPublishCarryForward:
    def test_standalone_run_keeps_the_previous_mark(self, tmp_path, monkeypatch):
        """Разовый пересбор снимка не должен вешать на живую страницу красное
        «сборщик ещё не получал данные» под свежими цифрами."""

        monkeypatch.setattr("publish.readings_since", lambda since: [])
        monkeypatch.setattr("publish.journal_since", lambda since: [])
        monkeypatch.setattr("publish.meal_origins_since", lambda since: {})
        monkeypatch.setattr("publish.last_readings", list)

        path = str(tmp_path / "data.json")
        publish(path=path, last_success=1756500000.0)
        publish(path=path)

        with open(path, encoding="utf-8") as handle:
            snapshot = json.load(handle)

        assert snapshot["collector"]["last_success"] == 1756500000

    def test_a_failed_write_leaves_no_half_snapshot_behind(self, tmp_path, monkeypatch):
        """Каталог раздаёт nginx: недописанный снимок в нём остаться не должен."""

        monkeypatch.setattr("publish.readings_since", lambda since: [])
        monkeypatch.setattr("publish.journal_since", lambda since: [])
        monkeypatch.setattr("publish.meal_origins_since", lambda since: {})
        monkeypatch.setattr("publish.last_readings", list)

        # Каталог на месте файла: переименовать в него нельзя, и publish
        # свалится уже после того, как временный файл написан.
        target = tmp_path / "data.json"
        target.mkdir()

        with pytest.raises(OSError):
            publish(path=str(target))

        assert [item.name for item in tmp_path.iterdir()] == ["data.json"]


class TestStandaloneEntryPoint:
    """The documented one-off rebuild has to start.

    Всё остальное здесь — чистые функции, и они оставались зелёными всё то
    время, пока `python publish.py` падал на незаданных настройках: сломана
    была обвязка ``__main__``, которой ни один тест не касался. Поэтому запуск
    проверяется как запуск — подпроцессом.
    """

    SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "publish.py"
    DOTENV = SCRIPT.parent / ".env"

    # Настройки без окружения может дать только .env — в этом и проверка.
    # Найти его подменой переменных нельзя: dotenv ищет файл рядом с модулем,
    # поэтому «а если .env нет» проверяется пропуском, а не подделкой.
    @pytest.mark.skipif(
        not DOTENV.is_file(), reason="проверяет чтение .env, а его здесь нет"
    )
    def test_it_reads_dotenv_without_exported_variables(self, tmp_path):
        """Команда из README запускается без единой экспортированной переменной.

        Адрес базы задан здесь — заведомо мёртвый порт, чтобы прогон тестов не
        ходил в настоящую базу и не зависел от сети. Пользователя, пароль и имя
        базы взять неоткуда, кроме .env, — в этом и проверка: без него запуск
        падал бы на незаданных настройках, а не на отказе в соединении.
        PUBLISH_PATH уводит запись в temp, подальше от снимка, который раздаёт
        nginx.
        """

        result = subprocess.run(
            [sys.executable, str(self.SCRIPT)],
            env={
                "PATH": os.environ["PATH"],
                "PUBLISH_PATH": str(tmp_path / "snapshot.json"),
                "MYSQL_HOST": "127.0.0.1",
                "MYSQL_PORT": "1",
            },
            capture_output=True,
            text=True,
            timeout=60,
            # Упереться в недоступную базу — законный исход: проверяется не
            # успех пересборки, а то, что настройки нашлись.
            check=False,
        )

        assert "MYSQL_USER, MYSQL_PASSWORD" not in result.stderr
