import { state } from "./state.js";

// Клинический коэффициент пересчёта. Точное значение — 18,016, но и Libre, и
// приложения используют 18: расхождение (0,01 ммоль/л) меньше шага сенсора.
export const MGDL_PER_MMOL = 18;

/* Часовой пояс всех подписей времени — тот же, в котором отвечает бот
   (DISPLAY_TZ=Europe/Belgrade).

   В data.json время лежит в unix-секундах, и без явной зоны страница читала бы
   его по часам устройства: один и тот же приём пищи назывался бы 01:43 с
   ноутбука и 02:43 с телефона, живущего по другой стране. Зона названа вслух в
   подсказке — иначе выбор остаётся невидимым ровно тогда, когда человек
   сверяет запись с собственными часами. */
export const TIMEZONE = "Europe/Belgrade";
export const TIMEZONE_LABEL = "Белград";

/* Зона, в которой сборщик нарезал сутки, приходит в снимке; пока снимка нет —
   или он собран прежним сборщиком — остаётся прибитая константа. Всё, что
   рисует нарезанные на сервере сутки (подневный вид месяца, профиль обычного
   дня), берёт зону отсюда: подписывать чужую нарезку по своей зоне значит
   молча врать на час-два. */
export function displayTimezone() {
    return (state.snapshot && state.snapshot.timezone) || TIMEZONE;
}

/* Минуты местных суток — для профиля обычного дня. Форматтер один на модуль:
   Intl.DateTimeFormat дорог в создании, а minutesOfDay зовётся на каждый слот
   каждой перерисовки. hourCycle: "h23" обязателен: при hour: "2-digit" без
   него ICU в части локалей отдаёт «24:00», и полуночный слот уезжает за
   пределы массива. */
export const TZ_MINUTES = new Intl.DateTimeFormat("ru-RU", {
    timeZone: TIMEZONE,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
});

export function minutesOfDay(seconds) {
    let hours = 0;
    let minutes = 0;
    for (const part of TZ_MINUTES.formatToParts(new Date(seconds * 1000))) {
        if (part.type === "hour") hours = Number(part.value);
        if (part.type === "minute") minutes = Number(part.value);
    }
    return hours * 60 + minutes;
}

export function toMmol(mgdl) {
    return mgdl / MGDL_PER_MMOL;
}

export function formatMmol(mgdl, digits = 1) {
    return toMmol(mgdl).toLocaleString("ru-RU", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    });
}

/* Пробел перед знаком процента по типографике неразрывный: в узкой карточке
   строка иначе рвётся между числом и знаком, оставляя «%» болтаться отдельно. */
export function percent(value) {
    return `${value.toLocaleString("ru-RU")} %`;
}

export function formatAgo(ms) {
    const minutes = Math.round(ms / 60000);
    if (minutes < 1) return "только что";
    if (minutes < 60) return `${minutes} мин назад`;

    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} ч назад`;

    const days = Math.round(hours / 24);
    return `${days} дн назад`;
}

/* Возраст записи журнала — с минутами внутри часа. formatAgo округляет до
   целого часа, а у еды и укола стирает ровно тот десяток минут, ради которого
   на них и смотрят: успел ли подействовать короткий, много ли осталось от
   съеденного. За сутками минуты снова не нужны — длинный, поставленный «27 ч
   назад», просрочен независимо от их числа. */
export function formatEventAgo(ms) {
    const minutes = Math.round(ms / 60000);
    if (minutes < 1) return "только что";
    if (minutes >= 24 * 60) return `${Math.floor(minutes / 60)} ч назад`;
    return `${formatSpan(ms / 1000)} назад`;
}

/* Длительность промежутка: «40 мин», «1 ч», «1 ч 40 мин». Отдельно от
   formatEventAgo потому, что промежуток не всегда отсчитывается от «сейчас» —
   молчание сенсора посреди ночи кончилось до того, как на него посмотрели, — а
   набираться оно обязано теми же словами: два формата длительности на одной
   странице читаются как две разные величины. */
export function formatSpan(seconds) {
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} мин`;

    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return rest ? `${hours} ч ${rest} мин` : `${hours} ч`;
}

