"""Tests for the alert decision.

``decide`` is a pure function over a reading and the remembered episode — no
network, no database, no clock. Everything that makes the alerts bearable
lives there: the repeat interval, the hysteresis around the threshold, and the
refusal to alert on a stale reading.
"""

from notify import (
    HIGH,
    HIGH_MGDL,
    HIGH_SOUND,
    LOW,
    LOW_MGDL,
    LOW_SOUND,
    PRIORITY_EMERGENCY,
    PRIORITY_HIGH,
    REPEAT_AFTER,
    URGENT,
    URGENT_HIGH,
    URGENT_HIGH_MGDL,
    URGENT_HIGH_SOUND,
    URGENT_MGDL,
    URGENT_SOUND,
    VERIFY_CLEAR_HIGH_MGDL,
    VERIFY_CLEAR_MGDL,
    VERIFY_FRESH_FOR,
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
        assert alert.priority == PRIORITY_HIGH
        assert updated["level"] == LOW
        assert updated["sent_at"] == NOW

    def test_a_critical_reading_alerts_as_emergency(self):
        alert, _ = decide(50, FRESH, {}, NOW)

        assert alert.level == URGENT
        assert alert.priority == PRIORITY_EMERGENCY

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

        assert alert.priority == PRIORITY_EMERGENCY
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


class TestHighAlerts:
    """Верхние пороги — зеркало нижних, и проверяется именно зеркальность.

    10,0 ммоль/л — та же граница, выше которой страница считает время выше
    цели; 16,0 — уровень, на котором пора смотреть кетоны. Сравнение строгое с
    обеих сторон: ровно на пороге тревоги ещё нет.
    """

    def test_a_high_reading_alerts(self):
        alert, updated = decide(200, FRESH, {}, NOW)

        assert alert.level == HIGH
        assert alert.priority == PRIORITY_HIGH
        assert updated["level"] == HIGH
        assert updated["sent_at"] == NOW

    def test_a_critical_high_alerts_as_emergency(self):
        alert, _ = decide(300, FRESH, {}, NOW)

        assert alert.level == URGENT_HIGH
        assert alert.priority == PRIORITY_EMERGENCY

    def test_each_level_has_its_own_sound(self):
        """Четыре звука на четыре уровня: спросонья по одному тону должно быть
        понятно не только «беда», но и в какую сторону."""

        sounds = {
            decide(mgdl, FRESH, {}, NOW)[0].sound
            for mgdl in (65, 50, 200, 300)
        }

        assert sounds == {LOW_SOUND, URGENT_SOUND, HIGH_SOUND, URGENT_HIGH_SOUND}

    def test_the_thresholds_are_exclusive(self):
        assert decide(HIGH_MGDL, FRESH, {}, NOW)[0] is None
        assert decide(HIGH_MGDL + 1, FRESH, {}, NOW)[0].level == HIGH
        assert decide(URGENT_HIGH_MGDL, FRESH, {}, NOW)[0].level == HIGH
        assert decide(URGENT_HIGH_MGDL + 1, FRESH, {}, NOW)[0].level == URGENT_HIGH

    def test_the_message_says_high_not_low(self):
        alert, _ = decide(200, FRESH, state(HIGH), NOW + REPEAT_AFTER)

        assert "высокий уже" in alert.message
        assert "низкий" not in alert.message
        assert "Высокий сахар" == alert.title

    def test_it_repeats_on_the_same_interval(self):
        assert decide(200, FRESH, state(HIGH), NOW + REPEAT_AFTER - 60)[0] is None
        assert decide(200, FRESH, state(HIGH), NOW + REPEAT_AFTER)[0] is not None

    def test_deepening_to_critical_does_not_wait(self):
        alert, updated = decide(300, FRESH, state(HIGH), NOW + 60)

        assert alert.level == URGENT_HIGH
        assert updated["level"] == URGENT_HIGH

    def test_the_episode_stays_open_just_below_the_threshold(self):
        """175 мг/дл — уже «в диапазоне», но это шум вокруг порога, а не
        возврат: запас тот же, что снизу, только в другую сторону."""

        opened = state(HIGH)
        alert, updated = decide(175, FRESH, opened, NOW + 600)

        assert alert is None
        assert updated == opened

    def test_a_clear_recovery_closes_the_episode(self):
        alert, updated = decide(150, FRESH, state(HIGH), NOW + 600)

        assert alert is None
        assert updated == {}

    def test_recovery_is_read_from_the_side_the_episode_opened_on(self):
        """Одно и то же показание закрывает низкий эпизод и не закрывает
        высокий: 175 — это подъём из гипогликемии, но ещё не спуск из
        гипергликемии."""

        assert decide(175, FRESH, state(LOW), NOW + 600)[1] == {}
        assert decide(175, FRESH, state(HIGH), NOW + 600)[1] != {}


class TestTurnaround:
    """Разворот: из гипогликемии в гипергликемию и обратно.

    Это два разных события, а не одно углубившееся. Сравнивать их тяжесть
    нечем, и второе обязано заговорить сразу — иначе передозированный сок
    промолчал бы полчаса, пока идёт отсчёт повтора от чужого эпизода.
    """

    def test_a_high_after_a_low_alerts_at_once(self):
        alert, updated = decide(200, FRESH, state(LOW), NOW + 60)

        assert alert is not None
        assert alert.level == HIGH
        assert updated["level"] == HIGH

    def test_a_low_after_a_high_alerts_at_once(self):
        alert, updated = decide(65, FRESH, state(HIGH), NOW + 60)

        assert alert is not None
        assert alert.level == LOW
        assert updated["level"] == LOW

    def test_the_new_episode_counts_from_itself(self):
        """«Высокий уже 40 минут» от начала гипогликемии — неправда о том,
        сколько человек пробыл наверху."""

        turned = NOW + 40 * 60
        alert, updated = decide(200, FRESH, state(LOW, since=NOW), turned)

        assert updated["since"] == turned
        assert "уже" not in alert.message

    def test_the_worst_level_does_not_carry_over(self):
        """Критическая гипогликемия в памяти не должна делать последующий
        высокий эпизод «уже критическим» — тяжесть меряется внутри стороны."""

        _, updated = decide(200, FRESH, state(URGENT), NOW + 60)

        assert updated["level"] == HIGH


class TestVerifiedByBlood:
    """Сверка глюкометром против ложной тревоги.

    Ночной провал у сенсора бывает компрессионным — лёг на него и всё, — и
    ровно этот случай гасится каплей крови. Проверяются три числа порога:
    свежесть сверки, её значение и то, что низкая сверка тревогу не снимает,
    а подтверждает.
    """

    def test_a_normal_fingerstick_silences_a_low_sensor(self):
        alert, updated = decide(55, FRESH, {}, NOW, verified=(NOW - 300, 95.0))

        assert alert is None
        assert updated == {}

    def test_it_silences_an_open_episode_too(self):
        """Сверка приходит посреди эпизода — чаще всего так и бывает: сначала
        разбудил звонок, потом достали глюкометр."""

        alert, updated = decide(55, FRESH, state(), NOW + 600, verified=(NOW + 590, 95.0))

        assert alert is None
        assert updated == {}

    def test_a_low_fingerstick_does_not_silence_anything(self):
        """Кровь подтвердила гипогликемию — это ровно тот случай, когда
        замолчать нельзя."""

        alert, _ = decide(50, FRESH, {}, NOW, verified=(NOW - 300, 58.0))

        assert alert is not None
        # Уровень по-прежнему считается по сенсору: сверка решает, говорить или
        # молчать, а не насколько громко.
        assert alert.level == URGENT

    def test_a_fingerstick_at_the_threshold_does_not_silence_it(self):
        """3,9 у самой черты — подтверждение, а не опровержение. Граница
        снятия — возвратная, 4,4 ммоль/л."""

        assert decide(65, FRESH, {}, NOW, verified=(NOW, LOW_MGDL))[0] is not None
        assert decide(65, FRESH, {}, NOW, verified=(NOW, VERIFY_CLEAR_MGDL))[0] is None

    def test_a_stale_fingerstick_does_not_silence_anything(self):
        """Сверка получасовой давности ничего не говорит о том, что с сахаром
        сейчас: за это время он успевает уйти на пару ммоль."""

        stale = (NOW - VERIFY_FRESH_FOR - 60, 95.0)

        assert decide(55, FRESH, {}, NOW, verified=stale)[0] is not None

    def test_it_silences_even_a_stale_sensor(self):
        """Свежая кровь в норме важнее возраста показания сенсора: сенсор мог
        отвалиться посреди мнимого провала, а гипогликемии всё равно нет."""

        alert, updated = decide(
            55, 7 * 24 * 3600, state(), NOW, verified=(NOW - 60, 95.0)
        )

        assert alert is None
        assert updated == {}

    def test_the_episode_reopens_once_the_verification_goes_stale(self):
        """Главное следствие того, что эпизод закрывается, а не замирает:
        сверили — помолчали — через четверть часа спросили снова, а не
        досидели получасовую паузу до повтора."""

        _, cleared = decide(55, FRESH, state(), NOW, verified=(NOW - 60, 95.0))
        later = NOW + VERIFY_FRESH_FOR
        alert, updated = decide(55, FRESH, cleared, later, verified=(NOW - 60, 95.0))

        assert alert is not None
        assert updated["since"] == later

    def test_no_verification_changes_nothing(self):
        """``None`` — безопасная сторона: журнала может не быть вовсе, а база
        может лежать, и тревога обязана работать как работала."""

        assert decide(55, FRESH, {}, NOW, verified=None)[0] is not None
        assert decide(55, FRESH, {}, NOW)[0] is not None

    def test_it_silences_a_high_sensor_too(self):
        """Сенсор врёт и вверх — реже, чем вниз, но врёт: свежая кровь в норме
        снимает и верхнюю тревогу."""

        alert, updated = decide(300, FRESH, {}, NOW, verified=(NOW - 300, 140.0))

        assert alert is None
        assert updated == {}

    def test_a_high_fingerstick_does_not_silence_a_high_alert(self):
        """Ровно та ошибка, ради которой у сверки есть направление: 16,6
        ммоль/л по крови — это «не низко», и без направления такая капля сняла
        бы тревогу о высоком сахаре, который сама же подтверждает."""

        alert, _ = decide(300, FRESH, {}, NOW, verified=(NOW - 300, 300.0))

        assert alert is not None
        assert alert.level == URGENT_HIGH

    def test_a_low_fingerstick_does_not_silence_a_low_alert(self):
        """И зеркально: снизу эта проверка уже была, здесь она названа парой
        к верхней, чтобы правило читалось целиком."""

        assert decide(50, FRESH, {}, NOW, verified=(NOW, 40.0))[0] is not None

    def test_the_high_threshold_is_the_recovery_margin(self):
        """Сверху снимает возврат ниже 170, а не любое «не критическое»."""

        assert decide(200, FRESH, {}, NOW, verified=(NOW, HIGH_MGDL))[0] is not None
        assert (
            decide(200, FRESH, {}, NOW, verified=(NOW, VERIFY_CLEAR_HIGH_MGDL))[0]
            is None
        )


class TestFormatting:
    def test_readings_are_shown_with_a_decimal_comma(self):
        assert _mmol(65) == "3,6"

    def test_minutes_agree_with_the_numeral(self):
        assert _minutes(60) == "1 минуту"
        assert _minutes(3 * 60) == "3 минуты"
        assert _minutes(30 * 60) == "30 минут"
        # 11–14 — исключение из общего правила: «11 минут», не «11 минута».
        assert _minutes(11 * 60) == "11 минут"
