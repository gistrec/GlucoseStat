/* Дашборд читает единственный статический data.json — ни бэкенда, ни запросов
   к Abbott со стороны браузера. */

import { state, MEALS_PAGE } from "./js/state.js";
import { els, readColor, readNumber } from "./js/dom.js";
import { THEMES, THEME_BG, initialTheme, rememberTheme, storedTheme } from "./js/theme.js";
import { renderNow, renderStats, statCard } from "./js/now-stats.js";
import { renderNights } from "./js/nights.js";
import { drawChart, legendItem, tipRow, placeTip, hoverAt, clearHover } from "./js/chart.js";
import {
    TIMEZONE,
    minutesOfDay,
    toMmol,
    formatMmol,
    formatAgo,
    formatDateTime,
    formatCellDateTime,
    sinceLabel,
    formatAmount,
    formatDelta,
    formatDose,
} from "./js/format.js";

const RELOAD_INTERVAL_MS = 60 * 1000;

// Сборщик опрашивает LibreLinkUp раз в 5 минут, так что три пропуска подряд —
// это уже не задержка, а молчание, о котором нужно сказать вслух.
const COLLECTOR_SILENT_AFTER_MS = 15 * 60 * 1000;

/* ── Тема ──────────────────────────────────────────────────────────── */

function applyTheme(id) {
    const current = THEMES.find((item) => item.id === id) || THEMES[1];
    state.theme = current.id;
    document.documentElement.dataset.theme = state.theme;

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

    els.themeColor.setAttribute("content", THEME_BG[state.theme]);

    // Разметка перекрашивается сама, холсты — нет: их цвета прочитаны из
    // CSS-переменных один раз, при отрисовке.
    if (state.snapshot && state.snapshot.latest) {
        drawChart();
        renderReview();
        // Холсты ночей тоже перерисовываются: их цвета прочитаны из CSS один
        // раз, а ширина ячейки меняется вместе с шириной окна.
        renderNights();
    }
}

/* ── Разбор приёмов пищи ───────────────────────────────────────────── */

/* Ряды оверлея. Отдельные кривые намеренно приглушены: они дают форму и
   разброс, а читается по ним медиана. */
const OVERLAY_SERIES = {
    single: {
        token: "--muted",
        fallback: "#8a90a6",
        label: "Отдельные приёмы",
        unit: "ммоль/л",
        line: true,
    },
    median: {
        token: "--accent",
        fallback: "#7eb8f7",
        label: "Медиана",
        unit: "ммоль/л",
        line: true,
    },
    // Цвет еды, а не акцента: акцентом нарисована медиана, и вторая синяя
    // линия читалась бы как ещё одна сводка, а не как один приём.
    picked: {
        token: "--meal",
        fallback: "#bd8a30",
        label: "Выбранный приём",
        unit: "ммоль/л",
        line: true,
        thick: true,
    },
};

function pickedMeal() {
    return state.previewMeal ?? state.pinnedMeal;
}

const OVERLAY_HEIGHT = 240;

// Сколько кривых должно накрыть отметку времени, чтобы медиана в ней что-то
// значила. На двух это просто среднее двух обедов, выданное за общую картину.
const MEDIAN_MIN_CURVES = 3;

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

    // Пустой холст не оставляет за собой раскладку: наведение на него обязано
    // ничего не найти, а не считать по прошлому набору кривых.
    state.overlayGeometry = null;

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

    state.overlayGeometry = {
        x,
        y,
        span,
        left: padding.left,
        right: width - padding.right,
        plotTop: padding.top,
        plotBottom: height - padding.bottom,
        // Нормализованные кривые, а не сырые приёмы: подсказка называет те же
        // отклонения, что нарисованы, и считать их второй раз незачем.
        curves,
        median,
        meals: drawn,
        picked,
    };

    // Последним, поверх всего: под указателем важнее прочитать значение, чем
    // сохранить в целости подпись ориентира.
    drawOverlayCursor(ctx);
}