export function formatDateTime(date) {
    return date.toLocaleString("ru-RU", {
        timeZone: TIMEZONE,
        day: "numeric",
        month: "long",
        hour: "2-digit",
        minute: "2-digit",
    });
}

/* Та же метка для колонки «Когда», но датой-числом.

   Ячейка тесная: семь колонок делят ширину панели, и первой достаётся
   минимум — «1 сентября в 00:08» складывалось в ней надвое. Числовая форма
   вдвое короче и, главное, одной ширины во всех строках (tabular-nums на
   .meals td), так что колонка не дышит при перерисовке раз в минуту.

   Только для таблицы. Во фразах — «Последнее измерение — …» — и в подписях
   для скринридера остаётся длинная форма: там место есть, а «01.09» читается
   вслух как «ноль один точка ноль девять». */
export function formatCellDateTime(date) {
    return date.toLocaleString("ru-RU", {
        timeZone: TIMEZONE,
        day: "2-digit",
        month: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
    });
}

export function formatDay(date) {
    return date.toLocaleDateString("ru-RU", {
        timeZone: TIMEZONE,
        day: "numeric",
        month: "long",
    });
}

/* День месяца в зоне отображения. getDate() читает часы устройства, и в ночь на
   второе число предлог выбирался бы по чужой дате — «с 2 сентября» вместо «со
   2 сентября». */
export function dayOfMonth(date) {
    return Number(
        date.toLocaleDateString("ru-RU", { timeZone: TIMEZONE, day: "numeric" })
    );
}

/* «Со 2 августа», «с 24 августа»: дата старейшего разобранного приёма вместо
   обещания «за две недели» — лимит кривых в снимке срабатывает раньше
   двухнедельного окна. Предлог меняется только перед «2», а неразрывные
   пробелы держат фразу одним куском — см. formatDose. */
export function sinceLabel(date) {
    const label = formatDay(date).replace(" ", " ");
    return `${dayOfMonth(date) === 2 ? "со" : "с"} ${label}`;
}

/* Стрелка тренда по скорости в мг/дл за минуту — те же пороги, по которым
   рисует стрелку сам Libre. */
export function trendArrow(rate) {
    if (rate === null || rate === undefined) return "";
    if (rate >= 2) return "↑";
    if (rate >= 1) return "↗";
    if (rate > -1) return "→";
    if (rate > -2) return "↘";
    return "↓";
}

export function formatAmount(value) {
    return value.toLocaleString("ru-RU", { maximumFractionDigits: 1 });
}

export function formatDelta(mgdl) {
    return toMmol(mgdl).toLocaleString("ru-RU", {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
        signDisplay: "exceptZero",
    });
}

/* Болюс еды: доза и её упреждение. Абсолютное время укола не нужно — оно
   читается из колонки «Когда», а связка «сколько и за сколько до еды» — то,
   ради чего колонка существует. */

// Укол в пределах пяти минут от еды — «с едой»: журнал ведётся руками, и пара
// минут в нём — точность записи, а не осмысленное упреждение.
const DOSE_WITH_MEAL_MIN = 5;

export function formatDose(dose) {
    if (!dose) return "—";
    // Неразрывные пробелы внутри половин: ячейка может сложиться в две строки
    // «7,2 ед / за 15 мин до», но не оставить «до» болтаться на своей.
    const units = `${formatAmount(dose.units)} ед`;
    if (dose.lead_min > DOSE_WITH_MEAL_MIN) return `${units} за ${dose.lead_min} мин до`;
    if (dose.lead_min < -DOSE_WITH_MEAL_MIN) return `${units} через ${-dose.lead_min} мин`;
    return `${units} с едой`;
}
