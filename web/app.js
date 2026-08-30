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

// Высота холста без дорожек событий — та же, что была до их появления.
const PLOT_HEIGHT = 340;
const LANE_HEIGHT = 34;
const LANE_GAP = 8;
const COLUMN_WIDTH = 7;

// Больше шести столбиков — и подписи над ними начинают наезжать друг на друга.
// Тогда подписывается только самый крупный, остальное читается наведением и
// таблицей разбора.
const LABEL_LIMIT = 6;

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
        day: "numeric",
        month: "long",
        hour: "2-digit",
        minute: "2-digit",
    });
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
    const labelAll = lane.bars.length <= LABEL_LIMIT;

    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";

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

        // Подписи — только пока их немного: у дорожки нет своей оси, и при
        // четырёх столбиках подпись и есть шкала. Дальше числа сливаются, и
        // их читают наведением и таблицей разбора.
        if (labelAll || bar.v === peak) {
            ctx.fillStyle = muted;
            ctx.globalAlpha = axisAlpha;
            ctx.fillText(formatAmount(bar.v), centre, top - 2);
            ctx.globalAlpha = 1;
        }
    }
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
        const day = date.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });

        // Суточное окно пересекает полночь, и без даты непонятно, «02:35» —
        // это сегодня или вчера. Дата подписывается там, где день меняется,
        // а не у каждой метки: повторять её пять раз незачем.
        const labels =
            activeRange === "day"
                ? [date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })]
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
        : `legend__key${series.line ? " legend__key--line" : ""}`;
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
    time.textContent = new Date(hoverTime * 1000).toLocaleTimeString("ru-RU", {
        hour: "2-digit",
        minute: "2-digit",
    });

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
};

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
        for (const [offset, mgdl] of curve) {
            // Кривые сенсора идут пятиминутным шагом, но у каждой свой сдвиг
            // относительно момента еды — без округления они не сложились бы.
            const slot = Math.round(offset / 5) * 5;
            if (!buckets.has(slot)) buckets.set(slot, []);
            buckets.get(slot).push(mgdl);
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

    // Отдельные кривые — тонкие и приглушённые: они дают разброс и форму, а
    // числа читаются в таблице.
    ctx.lineWidth = 1;
    ctx.globalAlpha = 0.3;
    for (const curve of curves) {
        ctx.beginPath();
        curve.forEach(([offset, mgdl], index) => {
            const px = x(Math.min(offset, span));
            const py = y(toMmol(mgdl));
            if (index === 0) ctx.moveTo(px, py);
            else ctx.lineTo(px, py);
        });
        ctx.stroke();
    }
    ctx.globalAlpha = 1;

    const median = medianCurve(curves);
    if (median.length > 1) {
        ctx.strokeStyle = accent;
        ctx.lineWidth = 2;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.beginPath();
        median.forEach(([offset, mgdl], index) => {
            const px = x(Math.min(offset, span));
            const py = y(toMmol(mgdl));
            if (index === 0) ctx.moveTo(px, py);
            else ctx.lineTo(px, py);
        });
        ctx.stroke();
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

    canvas.setAttribute(
        "aria-label",
        `Отклонение глюкозы от уровня в момент еды после ${drawn.length} приёмов пищи. ` +
            "Все значения перечислены в таблице ниже."
    );

    els.overlayLegend.replaceChildren(
        legendItem(OVERLAY_SERIES.single),
        legendItem(OVERLAY_SERIES.median)
    );
}

function outcome(meal, targets) {
    if (meal.hypo) return ["flag--hypo", "⚠ гипогликемия"];
    if (!meal.complete) return ["flag--skip", "· окно не закрылось"];
    if (meal.cut) return ["flag--skip", "· прервано следующей едой"];
    if (meal.rise <= targets.rise) return ["flag--ok", "✓ в ориентире"];
    return ["flag--skip", "· подъём выше ориентира"];
}

function renderMeals(analysis) {
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const title of ["Когда", "Углеводы", "Подъём", "Пик через", "Возврат", "Исход"]) {
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

        const cells = [
            formatDateTime(new Date(meal.t * 1000)),
            `${formatAmount(meal.carbs)} г`,
            `${formatDelta(meal.rise)}`,
            `${meal.peak_min} мин`,
            meal.ret === null ? "—" : formatDelta(meal.ret),
        ];

        for (const text of cells) {
            const cell = document.createElement("td");
            cell.textContent = text;
            row.append(cell);
        }

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

    const caption = els.meals.querySelector("caption");
    els.meals.replaceChildren(...(caption ? [caption] : []), head, body);
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
                ? `пропущено ${summary.skipped}: окно не закрылось или наложилось`
                : "за последние две недели"
        ),
        statCard(
            "Подъём, медиана",
            formatDelta(summary.rise),
            `ммоль/л, ориентир до ${formatMmol(targets.rise)}`
        ),
        statCard(
            "Пик через",
            `${summary.peak_min} мин`,
            `обычно ${targets.peak_min[0]}–${targets.peak_min[1]} мин`
        ),
        statCard(
            "С гипогликемией",
            `${summary.hypo} из ${summary.count}`,
            `ниже ${formatMmol(targets.hypo)} ммоль/л в течение 4 часов`
        ),
        statCard(
            "Уложились в ориентир",
            `${summary.good} из ${summary.count}`,
            "подъём в пределах ориентира и без гипогликемии"
        ),
    ];

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

    els.reviewNote.textContent =
        `На сколько глюкоза отклонялась от уровня в момент еды в течение ` +
        `${analysis.window_min / 60} часов после каждого приёма пищи за последние две ` +
        "недели. Это описание исхода, а не оценка дозы: на результат влияют и то, за " +
        "сколько до еды сделан укол, и активность, и остаток предыдущей дозы — ничего " +
        "из этого здесь нет.";

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
    renderNow();
    // До проверки на пустые данные: когда замеров нет вовсе, знать, жив ли
    // сборщик, тем более важно.
    renderCollectorState();

    if (!snapshot.latest) return;

    els.ranges.hidden = false;
    els.chart.hidden = false;
    drawChart();
    renderStats();
    // Разбор не зависит от выбранного окна: он всегда за две недели, поэтому
    // кнопки периода его не перерисовывают.
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

/* pointer, а не mouse: тем же обработчиком обслуживается касание, и на телефоне
   подсказка появляется по тапу вместо того, чтобы быть недоступной вовсе. */
els.canvas.addEventListener("pointermove", (event) => {
    if (!geometry) return;

    const rect = els.canvas.getBoundingClientRect();
    const px = event.clientX - rect.left;
    if (px < geometry.left || px > geometry.right) {
        hoverTime = null;
        drawChart();
        hideTip();
        return;
    }

    const share = (px - geometry.left) / (geometry.right - geometry.left);
    hoverTime = Math.round(geometry.startTime + share * geometry.spanSeconds);
    drawChart();
    showTip(event.clientX);
});

els.canvas.addEventListener("pointerleave", () => {
    hoverTime = null;
    hideTip();
    if (snapshot && snapshot.latest) drawChart();
});

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