// Допуск поиска значения под указателем. Кривые идут пятиминутным шагом, так
// что десять минут переживают один пропуск сенсора, но не выдают за значение
// под курсором точку из другого куска кривой.
const OVERLAY_NEAREST_MIN = 10;

function overlayValueAt(curve, minute) {
    let best = null;
    for (const [offset, mgdl] of curve) {
        const distance = Math.abs(offset - minute);
        if (distance <= OVERLAY_NEAREST_MIN && (best === null || distance < best[0])) {
            best = [distance, mgdl];
        }
    }
    return best === null ? null : best[1];
}

/* Засечка на кривой: кольцо цветом панели вокруг точки — тот же приём, что у
   графика выше, и по той же причине: без кольца точка теряется там, где
   кривые сходятся. */
function overlayDot(ctx, minute, mgdl, color) {
    ctx.beginPath();
    ctx.arc(state.overlayGeometry.x(minute), state.overlayGeometry.y(toMmol(mgdl)), 4, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = readColor("--panel", "#0d0d14");
    ctx.stroke();
}

function drawOverlayCursor(ctx) {
    if (state.overlayHoverMin === null || !state.overlayGeometry) return;

    const px = Math.round(state.overlayGeometry.x(state.overlayHoverMin)) + 0.5;
    if (px < state.overlayGeometry.left || px > state.overlayGeometry.right) return;

    ctx.strokeStyle = readColor("--muted", "#8a90a6");
    ctx.globalAlpha = 0.45;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px, state.overlayGeometry.plotTop);
    ctx.lineTo(px, state.overlayGeometry.plotBottom);
    ctx.stroke();
    ctx.globalAlpha = 1;

    // Засечки — только у двух названных рядов. У отдельных кривых их нет
    // намеренно: десяток колец на одной вертикали не сообщает ничего, чего не
    // сказала бы строка разброса в подсказке.
    const median = overlayValueAt(state.overlayGeometry.median, state.overlayHoverMin);
    if (median !== null) {
        overlayDot(ctx, state.overlayHoverMin, median, readColor("--accent", "#7eb8f7"));
    }

    if (state.overlayGeometry.picked >= 0) {
        const own = overlayValueAt(
            state.overlayGeometry.curves[state.overlayGeometry.picked],
            state.overlayHoverMin
        );
        if (own !== null) {
            overlayDot(ctx, state.overlayHoverMin, own, readColor("--meal", "#bd8a30"));
        }
    }
}

/* Сколько прошло от еды — словами. У нуля «в момент еды»: «через 0 мин» — та
   же мысль, высказанная арифметикой. */
function offsetLabel(minutes) {
    if (minutes < 1) return "в момент еды";

    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    if (!hours) return `через ${rest} мин`;
    return rest ? `через ${hours} ч ${rest} мин` : `через ${hours} ч`;
}

/* Подсказка оверлея. Порядок строк зафиксирован, как у графика выше: сводка →
   разброс → выбранное. Отдельные кривые названы краями, а не перечислены: их
   числа читаются в таблице, а на холсте они нарисованы ради широты. */
