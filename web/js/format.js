import { state } from "./state.js";

// Клинический коэффициент пересчёта. Точное значение — 18,016, но и Libre, и
// приложения используют 18: расхождение (0,01 ммоль/л) меньше шага сенсора.
export const MGDL_PER_MMOL = 18;

/* Два пояса, и путать их нельзя.

   Домашний (HOME_TIMEZONE) — тот, в котором отвечает бот и в котором сборщик
   режет сутки (DISPLAY_TZ=Europe/Belgrade): по нему считаны ночи, профиль
   обычного дня, подневный вид месяца и разбивка еды по времени суток.
   Перенести их в другой пояс страница не может — нарезка уже сделана.

   Пояс просмотра (TIMEZONE) — в нём страница подписывает часы: ось графика,
   подсказки, «Действие до 19:45». Его выбирают в шапке (js/timezone.js), по
   умолчанию это пояс устройства. В data.json время лежит в unix-секундах, и
   без явной зоны один и тот же приём пищи назывался бы 01:43 с ноутбука и
   02:43 с телефона; поэтому зона всегда задана явно и названа в подсказке.

   TIMEZONE, TIMEZONE_LABEL и TZ_MINUTES — живые привязки модуля: setTimezone
   меняет их, и все, кто их импортировал, видят новое значение. */
export const HOME_TIMEZONE = "Europe/Belgrade";
export const HOME_LABEL = "Белград";

/* Закреплённые в меню пояса — там, где владелец бывает. Русские имена только
   у них: у остальных четырёхсот имя города берётся из идентификатора. */
export const PINNED_ZONES = [
    ["Europe/Belgrade", "Белград"],
    ["Europe/Moscow", "Москва"],
    ["Asia/Novosibirsk", "Новосибирск"],
];
const ZONE_NAMES = Object.fromEntries(PINNED_ZONES);

export function zoneLabel(zone) {
    return ZONE_NAMES[zone] || zone.split("/").pop().replace(/_/g, " ");
}

/* «UTC+3», «UTC+5:30», «UTC». На дату, а не навсегда: у Белграда смещение
   зимой и летом разное, у Москвы — одно. */
export function zoneOffset(zone, at = new Date()) {
    const part = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "shortOffset" })
        .formatToParts(at)
        .find((item) => item.type === "timeZoneName");
    return part ? part.value.replace("GMT", "UTC") : "UTC";
}

export function zoneOffsetMinutes(zone, at = new Date()) {
    const match = zoneOffset(zone, at).match(/UTC([+-])(\d+)(?::(\d+))?/);
    if (!match) return 0;
    return (match[1] === "-" ? -1 : 1) * (Number(match[2]) * 60 + Number(match[3] || 0));
}

function clockFormat(zone) {
    return new Intl.DateTimeFormat("ru-RU", {
        timeZone: zone,
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
    });
}

export let TIMEZONE = HOME_TIMEZONE;
export let TIMEZONE_LABEL = HOME_LABEL;
/* Часы и минуты в поясе просмотра. Форматтер один на пояс: Intl.DateTimeFormat
   дорог в создании. hourCycle: "h23" обязателен: при hour: "2-digit" без него
   ICU в части локалей отдаёт «24:00». */
export let TZ_MINUTES = clockFormat(TIMEZONE);

export function setTimezone(zone) {
    TIMEZONE = zone;
    TIMEZONE_LABEL = zoneLabel(zone);
    TZ_MINUTES = clockFormat(zone);
}

/* Зона, в которой сборщик нарезал сутки, приходит в снимке; пока снимка нет —
   или он собран прежним сборщиком — остаётся домашняя. Всё, что рисует
   нарезанные на сервере сутки (подневный вид месяца, профиль обычного дня),
   берёт зону отсюда: подписывать чужую нарезку по своей зоне значит молча
   врать на час-два. */
export function displayTimezone() {
    return (state.snapshot && state.snapshot.timezone) || HOME_TIMEZONE;
}

/* Часы просмотра расходятся с часами нарезки — тогда блоки, нарезанные
   сервером, договаривают «по Белграду». По смещению, а не по имени: Париж и
   Белград живут по одним часам, и приписка там была бы шумом. */
export function awayFromHome(at = new Date()) {
    return zoneOffsetMinutes(TIMEZONE, at) !== zoneOffsetMinutes(displayTimezone(), at);
}

/* «по Белграду» — для подписи нарезанного сервером. */
export function homeNote() {
    const zone = displayTimezone();
    return `по ${zone === HOME_TIMEZONE ? "Белграду" : zone}`;
}

const SLICE_MINUTES = new Map();

/* Минуты местных суток в поясе нарезки — для профиля обычного дня и разбивки
   еды по времени суток. Не в поясе просмотра: слоты профиля посчитаны
   сервером по белградским суткам, и завтрак в 08:00 по Белграду остаётся
   завтраком, сколько бы ни показывали часы в Новосибирске. */
export function minutesOfDay(seconds) {
    let hours = 0;
    let minutes = 0;
    const zone = displayTimezone();
    if (!SLICE_MINUTES.has(zone)) SLICE_MINUTES.set(zone, clockFormat(zone));
    for (const part of SLICE_MINUTES.get(zone).formatToParts(new Date(seconds * 1000))) {
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

/* Дата без года и без месяца словом: «20.09». Для карточки сенсора, где дат
   две — поставлен и кончится — и стоят они в строке с числом дней: «20 сентября»
   рядом с «5 дней осталось» растягивает строку втрое, а год в сроке, который
   меряется двумя неделями, не значит ничего. */
export function formatShortDay(date) {
    return date.toLocaleDateString("ru-RU", {
        timeZone: TIMEZONE,
        day: "2-digit",
        month: "2-digit",
    });
}

/* Русское склонение при числе: 1 день, 2 дня, 5 дней. Нужно ровно там, где
   число приходит из данных и может оказаться любым, — «осталось 1 дней»
   выдаёт шаблон сильнее, чем любая опечатка. */
export function plural(count, one, few, many) {
    const abs = Math.abs(count) % 100;
    const last = abs % 10;
    if (abs > 10 && abs < 20) return many;
    if (last > 1 && last < 5) return few;
    return last === 1 ? one : many;
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
