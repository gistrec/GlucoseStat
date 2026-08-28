"""Tests for the alert decision.

``decide`` is a pure function over a reading and the remembered episode — no
network, no database, no clock. Everything that makes the alerts bearable
lives there: the repeat interval, the hysteresis around the threshold, and the
refusal to alert on a stale reading.
"""

from notify import (
    EMERGENCY,
    HIGH,
    LOW,
    LOW_MGDL,
    LOW_SOUND,
    REPEAT_AFTER,
    URGENT,
    URGENT_MGDL,
    URGENT_SOUND,
    _minutes,
    _mmol,
    decide,
)


NOW = 1_000_000.0
FRESH = 60.0


def state(level=LOW, since=NOW, sent_at=NOW):
    """An episode as it looks after an alert has gone out."""

    return {"level": level, "since": since, "sent_at": sent_at}


class TestFirstAlert:
    def test_a_reading_in_range_says_nothing(self):
        alert, updated = decide(120, FRESH, {}, NOW)

        assert alert is None
        assert updated == {}

    def test_a_low_reading_alerts(self):
        alert, updated = decide(65, FRESH, {}, NOW)

        assert alert.level == LOW
        assert alert.priority == HIGH
        assert updated["level"] == LOW
        assert updated["sent_at"] == NOW

    def test_a_critical_reading_alerts_as_emergency(self):
        alert, _ = decide(50, FRESH, {}, NOW)

        assert alert.level == URGENT
        assert alert.priority == EMERGENCY

    def test_the_two_levels_sound_different(self):
        # Звук задан явно, а не оставлен на настройку приложения, и у порогов
        # он разный — критическую гипогликемию слышно, не доставая телефон.
        assert decide(65, FRESH, {}, NOW)[0].sound == LOW_SOUND
        assert decide(50, FRESH, {}, NOW)[0].sound == URGENT_SOUND
        assert LOW_SOUND != URGENT_SOUND

    def test_the_threshold_itself_is_still_in_range(self):
        # 70 мг/дл — граница целевого диапазона, а не выход из него.
        assert decide(LOW_MGDL, FRESH, {}, NOW)[0] is None
        assert decide(URGENT_MGDL, FRESH, {}, NOW)[0].level == LOW

    def test_the_message_carries_the_value_in_mmol(self):
        alert, _ = decide(65, FRESH, {}, NOW)

        assert alert.message.startswith("3,6 ммоль/л")


class TestStaleReadings:
    def test_an_old_reading_does_not_alert(self):
        # После рестарта в базе может лежать значение недельной давности.
        alert, updated = decide(50, 7 * 24 * 3600, {}, NOW)

        assert alert is None
        assert updated == {}

    def test_an_open_episode_survives_the_sensor_going_quiet(self):
        # Сенсор отвалился посреди гипогликемии — эпизод не закрыт, и его
        # возвращение к низким значениям не должно считаться новым.
        opened = state()
        alert, updated = decide(60, 3600, opened, NOW)

        assert alert is None
        assert updated == opened


class TestRepeats:
    def test_the_same_low_stays_quiet_until_the_interval_passes(self):
        alert, updated = decide(65, FRESH, state(), NOW + REPEAT_AFTER - 60)

        assert alert is None
        assert updated["sent_at"] == NOW

    def test_a_low_that_will_not_lift_is_repeated(self):
        later = NOW + REPEAT_AFTER
        alert, updated = decide(65, FRESH, state(), later)

        assert alert is not None
        assert updated["sent_at"] == later

    def test_a_repeat_says_how_long_it_has_lasted(self):
        alert, _ = decide(65, FRESH, state(), NOW + REPEAT_AFTER)

        assert "низкий уже 30 минут" in alert.message

    def test_the_episode_keeps_its_original_start(self):
        _, updated = decide(65, FRESH, state(), NOW + REPEAT_AFTER)

        assert updated["since"] == NOW


class TestEscalation:
    def test_falling_to_critical_alerts_at_once(self):
        # Ждать конца получасового интервала здесь нельзя.
        alert, updated = decide(50, FRESH, state(), NOW + 300)

        assert alert.priority == EMERGENCY
        assert updated["level"] == URGENT

    def test_coming_back_up_to_merely_low_does_not_alert_again(self):
        alert, _ = decide(65, FRESH, state(level=URGENT), NOW + 300)

        assert alert is None

    def test_the_episode_remembers_its_worst_level(self):
        # Иначе колебание вокруг 55 поднимало бы экстренное уведомление через
        # замер — а оно и так повторяется само, пока его не подтвердят.
        _, updated = decide(65, FRESH, state(level=URGENT), NOW + REPEAT_AFTER)

        assert updated["level"] == URGENT


class TestRecovery:
    def test_a_clear_recovery_closes_the_episode(self):
        alert, updated = decide(120, FRESH, state(), NOW + 600)

        assert alert is None
        assert updated == {}

    def test_the_episode_stays_open_just_above_the_threshold(self):
        # 75 мг/дл — уже «в диапазоне», но это шум вокруг порога, а не возврат.
        opened = state()
        alert, updated = decide(75, FRESH, opened, NOW + 600)

        assert alert is None
        assert updated == opened

    def test_dipping_again_inside_the_margin_is_the_same_episode(self):
        _, updated = decide(75, FRESH, state(), NOW + 600)
        alert, _ = decide(65, FRESH, updated, NOW + 900)

        assert alert is None

    def test_a_low_after_a_recovery_is_a_new_episode(self):
        _, recovered = decide(120, FRESH, state(), NOW + 600)
        alert, updated = decide(65, FRESH, recovered, NOW + 900)

        assert alert is not None
        assert updated["since"] == NOW + 900


class TestFormatting:
    def test_readings_are_shown_with_a_decimal_comma(self):
        assert _mmol(65) == "3,6"

    def test_minutes_agree_with_the_numeral(self):
        assert _minutes(60) == "1 минуту"
        assert _minutes(3 * 60) == "3 минуты"
        assert _minutes(30 * 60) == "30 минут"
        # 11–14 — исключение из общего правила: «11 минут», не «11 минута».
        assert _minutes(11 * 60) == "11 минут"