function showOverlayTip(clientX) {
    if (state.overlayHoverMin === null || !state.overlayGeometry) return hideOverlayTip();

    const rows = [];
    const unit = OVERLAY_SERIES.median.unit;

    const median = overlayValueAt(state.overlayGeometry.median, state.overlayHoverMin);
    if (median !== null) {
        rows.push(
            tipRow(OVERLAY_SERIES.median, `Медиана ${formatDelta(median)} ${unit}`)
        );
    }

    const values = state.overlayGeometry.curves
        .map((curve) => overlayValueAt(curve, state.overlayHoverMin))
        .filter((value) => value !== null);

    if (values.length) {
        rows.push(
            tipRow(
                OVERLAY_SERIES.single,
                `Разброс ${formatDelta(Math.min(...values))} … ${formatDelta(
                    Math.max(...values)
                )} ${unit}`
            )
        );
    }

    if (state.overlayGeometry.picked >= 0) {
        const own = overlayValueAt(
            state.overlayGeometry.curves[state.overlayGeometry.picked],
            state.overlayHoverMin
        );
        if (own !== null) {
            rows.push(
                tipRow(OVERLAY_SERIES.picked, `Выбранный ${formatDelta(own)} ${unit}`)
            );
        }
    }

    // Ни одной кривой под указателем — нечего и показывать: справа от самого
    // длинного окна холст пуст, и подсказка с одним временем обещала бы, что
    // там что-то есть.
    if (!rows.length) return hideOverlayTip();

    const time = document.createElement("p");
    time.className = "tip__time";
    time.textContent = offsetLabel(state.overlayHoverMin);

    // Счёт кривых — не украшение: он объясняет, почему медианы в этой минуте
    // может не быть вовсе. Молча пропавшая строка читается как «подъёма
    // здесь нет», а не как «считать ещё не по чему».
    const note = document.createElement("p");
    note.className = "tip__note";
    note.textContent =
        median === null && values.length < MEDIAN_MIN_CURVES
            ? `Кривых здесь ${values.length} — медиана считается от ${MEDIAN_MIN_CURVES}`
            : `Кривых здесь ${values.length}`;

    els.overlayTip.replaceChildren(time, ...rows, note);
    placeTip(
        els.overlayTip,
        els.reviewPanel,
        els.overlay,
        state.overlayGeometry.plotTop,
        clientX
    );
}

function hideOverlayTip() {
    els.overlayTip.hidden = true;
}

/* Четыре состояния, и приглушены из них те два, где мерить было нечего.
   Подъём выше ориентира — исход, а не отсутствие исхода, и выглядеть как
   «окно не закрылось» он не должен. Его жёлтый — цвет жёлтой зоны графика:
   «выше нормы, но не критично» на всей странице значит одно и то же. Стрелка
   при этом остаётся: она читается и на монохроме, и при дальтонизме. */
function outcome(meal, targets) {
    if (meal.hypo) return ["flag--hypo", "⚠ гипогликемия"];
    if (!meal.complete) return ["flag--skip", "· окно не закрылось"];
    if (meal.cut) return ["flag--skip", "· прервано следующей едой"];
    // Раньше подъёма: приём, которым закрыли гипогликемию, почти всегда выходит
    // за ориентир — считать от опоры в 3,4 иначе и нельзя, — и «↑ подъём выше
    // ориентира» сказало бы о нём ровно то, что здесь ни при чём.
    if (meal.from_hypo) return ["flag--ok", "✓ гипогликемия купирована"];
    if (meal.rise <= targets.rise) return ["flag--ok", "✓ в ориентире"];
    return ["flag--over", "↑ подъём выше ориентира"];
}

function textCell(text) {
    const cell = document.createElement("td");
    cell.textContent = text;
    return cell;
}

/* Приём, записанный в несколько заходов, разбирается как одна еда, а на
   дорожке главного графика его записи стоят порознь. Знак суммы объясняет
   расхождение и под наведением перечисляет заходы. */
/* Уверенность в числе — два разных факта, и показывать нужно оба. Чем число
   получено, шкала не заменяет: «взвешено» и «по фото, прогоны сошлись» одинаково
   надёжны сейчас, но исправлять по ним разное — оценку стоит перевесить, весы
   уже всё сказали. А одним источником не обойтись: взвешенную порцию доедают
   наполовину, и «взвешено» без шкалы обещало бы точность, которой нет. Отсюда
   пара знаков — источник и три деления, — а не один общий значок.

   Точки приходят из данных отдельным полем и не выводятся из источника:
   уверенность у каждой записи своя, её называет человек в боте. Где он не
   ответил, число деления считает analysis.trust_level — здесь только показ. */
const TRUST_ORIGIN = {
    weighed: { mark: "⚖︎", spoken: "взвешено" },
    spoken: { mark: "✎", spoken: "со слов" },
    photo: { mark: "▣", spoken: "по фото" },
};

