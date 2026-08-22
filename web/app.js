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

const RANGE_LABELS = { day: "24 часа", week: "7 дней", month: "30 дней" };

let snapshot = null;
let activeRange = "day";

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
    footUpdated: document.getElementById("foot-updated"),
};

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
        statCard("В целевом диапазоне", `${stats.tir.toLocaleString("ru-RU")} %`,
            `ниже ${stats.below.toLocaleString("ru-RU")} % · выше ${stats.above.toLocaleString("ru-RU")} %`),
        statCard("Среднее", formatMmol(stats.avg), "ммоль/л"),
        statCard("Разброс", `${formatMmol(stats.min)} – ${formatMmol(stats.max)}`, "ммоль/л, минимум и максимум"),
        statCard("Вариабельность", `${stats.cv.toLocaleString("ru-RU")} %`,
            stats.cv <= 36 ? "стабильно, норма ≤ 36 %" : "выше нормы ≤ 36 %"),
    ];

    // GMI — оценка HbA1c по среднему уровню. На суточном окне она
    // статистически бессмысленна, поэтому только для недели и месяца.
    if (activeRange !== "day") {
        cards.push(statCard("GMI", `${stats.gmi.toLocaleString("ru-RU")} %`, "расчётный HbA1c"));
    }

    cards.push(statCard("Измерений", stats.count.toLocaleString("ru-RU"), RANGE_LABELS[activeRange]));

    els.stats.append(...cards);
    els.stats.hidden = false;
}

/* ── График ────────────────────────────────────────────────────────── */

function niceScale(points) {
    const values = points.map((p) => toMmol(p[1]));
    // Нижняя и верхняя границы целевого диапазона всегда в кадре: без этого
    // ровный график «висел бы» без опоры, и зона не читалась бы.
    const min = Math.min(3, ...values);
    const max = Math.max(11, ...values);

    return { min: Math.floor(min) - 0.5, max: Math.ceil(max) + 0.5 };
}

function drawChart() {
    const series = snapshot.series[activeRange];
    const points = series.points;

    els.chartEmpty.hidden = points.length > 0;

    const canvas = els.canvas;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;

    canvas.width = width * ratio;
    canvas.height = height * ratio;

    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    if (!points.length) return;

    const padding = { top: 12, right: 12, bottom: 24, left: 38 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;

    const scale = niceScale(points);
    const now = snapshot.generated_at;
    const spanSeconds = { day: 86400, week: 7 * 86400, month: 30 * 86400 }[activeRange];
    const startTime = now - spanSeconds;

    const x = (t) => padding.left + ((t - startTime) / spanSeconds) * plotWidth;
    const y = (mmol) =>
        padding.top + plotHeight - ((mmol - scale.min) / (scale.max - scale.min)) * plotHeight;

    const styles = getComputedStyle(document.documentElement);
    const muted = styles.getPropertyValue("--muted").trim();
    const accent = styles.getPropertyValue("--accent").trim();
    const inRange = styles.getPropertyValue("--in-range").trim();

    // Целевой диапазон — подложка, а не линии: так видно «сколько времени
    // график провёл внутри», не считая пересечения глазами.
    const targetTop = y(toMmol(snapshot.target.high));
    const targetBottom = y(toMmol(snapshot.target.low));
    ctx.fillStyle = inRange;
    ctx.globalAlpha = 0.07;
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

        ctx.globalAlpha = 0.7;
        ctx.fillText(String(value), padding.left - 8, lineY);
    }
    ctx.globalAlpha = 1;

    // Подписи по времени. Крайние выравниваются внутрь, иначе первая и
    // последняя наполовину уходят за край холста.
    ctx.textBaseline = "top";
    const ticks = activeRange === "day" ? 4 : 5;
    for (let i = 0; i <= ticks; i += 1) {
        const t = startTime + (spanSeconds / ticks) * i;
        const date = new Date(t * 1000);
        const label =
            activeRange === "day"
                ? date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
                : date.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });

        ctx.textAlign = i === 0 ? "left" : i === ticks ? "right" : "center";
        ctx.globalAlpha = 0.7;
        ctx.fillText(label, x(t), height - padding.bottom + 8);
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
}

/* ── Загрузка и события ────────────────────────────────────────────── */

function render() {
    renderNow();

    if (!snapshot.latest) return;

    els.ranges.hidden = false;
    els.chart.hidden = false;
    drawChart();
    renderStats();

    const generated = new Date(snapshot.generated_at * 1000);
    els.footUpdated.textContent = `Обновлено ${formatAgo(Date.now() - generated.getTime())}`;
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
        item.classList.toggle("is-active", item === button);
    }
    drawChart();
    renderStats();
});

window.addEventListener("resize", () => {
    if (snapshot && snapshot.latest) drawChart();
});

load();
setInterval(load, RELOAD_INTERVAL_MS);
