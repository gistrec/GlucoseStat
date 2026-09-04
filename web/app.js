/* Дашборд читает единственный статический data.json — ни бэкенда, ни запросов
   к Abbott со стороны браузера. */

// Клинический коэффициент пересчёта. Точное значение — 18,016, но и Libre, и
// приложения используют 18: расхождение (0,01 ммоль/л) меньше шага сенсора.
const MGDL_PER_MMOL = 18;

// Насколько свежим считается измерение. Сенсор отдаёт точку раз в 5 минут,
// graph отстаёт ещё на несколько — 20 минут отделяют «сейчас» от «сенсор снят»
// без ложных срабатываний на обычной задержке.
const STALE_AFTER_MS = 20 * 60 * 1000;

const RELOAD_INTERVAL_MS = 60 * 1000;

// Сборщик опрашивает LibreLinkUp раз в 5 минут, так что три пропуска подряд —
// это уже не задержка, а молчание, о котором нужно сказать вслух.
const COLLECTOR_SILENT_AFTER_MS = 15 * 60 * 1000;

const RANGE_LABELS = { day: "24 часа", week: "7 дней", month: "30 дней" };

/* Часовой пояс всех подписей времени — тот же, в котором отвечает бот
   (DISPLAY_TZ=Europe/Belgrade).

   В data.json время лежит в unix-секундах, и без явной зоны страница читала бы
   его по часам устройства: один и тот же приём пищи назывался бы 01:43 с
   ноутбука и 02:43 с телефона, живущего по другой стране. Зона названа вслух в
   подсказке — иначе выбор остаётся невидимым ровно тогда, когда человек
   сверяет запись с собственными часами. */
const TIMEZONE = "Europe/Belgrade";
const TIMEZONE_LABEL = "Белград";

// Высота холста без дорожек событий — та же, что была до их появления.
const PLOT_HEIGHT = 340;
const LANE_HEIGHT = 34;
const LANE_GAP = 8;
const COLUMN_WIDTH = 7;

// Просвет между соседними подписями на дорожке. Впритык поставленные числа
// читаются как одно: «45» и «60» в паре пикселей друг от друга — это «4560».
const LABEL_GAP = 4;

let snapshot = null;
let activeRange = "day";

// Раскладка последней отрисовки: по ней работает наведение. Пересчитывать её
// на каждое движение мыши — значит дублировать всю геометрию и однажды
// разойтись с тем, что нарисовано.
let geometry = null;
let hoverTime = null;

const els = {
    now: document.getElementById("now"),
    nowValue: document.getElementById("now-value"),
    nowArrow: document.getElementById("now-arrow"),
    nowMeta: document.getElementById("now-meta"),
    empty: document.getElementById("empty"),
    ranges: document.getElementById("ranges"),
    chart: document.getElementById("chart"),
    chartEmpty: document.getElementById("chart-empty"),
    canvas: document.getElementById("canvas"),
    stats: document.getElementById("stats"),
    legend: document.getElementById("legend"),
    tip: document.getElementById("tip"),
    review: document.getElementById("review"),
    reviewNote: document.getElementById("review-note"),
    reviewStats: document.getElementById("review-stats"),
    overlay: document.getElementById("overlay"),
    overlayLegend: document.getElementById("overlay-legend"),
    meals: document.getElementById("meals"),
    footUpdated: document.getElementById("foot-updated"),
    theme: document.getElementById("theme"),
    themeColor: document.getElementById("theme-color"),
};

/* Пользовательское свойство приходит сюда невычисленным — как записано в CSS.
   Значение, которое не является цветом (так было с light-dark()), канвас молча
   игнорирует и продолжает рисовать предыдущим, то есть чёрным: график исчезал
   на тёмном фоне, не оставив следа в консоли. Поэтому цвет проверяется, а не
   берётся на веру. */