/* Как читаются сами деления. Словами, а не «2 из 3»: под наведением и вслух
   должно звучать то же, что человек нажимал в боте. */
const TRUST_CONFIDENCE = {
    3: "точно",
    2: "примерно",
    1: "наугад",
};

const TRUST_DOTS = 3;

/* Колонка «Запись»: чем число углеводов получено и насколько ему верить.
   Пара знаков стояла в ячейке углеводов перед числом — и делала колонку
   чисел рваной по ширине; своя колонка возвращает числам ровный край.
   Σ составного приёма при этом остаётся у самого числа: он говорит о числе,
   а не о происхождении записи. */
function recordCell(meal) {
    const cell = document.createElement("td");

    const trust = meal.trust;
    const origin = trust ? TRUST_ORIGIN[trust.origin] : null;
    const confidence = trust ? TRUST_CONFIDENCE[trust.dots] : null;
    if (!origin && !confidence) return cell;

    // Обе половины произносятся одной фразой: «со слов, наугад». Пустая
    // половина из неё выпадает — способ без ответа человека и наоборот.
    const phrase = [origin?.spoken, confidence].filter(Boolean).join(", ");

    const mark = document.createElement("span");
    mark.className = `trust trust--${trust.origin || "unknown"}`;
    mark.title = phrase;
    mark.setAttribute("aria-hidden", "true");

    // Две части, а не одна строка: источник и шкала набраны разным кеглем —
    // ⚖︎ и ✎ рисуются заметно мельче точек при одном размере.
    if (origin) {
        const source = document.createElement("span");
        source.className = "trust__source";
        source.textContent = origin.mark;
        mark.append(source);
    }

    if (confidence) {
        const dots = document.createElement("span");
        dots.className = "trust__dots";
        dots.textContent = "●".repeat(trust.dots) + "○".repeat(TRUST_DOTS - trust.dots);
        mark.append(dots);
    }

    const said = document.createElement("span");
    said.className = "visually-hidden";
    said.textContent = phrase;

    cell.append(mark, said);
    return cell;
}

function carbsCell(meal) {
    const cell = textCell(`${formatAmount(meal.carbs)} г`);
    if (!meal.parts) return cell;

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
    // Стоит он перед числом: колонка выровнена по правому краю, и хвостовая
    // пометка сдвигала бы числа друг относительно друга.
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
    return cell;
}

function renderMeals(analysis) {
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const title of ["Когда", "Запись", "Углеводы", "Инсулин", "Подъём", "Пик через", "Возврат", "Исход"]) {
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
            recordCell(meal),
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
    state.hoveredMeal = t;
    // Ушёл указатель — остаётся то, что держит фокус, и наоборот.
    state.previewMeal = t ?? state.focusedMeal;
    refreshPicked();
}

function setFocused(t) {
    state.focusedMeal = t;
    state.previewMeal = t ?? state.hoveredMeal;
    refreshPicked();
}

function togglePinned(t) {
    state.pinnedMeal = state.pinnedMeal === t ? null : t;
    refreshPicked();
}

/* Отметить выбранную строку, не трогая холст: наведение при закреплённой
   кривой меняет только таблицу, и перерисовывать оверлей незачем. */
function markPicked() {
    const picked = pickedMeal();
    for (const row of els.meals.querySelectorAll("tbody tr")) {
        const t = Number(row.dataset.meal);
        row.classList.toggle("is-picked", t === picked);
        row.classList.toggle("is-pinned", t === state.pinnedMeal);

        const pick = row.querySelector(".meals__pick");
        if (pick) pick.setAttribute("aria-pressed", String(t === state.pinnedMeal));
    }
}

function refreshPicked() {
    markPicked();
    redrawOverlay();
}

/* Перерисовать один холст разбора — тем же набором, что показан сейчас, а не
   всем разбором: полный уговор — в renderReview. Здесь стоял state.snapshot.analysis
   целиком, и первое же наведение подкладывало на холст кривые приёмов, которых
   нет в списке под ним, вместе с медианой, посчитанной по другому набору.

   renderReview() на эту роль не годится: наведение — и на строку, и на сам
   холст — меняет только картинку, а он пересобрал бы таблицу под указателем. */
