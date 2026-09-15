"""Эпизоды ниже нормы: границы, склейка, длительность."""

from datetime import timedelta, timezone

from conftest import BASE

from lows import low_episodes
from publish import TARGET_LOW_MGDL


def readings(*values, step_minutes=5, start=BASE):
    """Build readings spaced evenly apart."""

    return [(start + timedelta(minutes=step_minutes * i), v) for i, v in enumerate(values)]


def unix(moment):
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


class TestLows:
    """Эпизоды ниже нормы: сколько раз и сколько минут, по сырым замерам."""

    def test_a_quiet_window_has_no_episodes(self):
        assert low_episodes(readings(100, 110, 120), TARGET_LOW_MGDL) == []

    def test_one_dip_is_one_episode(self):
        data = readings(120, 65, 60, 110, step_minutes=5)

        episodes = low_episodes(data, TARGET_LOW_MGDL)

        assert len(episodes) == 1
        assert episodes[0]["min"] == 60

    def test_the_episode_spans_the_crossings_not_the_low_readings(self):
        # Левый край — последний замер над порогом, правый — первый над ним:
        # пересечение случилось между замерами, и относить его целиком к
        # низкой точке значило бы терять по пять минут с каждой стороны.
        data = readings(120, 65, 110, step_minutes=5)

        episode = low_episodes(data, TARGET_LOW_MGDL)[0]

        assert episode["minutes"] == 10

    def test_a_single_low_reading_is_still_an_episode(self):
        # Минимальной длительности нет нарочно: провал в пару минут — тоже
        # провал, и прятать его значило бы повторить ошибку прореживания.
        data = readings(120, 68, 120, step_minutes=1)

        assert len(low_episodes(data, TARGET_LOW_MGDL)) == 1

    def test_flapping_around_the_threshold_is_one_episode(self):
        # Вверх-вниз через порог с шагом в минуту — один эпизод, а не пять:
        # столько раз человек ничего не переживал.
        data = readings(120, 65, 72, 66, 71, 64, 120, step_minutes=1)

        episodes = low_episodes(data, TARGET_LOW_MGDL)

        assert len(episodes) == 1
        assert episodes[0]["min"] == 64

    def test_dips_far_apart_stay_separate(self):
        data = readings(120, 65, 120) + readings(
            120, 62, 120, start=BASE + timedelta(hours=3)
        )

        episodes = low_episodes(data, TARGET_LOW_MGDL)

        assert [item["min"] for item in episodes] == [65, 62]

    def test_an_unfinished_dip_ends_at_its_last_reading(self):
        # Сахар всё ещё ниже нормы: эпизод кончается последним замером, а не
        # обещанием, что он уже позади.
        data = readings(120, 65, 62, step_minutes=5)

        episode = low_episodes(data, TARGET_LOW_MGDL)[0]

        assert episode["end"] == unix(BASE + timedelta(minutes=10))