function readColor(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return /^(#|rgb|hsl)/.test(value) ? value : fallback;
}

function readNumber(name, fallback) {
    return Number(getComputedStyle(document.documentElement).getPropertyValue(name)) || fallback;
}

/* Ряды графика в одном месте: отсюда берут цвет и холст, и легенда, и
   подсказка — разъехавшись, они перестали бы опознавать одно и то же. */
/* Название — в легенде, единица — в подсказке рядом с числом. Разводить их
   стоит потому, что в легенде единица не к чему приложить, а в подсказке
   название дублирует цветную метку слева от него.

   Оба инсулина уточняют свою единицу: они делят и цвет, и шкалу дорожки, а
   различаются только заливкой квадратика — две строки «5 ед» и «8 ед» рядом
   не прочитать. Уточнение стоит у обоих, а не у одного длинного: помеченным
   оказывается тогда не «необычный», а просто тот, о ком вспомнили. */
const SERIES = {
    glucose: { token: "--accent", fallback: "#7eb8f7", label: "Глюкоза", unit: "ммоль/л" },
    meal: { token: "--meal", fallback: "#bd8a30", label: "Углеводы", unit: "г" },
    insulin: {
        token: "--insulin",
        fallback: "#cc4fb0",
        label: "Короткий инсулин",
        unit: "ед короткого",
    },
    basal: {
        token: "--insulin",
        fallback: "#cc4fb0",
        label: "Длинный инсулин",
        unit: "ед длинного",
        hollow: true,
    },
};

/* ── Тема ──────────────────────────────────────────────────────────── */

/* Два состояния. Системную тему кнопка не предлагает — она лишь берётся при
   первом заходе, пока выбор не сделан. */
const THEMES = [
    { id: "light", glyph: "☀", label: "Светлая", next: "тёмную" },
    { id: "dark", glyph: "☾", label: "Тёмная", next: "светлую" },
];

// Те же значения, что у --bg в style.css: сюда попадает цвет панели Safari.
const THEME_BG = { light: "#f4f5f9", dark: "#07070b" };

let theme = "dark";

function systemPrefersDark() {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/* Хранилище недоступно в приватном режиме Safari, и обращение к нему там
   бросает исключение. Тема — не та вещь, ради которой страница вправе не
   открыться, поэтому оба обращения обёрнуты. */
function storedTheme() {
    try {
        const saved = localStorage.getItem("theme");
        return THEMES.some((item) => item.id === saved) ? saved : null;
    } catch (error) {
        return null;
    }
}

/* Пока выбор не сделан, страница открывается в системной теме: попасть на
   белый экран ночью только потому, что настройка ещё не тронута, — плохое
   первое впечатление. После первого нажатия решает кнопка. */
function initialTheme() {
    return storedTheme() || (systemPrefersDark() ? "dark" : "light");
}

function rememberTheme(id) {
    try {
        localStorage.setItem("theme", id);
    } catch (error) {
        /* Забудется после закрытия вкладки — не повод ломать переключение. */
    }
}

function applyTheme(id) {
    const current = THEMES.find((item) => item.id === id) || THEMES[1];
    theme = current.id;
    document.documentElement.dataset.theme = theme;

    const glyph = document.createElement("span");
    glyph.setAttribute("aria-hidden", "true");
    glyph.textContent = `${current.glyph} `;

    const label = document.createElement("span");
    label.className = "theme__label";
    label.textContent = current.label;

    els.theme.replaceChildren(glyph, label);
    // Подпись называет текущую тему, а на узком экране её и вовсе не видно —
    // отсюда полное описание: и что сейчас, и что будет по нажатию.
    const hint = `Тема ${current.label.toLowerCase()}, переключить на ${current.next}`;
    els.theme.setAttribute("aria-label", hint);
    els.theme.title = hint;

    els.themeColor.setAttribute("content", THEME_BG[theme]);

    // Разметка перекрашивается сама, холсты — нет: их цвета прочитаны из
    // CSS-переменных один раз, при отрисовке.
    if (snapshot && snapshot.latest) {
        drawChart();
        renderReview();
    }
}

/* ── Форматирование ────────────────────────────────────────────────── */

function toMmol(mgdl) {
    return mgdl / MGDL_PER_MMOL;
}

function formatMmol(mgdl, digits = 1) {
    return toMmol(mgdl).toLocaleString("ru-RU", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    });
}

/* Пробел перед знаком процента по типографике неразрывный: в узкой карточке
   строка иначе рвётся между числом и знаком, оставляя «%» болтаться отдельно. */
function percent(value) {
    return `${value.toLocaleString("ru-RU")} %`;
}

function formatAgo(ms) {
    const minutes = Math.round(ms / 60000);
    if (minutes < 1) return "только что";
    if (minutes < 60) return `${minutes} мин назад`;

    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} ч назад`;

    const days = Math.round(hours / 24);
    return `${days} дн назад`;
}

function formatDateTime(date) {
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
function formatCellDateTime(date) {
    return date.toLocaleString("ru-RU", {
        timeZone: TIMEZONE,
        day: "2-digit",
        month: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
    });
}

function formatDay(date) {
    return date.toLocaleDateString("ru-RU", {
        timeZone: TIMEZONE,
        day: "numeric",
        month: "long",
    });
}

/* День месяца в зоне отображения. getDate() читает часы устройства, и в ночь на
   второе число предлог выбирался бы по чужой дате — «с 2 сентября» вместо «со
   2 сентября». */
function dayOfMonth(date) {
    return Number(
        date.toLocaleDateString("ru-RU", { timeZone: TIMEZONE, day: "numeric" })
    );
}

/* «Со 2 августа», «с 24 августа»: дата старейшего разобранного приёма вместо
   обещания «за две недели» — лимит кривых в снимке срабатывает раньше
   двухнедельного окна. Предлог меняется только перед «2», а неразрывные
   пробелы держат фразу одним куском — см. formatDose. */
function sinceLabel(date) {
    const label = formatDay(date).replace(" ", " ");
    return `${dayOfMonth(date) === 2 ? "со" : "с"} ${label}`;
}

/* Стрелка тренда по скорости в мг/дл за минуту — те же пороги, по которым
   рисует стрелку сам Libre. */
function trendArrow(rate) {
    if (rate === null || rate === undefined) return "";
    if (rate >= 2) return "↑";
    if (rate >= 1) return "↗";
    if (rate > -1) return "→";
    if (rate > -2) return "↘";
    return "↓";
}

function zoneColor(mgdl) {
    const { low, high } = snapshot.target;
    if (mgdl < low) return "var(--low)";
    if (mgdl > high) return "var(--high)";
    return "var(--in-range)";
}

/* ── Текущее значение ──────────────────────────────────────────────── */

function renderNow() {
    const latest = snapshot.latest;

    if (!latest) {
        els.empty.textContent =
            "Данных пока нет. Как только сенсор начнёт передавать показания в LibreLinkUp, они появятся здесь.";
        els.empty.hidden = false;
        return;
    }

    const measuredAt = new Date(latest.t * 1000);
    const age = Date.now() - measuredAt.getTime();
    const stale = age > STALE_AFTER_MS;

    els.nowValue.textContent = formatMmol(latest.mgdl);
    // У устаревшего значения цвет зоны вводил бы в заблуждение: «6,2 зелёным»
    // читается как текущая норма, даже если замер сделан месяц назад.
    els.nowValue.style.color = stale ? "var(--muted)" : zoneColor(latest.mgdl);
    els.nowArrow.textContent = stale ? "" : trendArrow(latest.rate);

    if (stale) {
        els.nowMeta.textContent = `Сенсор не передаёт данные. Последнее измерение — ${formatDateTime(measuredAt)}.`;
        els.nowMeta.className = "now__meta now__meta--stale";
    } else {
        els.nowMeta.textContent = `Измерено ${formatAgo(age)}`;
        els.nowMeta.className = "now__meta";
    }

    els.now.hidden = false;
}

/* ── Статистика ────────────────────────────────────────────────────── */

function statCard(label, value, hint) {
    const card = document.createElement("div");
    card.className = "stat";

    const labelEl = document.createElement("p");
    labelEl.className = "stat__label";
    labelEl.textContent = label;

    const valueEl = document.createElement("p");
    valueEl.className = "stat__value";
    valueEl.textContent = value;

    card.append(labelEl, valueEl);

    if (hint) {
        const hintEl = document.createElement("p");
        hintEl.className = "stat__hint";
        hintEl.textContent = hint;
        card.append(hintEl);
    }
    return card;
}

function renderStats() {
    const stats = snapshot.stats[activeRange];
    els.stats.replaceChildren();

    if (!stats) {
        els.stats.hidden = true;
        return;
    }

    const cards = [
        statCard("В целевом диапазоне", percent(stats.tir),
            `ниже ${percent(stats.below)} · выше ${percent(stats.above)}`),
        statCard("Среднее", formatMmol(stats.avg), "ммоль/л"),
        statCard("Разброс", `${formatMmol(stats.min)} – ${formatMmol(stats.max)}`, "ммоль/л, минимум и максимум"),
    ];

    // cv приходит null, если среднее нулевое. Случай почти невозможный
    // (сенсор не отдаёт значений ниже 40 мг/дл), но обращение к методу у null
    // уронило бы отрисовку целиком — вместе с графиком и текущим значением.
    if (stats.cv !== null && stats.cv !== undefined) {
        cards.push(statCard("Вариабельность", percent(stats.cv),
            stats.cv <= 36 ? `стабильно, норма ≤ ${percent(36)}` : `выше нормы ≤ ${percent(36)}`));
    }

    /* GMI живёт по своему окну в две недели, а не по выбранному периоду: под
       одним названием иначе оказывались бы два разных числа. Снимок отдаёт
       null, когда за две недели набралось меньше 70 % измерений — тогда
       карточки просто нет, вместо солидно выглядящей выдумки. На суточной
       панели не показываем: она про сегодня, а GMI — про две недели. */
    const gmi = snapshot.gmi;
    if (activeRange !== "day" && gmi) {
        cards.push(
            statCard("GMI", percent(gmi.value), `расчётный HbA1c за ${gmi.days} дней`)
        );
    }

    cards.push(statCard("Измерений", stats.count.toLocaleString("ru-RU"), RANGE_LABELS[activeRange]));

    els.stats.append(...cards);
    els.stats.hidden = false;
}

/* ── График ────────────────────────────────────────────────────────── */

/* Что «видно» на графике, словами: сам холст для скринридера пуст, а
   пересказывать сотни точек бессмысленно — нужен итог. */
function chartDescription(points) {
    const period = RANGE_LABELS[activeRange];
    if (!points.length) return `График глюкозы за ${period}: данных нет`;

    const stats = snapshot.stats[activeRange];
    if (!stats) return `График глюкозы за ${period}: ${points.length} точек`;

    return (
        `График глюкозы за ${period}: ${stats.count} измерений, ` +
        `среднее ${formatMmol(stats.avg)} ммоль/л, ` +
        `от ${formatMmol(stats.min)} до ${formatMmol(stats.max)}, ` +
        `в целевом диапазоне ${stats.tir.toLocaleString("ru-RU")} процентов времени`
    );
}

function niceScale(points) {
    const values = points.map((p) => toMmol(p[1]));
    // Нижняя и верхняя границы целевого диапазона всегда в кадре: без этого
    // ровный график «висел бы» без опоры, и зона не читалась бы.
    const min = Math.min(3, ...values);
    const max = Math.max(11, ...values);

    return { min: Math.floor(min) - 0.5, max: Math.ceil(max) + 0.5 };
}

/* Событийные дорожки рисуются только на суточном окне. За месяц отметок
   набирается сотня: они сливаются в сплошную полосу, из которой ничего не
   прочитать. На длинных окнах за события отвечает разбор ниже, а не график. */
function eventLanes() {
    if (activeRange !== "day") return [];

    const events = snapshot.events || {};
    const lanes = [];

    if ((events.meals || []).length) {
        lanes.push({
            unit: "г",
            bars: events.meals.map(([t, v]) => ({ t, v, series: SERIES.meal })),
        });
    }

    /* Короткий и длинный в одной дорожке: и то и другое меряется единицами, а
       разные шкалы для одной величины — тот самый второй вертикальный масштаб,
       который выдумывает связь на пустом месте. Различаются заливкой:
       короткий сплошной, длинный контуром. */
    const insulin = [
        ...(events.bolus || []).map(([t, v]) => ({ t, v, series: SERIES.insulin })),
        ...(events.basal || []).map(([t, v]) => ({ t, v, series: SERIES.basal })),
    ].sort((a, b) => a.t - b.t);

    if (insulin.length) {
        lanes.push({ unit: "ед", bars: insulin });
    }

    return lanes;
}

/* Столбик со скруглённой макушкой и прямым основанием: основание сидит на
   базовой линии дорожки, и округлять его — значит отрывать столбик от нуля. */
function columnPath(ctx, left, top, width, bottom) {
    const radius = Math.min(width / 2, 3, Math.max(0, bottom - top));

    ctx.beginPath();
    ctx.moveTo(left, bottom);
    ctx.lineTo(left, top + radius);
    ctx.quadraticCurveTo(left, top, left + radius, top);
    ctx.lineTo(left + width - radius, top);
    ctx.quadraticCurveTo(left + width, top, left + width, top + radius);
    ctx.lineTo(left + width, bottom);
    ctx.closePath();
}

function drawLane(ctx, lane, box, x, muted, axisAlpha) {
    const bottom = box.top + LANE_HEIGHT;

    // Базовая линия — своя у каждой дорожки: столбики растут от неё, а не от
    // чужого нуля.
    ctx.strokeStyle = muted;
    ctx.globalAlpha = 0.25;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(box.left, Math.round(bottom) + 0.5);
    ctx.lineTo(box.right, Math.round(bottom) + 0.5);
    ctx.stroke();

    // Единица в левом отступе, там же, где числа основной оси. Полное название
    // ряда даёт легенда — в 38 пикселях оно всё равно не поместится.
    ctx.globalAlpha = axisAlpha;
    ctx.fillStyle = muted;
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(lane.unit, box.left - 8, box.top + LANE_HEIGHT / 2);
    ctx.globalAlpha = 1;

    // Запас сверху, чтобы подпись самого высокого столбика не упиралась в
    // соседнюю дорожку.
    const peak = Math.max(...lane.bars.map((bar) => bar.v));
    const scale = peak > 0 ? (LANE_HEIGHT - 12) / peak : 0;

    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";

    const visible = [];

    for (const bar of lane.bars) {
        const centre = x(bar.t);
        if (centre < box.left - COLUMN_WIDTH || centre > box.right + COLUMN_WIDTH) continue;

        // Минимальная высота: доза в половину единицы иначе рисуется в ноль
        // пикселей и выглядит как пропущенная запись.
        const top = bottom - Math.max(3, bar.v * scale);
        const left = centre - COLUMN_WIDTH / 2;
        const color = readColor(bar.series.token, bar.series.fallback);

        columnPath(ctx, left, top, COLUMN_WIDTH, bottom);
        if (bar.series.hollow) {
            ctx.strokeStyle = color;
            ctx.lineWidth = 1.5;
            ctx.stroke();
        } else {
            ctx.fillStyle = color;
            ctx.fill();
        }

        visible.push({ bar, centre, top });
    }

    /* Подписи — вторым проходом, и только там, где число никуда не упирается:
       у дорожки нет своей оси, подпись и есть шкала, но два обеда через полчаса
       на суточном окне разделяют полтора десятка пикселей — меньше, чем занимает
       само число. Порядок по убыванию: место достаётся крупному приёму, мелкий
       рядом читается наведением и таблицей разбора. */
    ctx.fillStyle = muted;
    ctx.globalAlpha = axisAlpha;

    const taken = [];
    for (const { bar, centre, top } of [...visible].sort((a, b) => b.bar.v - a.bar.v)) {
        const text = formatAmount(bar.v);
        const half = ctx.measureText(text).width / 2 + LABEL_GAP;
        const span = [centre - half, centre + half];

        if (taken.some(([from, to]) => from < span[1] && span[0] < to)) continue;

        taken.push(span);
        ctx.fillText(text, centre, top - 2);
    }
    ctx.globalAlpha = 1;
}

function formatAmount(value) {
    return value.toLocaleString("ru-RU", { maximumFractionDigits: 1 });
}

function drawChart() {
    const series = snapshot.series[activeRange];
    const points = series.points;
    const lanes = eventLanes();

    els.chartEmpty.hidden = points.length > 0;
    els.canvas.setAttribute("aria-label", chartDescription(points));
    renderLegend(lanes);

    const canvas = els.canvas;
    // Холст растёт вместе с дорожками. Подписи оси обязаны остаться внутри:
    // не хватит высоты — и у карточки заведётся собственная полоса прокрутки.
    canvas.style.height = `${PLOT_HEIGHT + lanes.length * (LANE_HEIGHT + LANE_GAP)}px`;

    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;

    canvas.width = width * ratio;
    canvas.height = height * ratio;

    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    if (!points.length) {
        geometry = null;
        hideTip();
        return;
    }

    // bottom с запасом на вторую строку подписи — дату на смене дня.
    const padding = { top: 12, right: 12, bottom: 38, left: 38 };
    const plotWidth = width - padding.left - padding.right;
    const lanesHeight = lanes.length * (LANE_HEIGHT + LANE_GAP);
    const plotHeight = height - padding.top - padding.bottom - lanesHeight;

    const scale = niceScale(points);
    const now = snapshot.generated_at;
    const spanSeconds = { day: 86400, week: 7 * 86400, month: 30 * 86400 }[activeRange];
    const startTime = now - spanSeconds;

    const x = (t) => padding.left + ((t - startTime) / spanSeconds) * plotWidth;
    const y = (mmol) =>
        padding.top + plotHeight - ((mmol - scale.min) / (scale.max - scale.min)) * plotHeight;

    const styles = getComputedStyle(document.documentElement);

    const muted = readColor("--muted", "#8a90a6");
    const accent = readColor("--accent", "#7eb8f7");
    const inRange = readColor("--in-range", "#7efcb0");
    const axisAlpha = readNumber("--axis-alpha", 0.85);

    // Целевой диапазон — подложка, а не линии: так видно «сколько времени
    // график провёл внутри», не считая пересечения глазами.
    const targetTop = y(toMmol(snapshot.target.high));
    const targetBottom = y(toMmol(snapshot.target.low));
    ctx.fillStyle = inRange;
    ctx.globalAlpha = Number(styles.getPropertyValue("--band-alpha")) || 0.07;
    ctx.fillRect(padding.left, targetTop, plotWidth, targetBottom - targetTop);
    ctx.globalAlpha = 1;

    // Горизонтальная сетка с подписями в ммоль/л.
    ctx.strokeStyle = muted;
    ctx.fillStyle = muted;
    ctx.font = '11px "JetBrains Mono", monospace';
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.lineWidth = 1;

    // Шаг сетки подстраивается под размах: единичный выброс за 20 ммоль/л
    // иначе расчерчивал бы панель дюжиной линий.
    const gridStep = scale.max - scale.min > 12 ? 4 : 2;
    for (let value = Math.ceil(scale.min / gridStep) * gridStep; value <= scale.max; value += gridStep) {
        const lineY = Math.round(y(value)) + 0.5;
        ctx.globalAlpha = 0.15;
        ctx.beginPath();
        ctx.moveTo(padding.left, lineY);
        ctx.lineTo(width - padding.right, lineY);
        ctx.stroke();

        ctx.globalAlpha = axisAlpha;
        ctx.fillText(String(value), padding.left - 8, lineY);
    }
    ctx.globalAlpha = 1;

    // Подписи по времени. Крайние выравниваются внутрь, иначе первая и
    // последняя наполовину уходят за край холста.
    ctx.textBaseline = "top";
    // На узком холсте пять подписей сливаются в сплошную строку цифр —
    // «04:0610:06». Лучше меньше делений, чем нечитаемые.
    const ticks = plotWidth < 340 ? 2 : activeRange === "day" ? 4 : 5;
    let previousDay = null;

    for (let i = 0; i <= ticks; i += 1) {
        const t = startTime + (spanSeconds / ticks) * i;
        const date = new Date(t * 1000);
        const day = date.toLocaleDateString("ru-RU", {
            timeZone: TIMEZONE,
            day: "numeric",
            month: "short",
        });

        // Суточное окно пересекает полночь, и без даты непонятно, «02:35» —
        // это сегодня или вчера. Дата подписывается там, где день меняется,
        // а не у каждой метки: повторять её пять раз незачем.
        const labels =
            activeRange === "day"
                ? [
                      date.toLocaleTimeString("ru-RU", {
                          timeZone: TIMEZONE,
                          hour: "2-digit",
                          minute: "2-digit",
                      }),
                  ]
                : [day];

        if (activeRange === "day" && day !== previousDay) {
            labels.push(day);
        }
        previousDay = day;

        ctx.textAlign = i === 0 ? "left" : i === ticks ? "right" : "center";
        ctx.globalAlpha = axisAlpha;
        labels.forEach((text, line) => {
            ctx.fillText(text, x(t), height - padding.bottom + 8 + line * 13);
        });
    }
    ctx.globalAlpha = 1;

    // Линия. Разрыв длиннее трёх шагов означает, что сенсор молчал —
    // соединять такие точки нельзя, иначе пропуск выглядит как ровный тренд.
    const gapSeconds = series.step * 60 * 3;
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.75;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();

    let drawing = false;
    for (let i = 0; i < points.length; i += 1) {
        const [t, mgdl] = points[i];
        const px = x(t);
        const py = y(toMmol(mgdl));

        if (!drawing || t - points[i - 1][0] > gapSeconds) {
            ctx.moveTo(px, py);
            drawing = true;
        } else {
            ctx.lineTo(px, py);
        }
    }
    ctx.stroke();

    // Точка без соседей не даёт отрезка и не нарисовалась бы вовсе. Так
    // выглядит начало каждого нового сенсора — первые замеры одиночные.
    ctx.fillStyle = accent;
    for (let i = 0; i < points.length; i += 1) {
        const previous = points[i - 1];
        const next = points[i + 1];
        const isolated =
            (!previous || points[i][0] - previous[0] > gapSeconds) &&
            (!next || next[0] - points[i][0] > gapSeconds);

        if (isolated) {
            ctx.beginPath();
            ctx.arc(x(points[i][0]), y(toMmol(points[i][1])), 2.5, 0, Math.PI * 2);
            ctx.fill();
        }
    }

    // Дорожки событий — под графиком, над подписями оси.
    const laneBoxes = lanes.map((lane, index) => ({
        lane,
        left: padding.left,
        right: width - padding.right,
        top: padding.top + plotHeight + LANE_GAP + index * (LANE_HEIGHT + LANE_GAP),
    }));

    for (const box of laneBoxes) {
        drawLane(ctx, box.lane, box, x, muted, axisAlpha);
    }

    // Геометрия нужна обработчику наведения: пересчитывать её на каждое
    // движение мыши — значит дублировать всю раскладку и однажды разойтись
    // с тем, что нарисовано.
    geometry = {
        points,
        laneBoxes,
        x,
        y,
        startTime,
        spanSeconds,
        plotTop: padding.top,
        plotBottom: padding.top + plotHeight,
        left: padding.left,
        right: width - padding.right,
        bottom: height - padding.bottom,
    };

    drawCrosshair(ctx, muted);
}

/* Вертикаль через график и обе дорожки: она связывает столбик еды с точкой на
   кривой, ради чего всё это и рисуется рядом. */
function drawCrosshair(ctx, muted) {
    if (hoverTime === null || !geometry) return;

    const px = Math.round(geometry.x(hoverTime)) + 0.5;
    if (px < geometry.left || px > geometry.right) return;

    ctx.strokeStyle = muted;
    ctx.globalAlpha = 0.45;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px, geometry.plotTop);
    ctx.lineTo(px, geometry.bottom);
    ctx.stroke();
    ctx.globalAlpha = 1;

    const point = nearestPoint(hoverTime);
    if (!point) return;

    // Кольцо цветом панели: без него точка теряется там, где пересекает
    // собственную линию.
    ctx.beginPath();
    ctx.arc(geometry.x(point[0]), geometry.y(toMmol(point[1])), 4, 0, Math.PI * 2);
    ctx.fillStyle = readColor("--accent", "#7eb8f7");
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = readColor("--panel", "#0d0d14");
    ctx.stroke();
}

/* ── Легенда и подсказка ───────────────────────────────────────────── */

function legendItem(series) {
    const item = document.createElement("li");
    item.className = "legend__item";

    const key = document.createElement("span");
    key.className = series.hollow
        ? "legend__key legend__key--hollow"
        : `legend__key${series.line ? " legend__key--line" : ""}${
              series.thick ? " legend__key--thick" : ""
          }`;
    // Контурная метка красится через currentColor — так одна и та же переменная
    // задаёт и заливку, и рамку.
    key.style.color = `var(${series.token})`;
    key.style.background = `var(${series.token})`;

    const text = document.createElement("span");
    text.textContent = series.label;

    item.append(key, text);
    return item;
}

/* Легенда есть всегда, когда рядов больше одного: опознавать их по цвету на
   глаз — единственный канал, который отказывает и при дальтонизме, и на
   распечатке. Один ряд легенды не требует — его называет заголовок. */
function renderLegend(lanes) {
    if (!lanes.length) {
        els.legend.hidden = true;
        els.legend.replaceChildren();
        return;
    }

    const events = snapshot.events || {};
    const shown = [{ ...SERIES.glucose, line: true }];
    if ((events.meals || []).length) shown.push(SERIES.meal);
    if ((events.bolus || []).length) shown.push(SERIES.insulin);
    if ((events.basal || []).length) shown.push(SERIES.basal);

    els.legend.replaceChildren(...shown.map(legendItem));
    els.legend.hidden = false;
}

function nearestPoint(t) {
    if (!geometry || !geometry.points.length) return null;

    let best = null;
    for (const point of geometry.points) {
        const distance = Math.abs(point[0] - t);
        if (best === null || distance < best[0]) best = [distance, point];
    }
    // Дальше получаса — это уже другой участок кривой, а не то, на что навели.
    return best[0] > 1800 ? null : best[1];
}

function tipRow(series, text) {
    const row = document.createElement("p");
    row.className = "tip__row";

    const key = document.createElement("span");
    key.className = "tip__key";
    key.style.background = `var(${series.token})`;
    key.style.color = `var(${series.token})`;
    if (series.hollow) {
        key.style.background = "none";
        key.style.border = "1.5px solid currentColor";
    }

    const label = document.createElement("span");
    label.textContent = text;

    row.append(key, label);
    return row;
}

function showTip(clientX) {
    if (hoverTime === null || !geometry) return hideTip();

    const point = nearestPoint(hoverTime);
    const rows = [];

    if (point) {
        rows.push(
            tipRow(SERIES.glucose, `${formatMmol(point[1])} ${SERIES.glucose.unit}`)
        );
    }

    // Событие в пределах четверти часа от курсора: столбик и точка кривой
    // почти никогда не совпадают по времени секунда в секунду.
    for (const box of geometry.laneBoxes) {
        for (const bar of box.lane.bars) {
            if (Math.abs(bar.t - hoverTime) <= 900) {
                rows.push(tipRow(bar.series, `${formatAmount(bar.v)} ${bar.series.unit}`));
            }
        }
    }

    if (!rows.length) return hideTip();

    const time = document.createElement("p");
    time.className = "tip__time";
    // Зона названа здесь, а не у оси: подсказку читают, когда сверяют запись со
    // своими часами, и «02:43» без города в поездке значит два разных момента.
    time.textContent = `${new Date(hoverTime * 1000).toLocaleTimeString("ru-RU", {
        timeZone: TIMEZONE,
        hour: "2-digit",
        minute: "2-digit",
    })}, ${TIMEZONE_LABEL}`;

    els.tip.replaceChildren(time, ...rows);
    els.tip.hidden = false;

    // Позиция считается от карточки, а не от холста: у карточки есть внутренний
    // отступ, и без поправки подсказка уезжает на его ширину.
    const chart = els.chart.getBoundingClientRect();
    const canvas = els.canvas.getBoundingClientRect();
    const width = els.tip.offsetWidth;
    const half = width / 2;
    const wanted = clientX - chart.left;

    els.tip.style.left = `${Math.min(
        Math.max(wanted, half + 4),
        chart.width - half - 4
    )}px`;
    els.tip.style.top = `${canvas.top - chart.top + geometry.plotTop}px`;
}

function hideTip() {
    els.tip.hidden = true;
}

/* ── Разбор приёмов пищи ───────────────────────────────────────────── */

/* Ряды оверлея. Отдельные кривые намеренно приглушены: они дают форму и
   разброс, а читается по ним медиана. */
const OVERLAY_SERIES = {
    single: { token: "--muted", fallback: "#8a90a6", label: "Отдельные приёмы", line: true },
    median: { token: "--accent", fallback: "#7eb8f7", label: "Медиана", line: true },
    // Цвет еды, а не акцента: акцентом нарисована медиана, и вторая синяя
    // линия читалась бы как ещё одна сводка, а не как один приём.
    picked: {
        token: "--meal",
        fallback: "#bd8a30",
        label: "Выбранный приём",
        line: true,
        thick: true,
    },
};

/* Какой приём подсвечен на оверлее. Закреплённый нажатием — состояние покоя,
   к которому подсветка возвращается; указатель и фокус поверх него только
   показывают, на что сейчас смотрят. Так строка под курсором всегда означает
   свою кривую на графике, а не иногда — в зависимости от того, закреплено ли
   что-то ещё. Приём опознаётся временем: оно уникально и переживает
   перерисовку таблицы, так что закрепление не слетает от обновления снимка.

   Указатель и фокус держатся порознь и решают последним словом: увести мышь
   со стола, не погасив кривую, выбранную с клавиатуры, — и наоборот. */
let hoveredMeal = null;
let focusedMeal = null;
let previewMeal = null;
let pinnedMeal = null;

function pickedMeal() {
    return previewMeal ?? pinnedMeal;
}

const OVERLAY_HEIGHT = 240;

// Сколько кривых должно накрыть отметку времени, чтобы медиана в ней что-то
// значила. На двух это просто среднее двух обедов, выданное за общую картину.
const MEDIAN_MIN_CURVES = 3;

function formatDelta(mgdl) {
    return toMmol(mgdl).toLocaleString("ru-RU", {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
        signDisplay: "exceptZero",
    });
}

function medianCurve(curves) {
    const buckets = new Map();

    for (const curve of curves) {
        // Кривые сенсора идут пятиминутным шагом, но у каждой свой сдвиг
        // относительно момента еды — без округления они не сложились бы.
        // Внутри слота кривая схлопывается до одного значения: сборщик пишет
        // и пятиминутную сетку, и внеочередное текущее измерение, так что без
        // схлопывания плотнее опрошенная еда весила бы в медиане вдвое, а два
        // обеда сходили бы за три кривые для MEDIAN_MIN_CURVES.
        const own = new Map();
        for (const [offset, mgdl] of curve) {
            const slot = Math.round(offset / 5) * 5;
            if (!own.has(slot)) own.set(slot, []);
            own.get(slot).push(mgdl);
        }
        for (const [slot, values] of own) {
            if (!buckets.has(slot)) buckets.set(slot, []);
            buckets.get(slot).push(values.reduce((sum, v) => sum + v, 0) / values.length);
        }
    }

    const curve = [];
    for (const [slot, values] of [...buckets].sort((a, b) => a[0] - b[0])) {
        if (values.length < MEDIAN_MIN_CURVES) continue;
        values.sort((a, b) => a - b);
        const middle = values.length >> 1;
        curve.push([
            slot,
            values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2,
        ]);
    }
    return curve;
}

function drawOverlay(analysis) {
    const canvas = els.overlay;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = OVERLAY_HEIGHT;

    canvas.width = width * ratio;
    canvas.height = height * ratio;

    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const drawn = analysis.meals.filter((meal) => meal.curve.length > 1);
    if (!drawn.length) return;

    /* Кривые приводятся к уровню в момент еды. В абсолютных значениях медиана
       выходит почти плоской: обед начинается с 6, ужин с 9, и разные исходные
       уровни гасят как раз тот подъём, ради которого всё это рисуется. */
    const curves = drawn.map((meal) =>
        meal.curve.map(([offset, mgdl]) => [offset, mgdl - meal.baseline])
    );

    const padding = { top: 12, right: 12, bottom: 26, left: 44 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const span = analysis.window_min;
    const target = toMmol(analysis.targets.rise);

    // Ориентир и ноль всегда в кадре: без них подъём не с чем сопоставить.
    const deltas = curves.flat().map(([, mgdl]) => toMmol(mgdl));
    const min = Math.floor(Math.min(-2, ...deltas));
    const max = Math.ceil(Math.max(target + 1, ...deltas));

    const x = (minutes) => padding.left + (minutes / span) * plotWidth;
    const y = (mmol) => padding.top + plotHeight - ((mmol - min) / (max - min)) * plotHeight;

    const muted = readColor("--muted", "#8a90a6");
    const accent = readColor("--accent", "#7eb8f7");
    const axisAlpha = readNumber("--axis-alpha", 0.85);

    ctx.strokeStyle = muted;
    ctx.fillStyle = muted;
    ctx.font = '11px "JetBrains Mono", monospace';
    ctx.lineWidth = 1;

    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const gridStep = max - min > 12 ? 4 : 2;
    for (let value = Math.ceil(min / gridStep) * gridStep; value <= max; value += gridStep) {
        const lineY = Math.round(y(value)) + 0.5;
        ctx.globalAlpha = 0.15;
        ctx.beginPath();
        ctx.moveTo(padding.left, lineY);
        ctx.lineTo(width - padding.right, lineY);
        ctx.stroke();

        ctx.globalAlpha = axisAlpha;
        ctx.fillText(value > 0 ? `+${value}` : String(value), padding.left - 8, lineY);
    }

    ctx.textBaseline = "top";
    for (let minutes = 0; minutes <= span; minutes += 60) {
        ctx.textAlign = minutes === 0 ? "left" : minutes === span ? "right" : "center";
        ctx.globalAlpha = axisAlpha;
        ctx.fillText(`${minutes / 60} ч`, x(minutes), height - padding.bottom + 8);
    }
    ctx.globalAlpha = 1;

    // Уровень в момент еды. Всё, что выше этой линии, — и есть подъём.
    ctx.globalAlpha = 0.5;
    ctx.beginPath();
    ctx.moveTo(padding.left, Math.round(y(0)) + 0.5);
    ctx.lineTo(width - padding.right, Math.round(y(0)) + 0.5);
    ctx.stroke();
    ctx.globalAlpha = 1;

    const stroke = (curve) => {
        ctx.beginPath();
        curve.forEach(([offset, mgdl], index) => {
            const px = x(Math.min(offset, span));
            const py = y(toMmol(mgdl));
            if (index === 0) ctx.moveTo(px, py);
            else ctx.lineTo(px, py);
        });
        ctx.stroke();
    };

    // Выбранная кривая рисуется последней, поверх медианы: подсветка, лежащая
    // под ней, теряется как раз там, где кривые сходятся плотнее всего.
    const picked = drawn.findIndex((meal) => meal.t === pickedMeal());

    // Отдельные кривые — тонкие и приглушённые: они дают разброс и форму, а
    // числа читаются в таблице. Когда одна выбрана, остальные отступают, но не
    // исчезают: ниже 0.2 разброс перестаёт читаться, а он и есть их работа.
    ctx.lineWidth = 1;
    ctx.globalAlpha = picked >= 0 ? 0.2 : 0.3;
    curves.forEach((curve, index) => {
        if (index !== picked) stroke(curve);
    });
    ctx.globalAlpha = 1;

    const median = medianCurve(curves);
    if (median.length > 1) {
        ctx.strokeStyle = accent;
        ctx.lineWidth = 2;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        stroke(median);
    }

    if (picked >= 0) {
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        // Ореол цветом панели, а под ним линия толще медианы. Цвет здесь —
        // не единственное отличие нарочно: у --meal и --accent в светлой теме
        // совпадает светлота, и на монохромном экране, при дальтонизме и на
        // распечатке кривые различались бы только шириной и просветом вокруг.
        ctx.strokeStyle = readColor("--panel", "#ffffff");
        ctx.lineWidth = 6;
        ctx.globalAlpha = 0.75;
        stroke(curves[picked]);

        ctx.globalAlpha = 1;
        ctx.strokeStyle = readColor("--meal", "#bd8a30");
        ctx.lineWidth = 3;
        stroke(curves[picked]);
    }

    // Ориентир подписывается прямо на линии: считать, какая это по счёту
    // клетка сетки, никто не станет.
    const targetY = Math.round(y(target)) + 0.5;
    ctx.strokeStyle = muted;
    ctx.lineWidth = 1;
    ctx.globalAlpha = 0.55;
    ctx.beginPath();
    ctx.moveTo(padding.left, targetY);
    ctx.lineTo(width - padding.right, targetY);
    ctx.stroke();

    ctx.globalAlpha = 1;
    ctx.fillStyle = readColor("--panel", "#ffffff");
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const label = `ориентир +${formatMmol(analysis.targets.rise)}`;
    const box = ctx.measureText(label).width + 8;
    ctx.fillRect(width - padding.right - box, targetY - 7, box, 14);
    ctx.fillStyle = muted;
    ctx.fillText(label, width - padding.right - 4, targetY);

    // Что именно выделено, из картинки не прочитать — говорим словами: иначе
    // кнопка сообщает «нажато», а чем это кончилось на холсте, неизвестно.
    const pickedAt = picked >= 0 ? formatDateTime(new Date(drawn[picked].t * 1000)) : null;
    canvas.setAttribute(
        "aria-label",
        `Отклонение глюкозы от уровня в момент еды после ${drawn.length} приёмов пищи. ` +
            (pickedAt ? `Выделен приём ${pickedAt}. ` : "") +
            "Все значения перечислены в таблице ниже."
    );

    const legend = [legendItem(OVERLAY_SERIES.single), legendItem(OVERLAY_SERIES.median)];
    // Ряд появляется только когда есть что называть: пустая строка легенды
    // обещала бы линию, которой на холсте нет.
    if (picked >= 0) legend.push(legendItem(OVERLAY_SERIES.picked));
    els.overlayLegend.replaceChildren(...legend);
}

/* Четыре состояния, и приглушены из них те два, где мерить было нечего.
   Подъём выше ориентира — исход, а не отсутствие исхода, и выглядеть как
   «окно не закрылось» он не должен; но и цветом он не выделен: у этого
   дневника такие приёмы — большинство, а раскрашенное большинство глушит как
   раз редкое — гипогликемию и попадание в ориентир. Отличает его стрелка:
   она читается и на монохроме, и при дальтонизме. */
function outcome(meal, targets) {
    if (meal.hypo) return ["flag--hypo", "⚠ гипогликемия"];
    if (!meal.complete) return ["flag--skip", "· окно не закрылось"];
    if (meal.cut) return ["flag--skip", "· прервано следующей едой"];
    if (meal.rise <= targets.rise) return ["flag--ok", "✓ в ориентире"];
    return ["flag--over", "↑ подъём выше ориентира"];
}

/* Болюс еды: доза и её упреждение. Абсолютное время укола не нужно — оно
   читается из колонки «Когда», а связка «сколько и за сколько до еды» — то,
   ради чего колонка существует. */

// Укол в пределах пяти минут от еды — «с едой»: журнал ведётся руками, и пара
// минут в нём — точность записи, а не осмысленное упреждение.
const DOSE_WITH_MEAL_MIN = 5;

function formatDose(dose) {
    if (!dose) return "—";
    // Неразрывные пробелы внутри половин: ячейка может сложиться в две строки
    // «7,2 ед / за 15 мин до», но не оставить «до» болтаться на своей.
    const units = `${formatAmount(dose.units)} ед`;
    if (dose.lead_min > DOSE_WITH_MEAL_MIN) return `${units} за ${dose.lead_min} мин до`;
    if (dose.lead_min < -DOSE_WITH_MEAL_MIN) return `${units} через ${-dose.lead_min} мин`;
    return `${units} с едой`;
}

function textCell(text) {
    const cell = document.createElement("td");
    cell.textContent = text;
    return cell;
}

/* Приём, записанный в несколько заходов, разбирается как одна еда, а на
   дорожке главного графика его записи стоят порознь. Знак суммы объясняет
   расхождение и под наведением перечисляет заходы. Стоит он перед числом:
   колонка выровнена по правому краю, и хвостовая пометка сдвигала бы числа
   друг относительно друга — то самое выравнивание, ради которого колонка и
   набрана моноширинными цифрами. */
/* Уверенность в числе — два разных факта, и показывать нужно оба. Чем число
   получено, шкала не заменяет: «взвешено» и «по фото, прогоны сошлись» одинаково
   надёжны сейчас, но исправлять по ним разное — оценку стоит перевесить, весы
   уже всё сказали. А одним источником не обойтись: у фото разброс прогонов
   меняется от приёма к приёму, и «оценено по фото» без него ничего не обещает.
   Отсюда пара знаков — источник и три деления, — а не один общий значок.

   Уровни и пороги живут в analysis.trust_level, здесь только показ; порядок
   TRUST_ORDER при этом сжимается: весы и сошедшиеся прогоны делят верхнее
   деление, слово человека и разошедшиеся — среднее. Различает их источник. */
const TRUST = {
    weighed: { source: "⚖︎", dots: 3, spoken: "взвешено" },
    manual: { source: "✎", dots: 2, spoken: "со слов" },
    ok: { source: "▣", dots: 3, spoken: "по фото, прогоны сошлись" },
    medium: { source: "▣", dots: 2, spoken: "по фото, прогоны разошлись" },
    low: { source: "▣", dots: 1, spoken: "по фото, прогоны сильно разошлись" },
};

const TRUST_DOTS = 3;

function trustMark(cell, meal) {
    const trust = TRUST[meal.trust];
    if (!trust) return cell;

    const mark = document.createElement("span");
    mark.className = `trust trust--${meal.trust}`;
    mark.title = trust.spoken;
    mark.setAttribute("aria-hidden", "true");

    // Две части, а не одна строка: источник и шкала набраны разным кеглем —
    // ⚖︎ и ✎ рисуются заметно мельче точек при одном размере.
    const source = document.createElement("span");
    source.className = "trust__source";
    source.textContent = trust.source;

    const dots = document.createElement("span");
    dots.className = "trust__dots";
    dots.textContent = "●".repeat(trust.dots) + "○".repeat(TRUST_DOTS - trust.dots);

    mark.append(source, dots);

    const spoken = document.createElement("span");
    spoken.className = "visually-hidden";
    spoken.textContent = `, ${trust.spoken}`;

    cell.prepend(mark, " ");
    cell.append(spoken);
    return cell;
}

function carbsCell(meal) {
    const cell = textCell(`${formatAmount(meal.carbs)} г`);
    if (!meal.parts) return trustMark(cell, meal);

    const sittings = meal.parts
        .map(([seconds, carbs]) => {
            const at = new Date(seconds * 1000).toLocaleTimeString("ru-RU", {
                timeZone: TIMEZONE,
                hour: "2-digit",
                minute: "2-digit",
            });
            return `${at} — ${formatAmount(carbs)} г`;
        })
        .join(", ");

    // Знак — только для глаз: Σ ничего не сокращает, так что <abbr> здесь ни
    // при чём, а озвучивать «греческая заглавная сигма» перед числом незачем.
    const mark = document.createElement("span");
    mark.className = "parts";
    mark.textContent = "Σ";
    mark.title = sittings;
    mark.setAttribute("aria-hidden", "true");

    // Состав словами: title не показывается на телефоне и не читается вслух,
    // а без него знак остаётся необъяснённым.
    const spoken = document.createElement("span");
    spoken.className = "visually-hidden";
    spoken.textContent = `, сложено из записей: ${sittings}`;

    cell.prepend(mark, " ");
    cell.append(spoken);
    return trustMark(cell, meal);
}

function renderMeals(analysis) {
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const title of ["Когда", "Углеводы", "Инсулин", "Подъём", "Пик через", "Возврат", "Исход"]) {
        const cell = document.createElement("th");
        cell.scope = "col";
        cell.textContent = title;
        headRow.append(cell);
    }
    head.append(headRow);

    const body = document.createElement("tbody");
    // Свежие сверху: разбор читают сразу после еды, а не спустя две недели.
    for (const meal of [...analysis.meals].reverse()) {
        const row = document.createElement("tr");
        const [flagClass, flagText] = outcome(meal, analysis.targets);

        // Наведение показывает кривую по всей строке — вести курсор в одну
        // колонку никто не станет. Но нажимается настоящая кнопка внутри
        // первой ячейки: у неё есть роль, состояние и клавиатура даром, а
        // восьмой колонки ради этого заводить не пришлось.
        //
        // Кнопки нет там, где нечего показать: у только что записанной еды в
        // окне ещё нет двух измерений, и оверлей её не рисует. Такая строка
        // стоит первой — то есть выбирать нечего ровно тогда, когда разбор
        // открывают чаще всего, — и нажатие обещало бы кривую, которой нет.
        const drawable = meal.curve.length > 1;
        row.dataset.meal = meal.t;
        if (drawable) {
            row.addEventListener("mouseenter", () => setHovered(meal.t));
            row.addEventListener("mouseleave", () => setHovered(null));
        }

        const when = document.createElement("td");
        const at = new Date(meal.t * 1000);
        if (drawable) {
            const pick = document.createElement("button");
            pick.type = "button";
            pick.className = "meals__pick";
            pick.textContent = formatCellDateTime(at);
            // Имя кнопки не меняется вместе с состоянием: состояние говорит
            // aria-pressed, и «снять подсветку… нажато» звучало бы так, будто
            // снятие уже произошло.
            //
            // Дата в имени длинная, а не та, что на экране: колонке нужна
            // короткая, но вслух «01.09» читается как «ноль один точка ноль
            // девять» — и приём перестаёт называться днём.
            pick.setAttribute(
                "aria-label",
                `${formatDateTime(at)} — подсветить кривую на графике`
            );
            pick.addEventListener("click", () => togglePinned(meal.t));
            pick.addEventListener("focus", () => setFocused(meal.t));
            pick.addEventListener("blur", () => setFocused(null));
            when.append(pick);
        } else {
            when.textContent = formatCellDateTime(at);
        }

        row.append(
            when,
            carbsCell(meal),
            textCell(formatDose(meal.dose)),
            textCell(formatDelta(meal.rise)),
            textCell(`${meal.peak_min} мин`),
            textCell(meal.ret === null ? "—" : formatDelta(meal.ret))
        );

        const flagCell = document.createElement("td");
        const flag = document.createElement("span");
        // Значок и слово рядом с цветом: состояние, названное одним цветом, не
        // названо никак.
        flag.className = `flag ${flagClass}`;
        flag.textContent = flagText;
        flagCell.append(flag);
        row.append(flagCell);

        body.append(row);
    }

    // Снимок перечитывается раз в минуту, и таблица пересобирается целиком —
    // вместе с кнопкой, на которой стоял фокус. Без возврата фокус раз в
    // минуту улетал бы на body: до появления кнопок это было незаметно.
    const focused = document.activeElement;
    const keepFocus =
        focused && focused.classList.contains("meals__pick")
            ? focused.closest("tr").dataset.meal
            : null;

    const caption = els.meals.querySelector("caption");
    els.meals.replaceChildren(...(caption ? [caption] : []), head, body);

    if (keepFocus) {
        const restored = body.querySelector(`tr[data-meal="${keepFocus}"] .meals__pick`);
        // preventScroll: возврат фокуса не должен утаскивать страницу к
        // таблице, если читатель успел уйти взглядом выше.
        if (restored) restored.focus({ preventScroll: true });
    }

    markPicked();
}

function setHovered(t) {
    hoveredMeal = t;
    // Ушёл указатель — остаётся то, что держит фокус, и наоборот.
    previewMeal = t ?? focusedMeal;
    refreshPicked();
}

function setFocused(t) {
    focusedMeal = t;
    previewMeal = t ?? hoveredMeal;
    refreshPicked();
}

function togglePinned(t) {
    pinnedMeal = pinnedMeal === t ? null : t;
    refreshPicked();
}

/* Отметить выбранную строку, не трогая холст: наведение при закреплённой
   кривой меняет только таблицу, и перерисовывать оверлей незачем. */
function markPicked() {
    const picked = pickedMeal();
    for (const row of els.meals.querySelectorAll("tbody tr")) {
        const t = Number(row.dataset.meal);
        row.classList.toggle("is-picked", t === picked);
        row.classList.toggle("is-pinned", t === pinnedMeal);

        const pick = row.querySelector(".meals__pick");
        if (pick) pick.setAttribute("aria-pressed", String(t === pinnedMeal));
    }
}

function refreshPicked() {
    markPicked();
    if (snapshot && snapshot.analysis) drawOverlay(snapshot.analysis);
}

function renderReviewStats(analysis) {
    const summary = analysis.summary;
    els.reviewStats.replaceChildren();

    if (!summary) {
        els.reviewStats.hidden = true;
        return;
    }

    const targets = analysis.targets;
    const cards = [
        statCard(
            "Разобрано приёмов",
            String(summary.count),
            summary.skipped
                ? `пропущено ${summary.skipped}: окно не закрылось или прервано`
                : sinceLabel(new Date(analysis.meals[0].t * 1000))
        ),
    ];

    // Медианы бывают null — когда ни одно окно не дожило до конца чистым.
    // Карточек с прочерками не рисуем: отсутствие честнее выдуманного нуля.
    if (summary.rise !== null) {
        cards.push(
            statCard(
                "Подъём, медиана",
                formatDelta(summary.rise),
                `ммоль/л, ориентир до ${formatMmol(targets.rise)}`
            )
        );
    }
    if (summary.peak_min !== null) {
        cards.push(
            statCard(
                "Пик через",
                `${summary.peak_min} мин`,
                `обычно ${targets.peak_min[0]}–${targets.peak_min[1]} мин`
            )
        );
    }

    cards.push(
        statCard(
            "С гипогликемией",
            // ?? — снимок от прежнего сборщика мог ещё не знать total.
            `${summary.hypo} из ${summary.total ?? summary.count}`,
            `ниже ${formatMmol(targets.hypo)} ммоль/л, считая незакрытые и прерванные окна`
        )
    );

    if (summary.count) {
        cards.push(
            statCard(
                "Уложились в ориентир",
                `${summary.good} из ${summary.count}`,
                "подъём в пределах ориентира и без гипогликемии"
            )
        );
    }

    els.reviewStats.append(...cards);
    els.reviewStats.hidden = false;
}

function renderReview() {
    const analysis = snapshot.analysis;

    // Ни одного разобранного приёма пищи — секции просто нет. Пустая таблица с
    // прочерками сообщает не больше, чем её отсутствие, а места занимает экран.
    if (!analysis || !analysis.meals.length) {
        els.review.hidden = true;
        return;
    }

    const reviewSince = sinceLabel(new Date(analysis.meals[0].t * 1000));
    els.reviewNote.textContent =
        `На сколько глюкоза отклонялась от уровня в момент еды в течение ` +
        `${analysis.window_min / 60} часов после каждого приёма пищи ${reviewSince}. ` +
        "Это описание исхода, а не оценка дозы: на результат влияют и " +
        "активность, и болезнь, и остаток предыдущей дозы — ничего из этого здесь нет.";

    // Закреплённый приём мог уехать из снимка: окно разбора движется, и раз в
    // минуту страница перечитывает его заново. Проверяется не наличие в
    // таблице, а рисуемость: строка может остаться, а кривая — сократиться до
    // точки, и подсветка повисла бы ни на чём.
    const drawable = analysis.meals.some(
        (meal) => meal.t === pinnedMeal && meal.curve.length > 1
    );
    if (pinnedMeal !== null && !drawable) pinnedMeal = null;

    // Раскрыть до отрисовки: у скрытой секции холст имеет нулевую ширину, и
    // рисовать в него — значит рисовать в ничто.
    els.review.hidden = false;

    renderReviewStats(analysis);
    drawOverlay(analysis);
    renderMeals(analysis);
}

/* ── Загрузка и события ────────────────────────────────────────────── */

/* Состояние сборщика, а не возраст файла. Снимок перезаписывается каждые
   пять минут независимо от того, отвечает ли Abbott, поэтому «обновлено
   только что» само по себе ничего не говорит о свежести данных. */
function renderCollectorState() {
    const lastSuccess = (snapshot.collector || {}).last_success;

    if (!lastSuccess) {
        els.footUpdated.textContent = "Сборщик ещё не получал данные";
        els.footUpdated.className = "foot__warn";
        return;
    }

    const silence = Date.now() - lastSuccess * 1000;
    if (silence > COLLECTOR_SILENT_AFTER_MS) {
        els.footUpdated.textContent =
            `Нет связи с LibreLinkUp. Последний успешный опрос — ${formatDateTime(new Date(lastSuccess * 1000))}.`;
        els.footUpdated.className = "foot__warn";
        return;
    }

    els.footUpdated.textContent = `Обновлено ${formatAgo(silence)}`;
    els.footUpdated.className = "";
}

function render() {
    // Баннер живёт до первой удачной отрисовки: страница перечитывает снимок
    // каждую минуту, и один моргнувший fetch не должен навсегда повесить
    // «не удалось загрузить» над живыми данными. renderNow() вернёт баннер,
    // если показывать по-прежнему нечего.
    els.empty.hidden = true;

    renderNow();
    // До проверки на пустые данные: когда замеров нет вовсе, знать, жив ли
    // сборщик, тем более важно.
    renderCollectorState();

    if (!snapshot.latest) return;

    els.ranges.hidden = false;
    els.chart.hidden = false;
    drawChart();
    renderStats();
    // Разбор не зависит от выбранного окна графика: у него свой период,
    // названный в его же заметке, поэтому кнопки его не перерисовывают.
    renderReview();
}

async function load() {
    try {
        // Кэш снимка живёт минуту на стороне nginx; no-store не даёт браузеру
        // держать его дольше и показывать вчерашний сахар как текущий.
        const response = await fetch("data.json", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        snapshot = await response.json();
        render();
    } catch (error) {
        els.empty.textContent = "Не удалось загрузить данные. Обновите страницу позже.";
        els.empty.hidden = false;
        console.error(error);
    }
}

els.ranges.addEventListener("click", (event) => {
    const button = event.target.closest(".ranges__btn");
    if (!button) return;

    activeRange = button.dataset.range;
    for (const item of els.ranges.children) {
        const active = item === button;
        item.classList.toggle("is-active", active);
        // Подсветка сообщает о выборе только глазами; aria-pressed — всем
        // остальным.
        item.setAttribute("aria-pressed", String(active));
    }
    drawChart();
    renderStats();
});

window.addEventListener("resize", () => {
    if (snapshot && snapshot.latest) {
        drawChart();
        renderReview();
    }
});

function hoverAt(event) {
    if (!geometry) return;

    const rect = els.canvas.getBoundingClientRect();
    const px = event.clientX - rect.left;
    if (px < geometry.left || px > geometry.right) {
        clearHover();
        return;
    }

    const share = (px - geometry.left) / (geometry.right - geometry.left);
    hoverTime = Math.round(geometry.startTime + share * geometry.spanSeconds);
    drawChart();
    showTip(event.clientX);
}

function clearHover() {
    if (hoverTime === null) return;
    hoverTime = null;
    hideTip();
    if (snapshot && snapshot.latest) drawChart();
}

/* pointer, а не mouse: тем же обработчиком обслуживается касание, и на телефоне
   подсказка появляется по тапу вместо того, чтобы быть недоступной вовсе.
   Отдельно pointerdown, потому что тап без движения не рождает pointermove:
   мышь наводят, а пальцем именно тыкают. */
els.canvas.addEventListener("pointerdown", hoverAt);
els.canvas.addEventListener("pointermove", hoverAt);

/* Три события на снятие. pointerleave отвечает за мышь, pointerup — за
   отпущенный палец, pointercancel — за случай, когда жест забрал себе браузер:
   без него подсказка осталась бы висеть над уехавшей страницей. */
els.canvas.addEventListener("pointerleave", clearHover);
els.canvas.addEventListener("pointerup", clearHover);
els.canvas.addEventListener("pointercancel", clearHover);

els.theme.addEventListener("click", () => {
    const next = THEMES[(THEMES.findIndex((item) => item.id === theme) + 1) % THEMES.length];
    applyTheme(next.id);
    rememberTheme(next.id);
});

/* Пока выбор не сохранён, страница продолжает следовать системе: на телефоне
   она переключается по расписанию, в том числе посреди чтения. */
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (event) => {
    if (!storedTheme()) applyTheme(event.matches ? "dark" : "light");
});

applyTheme(initialTheme());

load();
setInterval(load, RELOAD_INTERVAL_MS);