function redrawOverlay() {
    const visible = visibleMeals();
    if (visible) drawOverlay(visible);
}

/* Видимая часть разбора: последние ``state.mealsShown`` приёмов. Считается в одном
   месте — иначе холст и таблица однажды разойдутся в том, что показывают. */
function visibleMeals() {
    const analysis = state.snapshot && state.snapshot.analysis;
    if (!analysis || !analysis.meals.length) return null;

    const shown = analysis.meals.slice(-Math.min(state.mealsShown, analysis.meals.length));
    return { ...analysis, meals: shown };
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
            `ниже ${formatMmol(targets.hypo)} ммоль/л после пика, считая незакрытые и прерванные окна`
        )
    );

    // Отдельной карточкой, а не строчкой в предыдущей: это не разновидность
    // исхода, а причина, по которой человек сел есть. Карточки нет, когда таких
    // приёмов не было, — ноль здесь сообщал бы о благополучии, которого никто
    // не измерял.
    if (summary.from_hypo) {
        cards.push(
            statCard(
                "Съедено на гипогликемии",
                `${summary.from_hypo} из ${summary.total ?? summary.count}`,
                "сахар был ниже порога в момент еды и поднимался после неё"
            )
        );
    }

    if (summary.count) {
        cards.push(
            statCard(
                "Уложились в ориентир",
                `${summary.good} из ${summary.count}`,
                "подъём в пределах ориентира и без гипогликемии следом"
            )
        );
    }

    els.reviewStats.append(...cards);
    els.reviewStats.hidden = false;
}

/* ── Углеводный коэффициент ────────────────────────────────────────── */

/* Границы — местные часы еды, а не тройка «завтрак/обед/ужин» по названию:
   имя приёму даёт человек, а чувствительность к инсулину ходит за солнцем.
   Ночь отдельно и последней: приёмы в ней редки, и чаще всего это не ужин,
   а купирование гипогликемии. */
const DAYPARTS = [
    { label: "Утро", from: 6 * 60, to: 12 * 60 },
    { label: "День", from: 12 * 60, to: 18 * 60 },
    { label: "Вечер", from: 18 * 60, to: 24 * 60 },
    { label: "Ночь", from: 0, to: 6 * 60 },
];

/* Второй разрез тех же приёмов — по размеру порции. Границы фиксированные и
   круглые, а не перцентили выборки: строка «до 40 г» обязана значить одно и то
   же сегодня и через месяц, иначе её не с чем сравнить, а на живом журнале
   подпись менялась бы при каждом пересчёте.

   Сорок и семьдесят выбраны по этому журналу, а не по привычке: приёмы в нём
   лежат между 14 и 100 граммами с медианой 50, и эта пара делит их примерно на
   терцили (p33 = 38, p75 = 66). Мельче делить нечего — перекусы до SNACK_CARBS
   в разбор не попадают вовсе, и приёмов легче двадцати граммов в нём считаные
   единицы; крупнее — тоже: за сотню выходит от силы один. */
const PORTIONS = [
    { label: "До 40 г", from: 0, to: 40 },
    { label: "40–70 г", from: 40, to: 70 },
    { label: "70 г и больше", from: 70, to: Infinity },
];

// Тот же порог, что MEDIAN_MIN_CURVES у оверлея, и по той же причине: медиана
// по двум обедам — это среднее двух обедов, выданное за общую картину.
const RATIO_MIN_MEALS = 3;

function medianOf(values) {
    const sorted = [...values].sort((a, b) => a - b);
    const middle = sorted.length >> 1;
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

/* Приём, у которого отношение углеводов к дозе вообще о чём-то говорит.
   Четыре условия, и каждое отсекает свой способ соврать.

   Опора внутри целевого диапазона: болюс привязывается к еде целиком (см.
   ``_doses`` в analysis.py), и часть, введённая на снижение высокого сахара,
   делится в коэффициенте на те же углеводы — выходит меньше граммов на
   единицу, чем нужно было еде. Вход с низкого — обратная ошибка: дозу там
   урезают руками, и коэффициент задирается.

   Нижняя граница почти всегда уже сработала как from_hypo — порог
   гипогликемии и низ диапазона это один TARGET_LOW_MGDL, — но не всегда:
   опора берётся ближайшим показанием, в том числе до начала приёма, а
   from_hypo смотрит только внутрь окна.

   Гипогликемия после пика — это «дозы было слишком много», и такой приём
   называет свой коэффициент ровно тем, чем он не является. Незакрытое и
   прерванное окно отброшены по той же причине, что в сводке: чем кончился
   подъём, никто не видел. */
function ratioReady(meal) {
    return (
        meal.complete &&
        !meal.cut &&
        !meal.hypo &&
        !meal.from_hypo &&
        meal.baseline >= state.snapshot.target.low &&
        meal.baseline <= state.snapshot.target.high
    );
}

/* Сколько граммов углеводов пришлось на единицу короткого — по записям, а не
   по правилам. Считается по окну разбора целиком, а не по раскрытой части
   таблицы. Подъём рядом — чтобы коэффициент читался с исходом: одинаковые
   «10 г/ед» с подъёмом в ориентире и с подъёмом вдвое выше — разные истории.
   Вывод из них — не дело страницы. */
function renderRatio(analysis) {
    const dosed = analysis.meals.filter(
        (meal) => meal.dose && meal.dose.units > 0 && meal.carbs > 0
    );
    const clean = dosed.filter(ratioReady);

    const byDaypart = ratioRows(clean, DAYPARTS, (meal) => minutesOfDay(meal.t));
    const byPortion = ratioRows(clean, PORTIONS, (meal) => meal.carbs);

    if (!byDaypart.length && !byPortion.length) {
        els.ratio.hidden = true;
        return;
    }

    // Отброшенные названы числом и причиной: коэффициент, посчитанный по
    // половине приёмов с болюсом, обязан сказать, по какой именно половине, —
    // иначе «12 приёмов» читается как «все, что были».
    const dropped = dosed.length - clean.length;
    els.ratioNote.textContent =
        "Сколько граммов углеводов пришлось на единицу короткого — медиана " +
        `по ${clean.length} приёмам с болюсом` +
        (dropped
            ? ` из ${dosed.length}. Остальные отброшены: опора вне целевого ` +
              "диапазона, гипогликемия или незакрытое окно"
            : "") +
        ". Не рекомендация дозы.";

    fillRatioTable(els.ratioTable, "Время суток", byDaypart);
    els.ratioTable.parentElement.hidden = !byDaypart.length;

    fillRatioTable(els.ratioSizeTable, "Размер порции", byPortion);
    els.ratioSizeWrap.hidden = !byPortion.length;

    els.ratio.hidden = false;
}

/* Один и тот же счёт для обоих разрезов: группы задаются парой границ и тем,
   какую величину у приёма мерить — час еды или граммы. Разводить их по двум
   похожим функциям значило бы однажды поправить кворум в одной из них. */
function ratioRows(dosed, groups, valueOf) {
    const rows = [];
    for (const group of groups) {
        const meals = dosed.filter((meal) => {
            const value = valueOf(meal);
            return group.from <= value && value < group.to;
        });
        if (meals.length < RATIO_MIN_MEALS) continue;

        // Оба числа — по одной и той же группе: выборку уже просеял
        // ratioReady, и отдельного отбора для подъёма здесь больше нет.
        // Раньше он был, и «Приёмов» считало всю группу, а «Подъём» — её
        // чистую часть: строка про пять приёмов показывала медиану одного.
        rows.push({
            label: group.label,
            count: meals.length,
            ratio: medianOf(meals.map((meal) => meal.carbs / meal.dose.units)),
            rise: medianOf(meals.map((meal) => meal.rise)),
        });
    }
    return rows;
}

function fillRatioTable(table, firstColumn, rows) {
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const title of [firstColumn, "Приёмов", "Г на 1 ед", "Подъём, медиана"]) {
        const cell = document.createElement("th");
        cell.scope = "col";
        cell.textContent = title;
        headRow.append(cell);
    }
    head.append(headRow);

    const body = document.createElement("tbody");
    for (const row of rows) {
        const tr = document.createElement("tr");
        tr.append(
            textCell(row.label),
            textCell(String(row.count)),
            textCell(formatAmount(row.ratio)),
            textCell(formatDelta(row.rise))
        );
        body.append(tr);
    }

    const caption = table.querySelector("caption");
    table.replaceChildren(...(caption ? [caption] : []), head, body);
}

function renderReview() {
    const analysis = state.snapshot.analysis;

    // Ни одного разобранного приёма пищи — секции просто нет. Пустая таблица с
    // прочерками сообщает не больше, чем её отсутствие, а места занимает экран.
    if (!analysis || !analysis.meals.length) {
        els.review.hidden = true;
        return;
    }

    /* Оверлей и таблица показывают один и тот же набор — иначе на графике
       лежали бы кривые приёмов, которых в списке под ним нет, и подпись «после
       N приёмов» считала бы одно, а глаз видел другое. Сводка выше остаётся по
       всему окну: она про две недели, а не про то, что сейчас раскрыто, и её
       карточка так и подписана. */
    const visible = visibleMeals();
    const shown = visible.meals;

    const reviewSince = sinceLabel(new Date(shown[0].t * 1000));
    els.reviewNote.textContent =
        `На сколько глюкоза отклонялась от уровня в момент еды за ` +
        `${analysis.window_min / 60} ч после приёма, ${reviewSince}. ` +
        "Описание исхода, не оценка дозы: активность, болезнь и остаток " +
        "прошлой дозы сюда не входят.";

    // Закреплённый приём мог уехать из снимка: окно разбора движется, и раз в
    // минуту страница перечитывает его заново. Проверяется не наличие в
    // таблице, а рисуемость: строка может остаться, а кривая — сократиться до
    // точки, и подсветка повисла бы ни на чём.
    // Свёрнутый список тоже снимает подсветку: приём, ушедший под кнопку, на
    // оверлее больше не рисуется, и закрепление висело бы ни на чём — ровно как
    // у приёма, уехавшего из снимка.
    const drawable = shown.some(
        (meal) => meal.t === state.pinnedMeal && meal.curve.length > 1
    );
    if (state.pinnedMeal !== null && !drawable) state.pinnedMeal = null;

    // Раскрыть до отрисовки: у скрытой секции холст имеет нулевую ширину, и
    // рисовать в него — значит рисовать в ничто.
    els.review.hidden = false;

    renderReviewStats(analysis);
    renderRatio(analysis);
    drawOverlay(visible);
    renderMeals(visible);

    const hidden = analysis.meals.length - shown.length;
    els.mealsMore.hidden = hidden === 0;
    if (hidden) {
        els.mealsMore.textContent = `Показать ещё ${Math.min(MEALS_PAGE, hidden)}`;
    }
}

/* ── Загрузка и события ────────────────────────────────────────────── */

/* Состояние сборщика, а не возраст файла. Снимок перезаписывается каждые
   пять минут независимо от того, отвечает ли Abbott, поэтому «обновлено
   только что» само по себе ничего не говорит о свежести данных. */
function renderCollectorState() {
    const lastSuccess = (state.snapshot.collector || {}).last_success;

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

/* Однократно: расхождение зон — ошибка конфигурации, о которой надо сказать
   вслух, а не спамить на каждую перерисовку раз в минуту. Дневной вид при
   этом продолжает рисовать — данные нарезаны честно, просто в другой зоне,
   и подсказка называет её по имени. */

function render() {
    if (!state.timezoneWarned && state.snapshot.timezone && state.snapshot.timezone !== TIMEZONE) {
        state.timezoneWarned = true;
        console.warn(
            `Снимок нарезан в зоне ${state.snapshot.timezone}, страница подписывает время в ${TIMEZONE} — поменяйте DISPLAY_TZ и TIMEZONE вместе.`
        );
    }

    // Баннер живёт до первой удачной отрисовки: страница перечитывает снимок
    // каждую минуту, и один моргнувший fetch не должен навсегда повесить
    // «не удалось загрузить» над живыми данными. renderNow() вернёт баннер,
    // если показывать по-прежнему нечего.
    els.empty.hidden = true;

    renderNow();
    // До проверки на пустые данные: когда замеров нет вовсе, знать, жив ли
    // сборщик, тем более важно.
    renderCollectorState();

    if (!state.snapshot.latest) return;

    els.ranges.hidden = false;
    els.chart.hidden = false;
    drawChart();
    renderStats();
    // Ночи не зависят от выбранного окна графика: у них своё, названное в их
    // же подписи, — тот же уговор, что у разбора приёмов ниже.
    renderNights();
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
        state.snapshot = await response.json();
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

    state.activeRange = button.dataset.range;
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
    if (state.snapshot && state.snapshot.latest) {
        drawChart();
        renderReview();
        // Холсты ночей тоже перерисовываются: их цвета прочитаны из CSS один
        // раз, а ширина ячейки меняется вместе с шириной окна.
        renderNights();
    }
});

/* Раскрытие списка. Пока кнопка на месте, фокус остаётся на ней — жать её
   подряд удобнее, чем каждый раз возвращаться табом. На последнем нажатии она
   исчезает, и фокус улетел бы на body посреди таблицы; тогда он передаётся
   первой из добавленных строк.

   Но только при нажатии с клавиатуры (detail === 0 у Enter и пробела, у мыши
   там счётчик кликов): фокус на строке подсвечивает её кривую, и после щелчка
   мышью на графике сама собой выделялась бы кривая приёма, которого никто не
   выбирал. */
els.mealsMore.addEventListener("click", (event) => {
    const before = els.meals.querySelectorAll("tbody tr").length;
    state.mealsShown += MEALS_PAGE;
    renderReview();

    if (!els.mealsMore.hidden || event.detail !== 0) return;

    const added = [...els.meals.querySelectorAll("tbody tr")].slice(before);
    const pick = added.map((row) => row.querySelector(".meals__pick")).find(Boolean);
    if (pick) pick.focus({ preventScroll: true });
});

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

/* Наведение на оверлей разбора — тот же набор событий и тот же счёт, только
   ось считает минуты от еды, а не время суток. Закрепление кривой остаётся за
   таблицей: холст тут отвечает на «сколько было в эту минуту», а не на «какой
   это приём» — по десятку сошедшихся кривых ближайшую не выбрать. */
function overlayHoverAt(event) {
    if (!state.overlayGeometry) return;

    const rect = els.overlay.getBoundingClientRect();
    const px = event.clientX - rect.left;
    if (px < state.overlayGeometry.left || px > state.overlayGeometry.right) {
        clearOverlayHover();
        return;
    }

    const share = (px - state.overlayGeometry.left) / (state.overlayGeometry.right - state.overlayGeometry.left);
    state.overlayHoverMin = Math.round(share * state.overlayGeometry.span);
    redrawOverlay();
    showOverlayTip(event.clientX);
}

function clearOverlayHover() {
    if (state.overlayHoverMin === null) return;
    state.overlayHoverMin = null;
    hideOverlayTip();
    redrawOverlay();
}

els.overlay.addEventListener("pointerdown", overlayHoverAt);
els.overlay.addEventListener("pointermove", overlayHoverAt);
els.overlay.addEventListener("pointerleave", clearOverlayHover);
els.overlay.addEventListener("pointerup", clearOverlayHover);
els.overlay.addEventListener("pointercancel", clearOverlayHover);

els.theme.addEventListener("click", () => {
    const next = THEMES[(THEMES.findIndex((item) => item.id === state.theme) + 1) % THEMES.length];
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
