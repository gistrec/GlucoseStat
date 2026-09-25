/* Дашборд читает единственный статический data.json — ни бэкенда, ни запросов
   к Abbott со стороны браузера. */

import { state, MEALS_PAGE } from "./js/state.js";
import { els, readColor, readNumber } from "./js/dom.js";
import { THEMES, THEME_BG, initialTheme, rememberTheme, storedTheme } from "./js/theme.js";
import { SERIES, targetMid, readingColor, seriesKey } from "./js/series.js";
import { RANGE_LABELS, SPAN_SECONDS, HOURLY_RANGES } from "./js/ranges.js";
import { renderNow, renderStats, statCard, STALE_AFTER_MS } from "./js/now-stats.js";
import { renderNights } from "./js/nights.js";
import {
    TIMEZONE,
    TIMEZONE_LABEL,
    displayTimezone,
    minutesOfDay,
    toMmol,
    formatMmol,
    percent,
    formatAgo,
    formatSpan,
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

/* Горизонт пунктирного прогноза. Линейное продолжение по 15-минутной скорости
   честно на полчаса — дальше еда и инсулин ломают прямую раньше, чем она
   успевает сбыться. Скорость — та же latest.rate, по которой рисуется стрелка
   тренда: две подписи одного числа не вправе разойтись. */
const FORECAST_MINUTES = 30;

/* Шкала сенсора: значений за её пределами Libre не отдаёт, и хвост, ушедший
   ниже 40 мг/дл, обещал бы замер, которого не может быть. На границе шкалы
   хвост обрезается, а не ложится горизонталью. */
const SENSOR_MIN_MGDL = 40;
const SENSOR_MAX_MGDL = 500;

// Высота холста без дорожек событий — та же, что была до их появления.
const PLOT_HEIGHT = 340;
const LANE_HEIGHT = 34;
const LANE_GAP = 8;
const COLUMN_WIDTH = 7;
// Кружок на макушке метки смены ручки. Чуть шире половины столбика: метка
// стоит между столбиками и не должна теряться рядом с ними.
const MARK_RADIUS = 3.5;

// Просвет между соседними подписями на дорожке. Впритык поставленные числа
// читаются как одно: «45» и «60» в паре пикселей друг от друга — это «4560».
const LABEL_GAP = 4;

/* Дневные коробки месячного вида. Ширина одна на все полные дни: коробка —
   карточка суток, а не их длительность, и 25-часовые сутки перехода на зимнее
   время не должны выглядеть толще соседей. Обрезанные окном крайние дни уже —
   по прожитой доле, см. dailyBoxWidth. Заливка полупрозрачная, чтобы засечка
   медианы читалась и поверх коробки собственного цвета. */
const BOX_SHARE = 0.6;
const BOX_MIN_WIDTH = 3;
const BOX_MAX_WIDTH = 22;
const BOX_ALPHA = 0.45;

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

/* ── График ────────────────────────────────────────────────────────── */

/* Что «видно» на графике, словами: сам холст для скринридера пуст, а
   пересказывать сотни точек бессмысленно — нужен итог. Числа окна — из
   stats, то есть по сырым замерам, а не по нарисованной сводке. */
function chartDescription(daily, hasData, tail, gaps, artifacts) {
    const period = RANGE_LABELS[state.activeRange];
    if (!hasData) return `График глюкозы за ${period}: данных нет`;

    const stats = state.snapshot.stats[state.activeRange];
    if (!stats) return `График глюкозы за ${period}`;

    const summary =
        `${stats.count} измерений, ` +
        `среднее ${formatMmol(stats.avg)} ммоль/л, ` +
        `от ${formatMmol(stats.min)} до ${formatMmol(stats.max)}, ` +
        `в целевом диапазоне ${stats.tir.toLocaleString("ru-RU")} процентов времени`;

    if (daily) {
        return (
            `График глюкозы за ${period} по дням: коробка каждого дня — ` +
            `разброс от 25-го до 75-го перцентиля, засечка — медиана. ${summary}`
        );
    }

    // Пунктирный хвост назван словами: из пересказа сотни точек он выпал бы,
    // а обещать прогноз, которого на холсте нет, подпись не вправе — отсюда
    // фраза только при нарисованном хвосте.
    const forecast = tail
        ? `. Пунктиром — прогноз на ${Math.round((tail.to.t - tail.from.t) / 60)} минут вперёд по текущей скорости`
        : "";

    /* Молчание сенсора — тем же правилом: сказано ровно то, что нарисовано.
       Числа читаются вслух, поэтому промежутки сложены в один итог: список из
       пяти длительностей подряд на слух не удержать, а вопрос всё равно один
       — сколько времени окна страница не видела. */
    const silence = (gaps || []).length
        ? `. Без сигнала ${formatSpan(gaps.reduce((total, gap) => total + (gap.to - gap.from), 0))}` +
          (gaps.length > 1 ? ` в ${gaps.length} промежутках` : "")
        : "";

    // Кольца — тем же правилом: количеством, не перечислением. Число после
    // двоеточия, а не перед существительным, — русское склонение числительных
    // (1 скачок / 2 скачка / 5 скачков) тут не нужно вовсе.
    const noise = (artifacts || []).length
        ? `. Возможных скачков шума сенсора: ${artifacts.length}`
        : "";

    return `График глюкозы за ${period}: ${summary}${forecast}${silence}${noise}`;
}

/* Принимает готовые значения в ммоль/л, а не точки: у ломаной это сами замеры,
   у дневного вида — края коробок, у профиля дня — края коридора. Общая форма
   и есть причина: рядов с разной геометрией стало больше одного. */
function niceScale(values) {
    // Нижняя и верхняя границы целевого диапазона всегда в кадре: без этого
    // ровный график «висел бы» без опоры, и зона не читалась бы.
    const min = Math.min(3, ...values);
    const max = Math.max(11, ...values);

    return { min: Math.floor(min) - 0.5, max: Math.ceil(max) + 0.5 };
}

/* Событийные дорожки рисуются только на почасовых окнах. За месяц отметок
   набирается сотня: они сливаются в сплошную полосу, из которой ничего не
   прочитать. На длинных окнах за события отвечает разбор ниже, а не график. */
function eventLanes() {
    if (!HOURLY_RANGES.has(state.activeRange)) return [];

    const events = state.snapshot.events || {};

    /* Снимок несёт события на самое длинное почасовое окно, короткому достаётся
       срез. Резать надо здесь, до масштаба и легенды: вчерашний большой обед
       иначе сжимал бы сегодняшние столбики, а легенда обещала бы ряд без
       единого столбика на холсте. */
    const since = state.snapshot.generated_at - SPAN_SECONDS[state.activeRange];
    const cut = (list) => (list || []).filter(([t]) => t >= since);

    const meals = cut(events.meals);
    const lanes = [];

    if (meals.length) {
        lanes.push({
            unit: "г",
            bars: meals.map(([t, v]) => ({ t, v, series: SERIES.meal })),
            marks: [],
        });
    }

    /* Короткий и длинный в одной дорожке: и то и другое меряется единицами, а
       разные шкалы для одной величины — тот самый второй вертикальный масштаб,
       который выдумывает связь на пустом месте. Различаются заливкой:
       короткий сплошной, длинный контуром. */
    const insulin = [
        ...cut(events.bolus).map(([t, v]) => ({ t, v, series: SERIES.insulin })),
        ...cut(events.basal).map(([t, v]) => ({ t, v, series: SERIES.basal })),
    ].sort((a, b) => a.t - b.t);

    /* Смена ручки стоит в дорожке инсулина: она про тот же инсулин, что и
       столбики. Дорожка нужна и без единого укола в окне — метке надо где-то
       стоять, а своя дорожка ради вертикальной черты была бы пустой полосой. */
    const pens = cut(events.pens).map(([t, insulin]) => ({
        t,
        series: insulin === "basal" ? SERIES.penBasal : SERIES.penBolus,
    }));

    if (insulin.length || pens.length) {
        lanes.push({ unit: "ед", bars: insulin, marks: pens });
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

    // Поверх столбиков и подписей: метка — граница «с этого момента другая
    // ручка», и столбик рядом с ней не должен её перекрывать.
    drawMarks(ctx, lane.marks || [], box, x);
}

/* Смена ручки: вертикальная черта на всю дорожку с кружком на макушке. Не
   столбик — количества нет, а черта читается как граница. Кружок залит у
   короткого и пуст у длинного, тем же правилом, что и столбики уколов. Круг
   сидит в верхнем запасе дорожки, там же, где подписи столбиков: подпись
   рядом с меткой иногда ляжет на неё, и это дешевле, чем дорожка выше на
   восемь пикселей ради события раз в месяц. */
function drawMarks(ctx, marks, box, x) {
    const bottom = box.top + LANE_HEIGHT;

    for (const mark of marks) {
        const centre = x(mark.t);
        if (centre < box.left - MARK_RADIUS || centre > box.right + MARK_RADIUS) continue;

        const color = readColor(mark.series.token, mark.series.fallback);
        const line = Math.round(centre) + 0.5;

        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(line, bottom);
        ctx.lineTo(line, box.top + MARK_RADIUS * 2);
        ctx.stroke();

        ctx.beginPath();
        ctx.arc(line, box.top + MARK_RADIUS, MARK_RADIUS, 0, Math.PI * 2);
        if (mark.series.hollow) {
            ctx.stroke();
        } else {
            ctx.fill();
        }
    }
}

/* Привратник профиля — по образцу eventLanes(): null всюду, где рисовать
   нечего или опасно. Коридор, разложенный по одной зоне под кривой,
   подписанной по другой, врёт на час-два молча — это хуже его отсутствия,
   поэтому чужая зона и несходящаяся длина слотов гасят его целиком. */

function dayProfile() {
    if (!HOURLY_RANGES.has(state.activeRange)) return null;

    const profile = state.snapshot.profile;
    if (!profile) return null;

    if (profile.tz !== TIMEZONE) {
        // Однократно: предупреждение о конфигурации, а не спам на каждую
        // перерисовку раз в минуту.
        if (!state.profileTzWarned) {
            state.profileTzWarned = true;
            console.warn(
                `Профиль дня нарезан в зоне ${profile.tz}, страница подписывает время в ${TIMEZONE} — коридор не рисуется.`
            );
        }
        return null;
    }

    if (!Array.isArray(profile.slots) || profile.slots.length !== 1440 / profile.slot_min) {
        return null;
    }

    return profile;
}

/* Раскладка профиля на непрерывные пробеги вдоль оси времени. Слот без данных
   даёт разрыв, а не интерполяцию: это ровно тот час, про который ничего не
   известно. Пробег из одного слота не рисуется — семипиксельный островок
   читается как артефакт отрисовки, а не как данные. */
function profileRuns(profile, startTime, endTime) {
    const step = profile.slot_min * 60;
    // Начало — на местной границе слота. Номер слота пересчитывается через
    // minutesOfDay на каждом шаге: в сутки перехода на летнее время слоты
    // съезжают вместе с местными часами, а не копят сдвиг до конца окна.
    let t = startTime - ((minutesOfDay(startTime) % profile.slot_min) * 60 + (startTime % 60));

    const runs = [];
    let run = null;
    for (; t < endTime; t += step) {
        const idx = Math.floor(minutesOfDay(t) / profile.slot_min);
        const slot = profile.slots[idx];
        if (!slot) {
            run = null;
            continue;
        }

        if (!run) {
            run = [];
            runs.push(run);
        }
        // Точка пробега — середина слота, прижатая к окну на его краях.
        // Номер слота едет вместе с точкой: по нему подсказка сверяется с
        // тем, что действительно нарисовано.
        run.push({
            t: Math.min(Math.max(t + step / 2, startTime), endTime),
            idx,
            p25: slot[0],
            p50: slot[1],
            p75: slot[2],
        });
    }

    return runs.filter((run) => run.length > 1);
}

/* Коридор 25–75 % с медианой — «обычный день» под сегодняшней кривой. */
function drawDayProfile(ctx, runs, x, y) {
    const color = readColor("--agp", "#8a90a6");
    const alpha = readNumber("--agp-alpha", 0.2);

    for (const run of runs) {
        ctx.beginPath();
        run.forEach((slot, index) => {
            if (index === 0) ctx.moveTo(x(slot.t), y(toMmol(slot.p75)));
            else ctx.lineTo(x(slot.t), y(toMmol(slot.p75)));
        });
        for (let i = run.length - 1; i >= 0; i -= 1) {
            ctx.lineTo(x(run[i].t), y(toMmol(run[i].p25)));
        }
        ctx.closePath();
        ctx.fillStyle = color;
        ctx.globalAlpha = alpha;
        ctx.fill();
        ctx.globalAlpha = 1;

        // Медиана — тонкой сплошной: она и есть «обычно», коридор — разброс.
        // Приглушена сильнее коридора: это контекст под сегодняшней кривой, и
        // там, где линии пересекаются, читаться должна сегодняшняя — на 0.55
        // медиана спорила с ней за внимание в каждой точке пересечения.
        ctx.beginPath();
        run.forEach((slot, index) => {
            if (index === 0) ctx.moveTo(x(slot.t), y(toMmol(slot.p50)));
            else ctx.lineTo(x(slot.t), y(toMmol(slot.p50)));
        });
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.25;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.globalAlpha = 0.35;
        ctx.stroke();
        ctx.globalAlpha = 1;
    }
}

/* Привратник прогноза — по образцу eventLanes() и dayProfile(): null всюду,
   где рисовать нечего или нечестно. Свежесть меряется от правого края холста
   (generated_at), а не от часов устройства: пока Abbott молчит, снимок
   продолжает штамповаться, точка отходит от края — и тянуть из неё будущее
   значило бы врать дважды. Порог — тот же, каким шапка гасит цвет значения. */
function forecastTail() {
    if (!HOURLY_RANGES.has(state.activeRange)) return null;

    const latest = state.snapshot.latest;
    if (!latest || latest.rate === null || latest.rate === undefined) return null;
    if ((state.snapshot.generated_at - latest.t) * 1000 > STALE_AFTER_MS) return null;

    let minutes = FORECAST_MINUTES;
    if (latest.rate > 0) {
        minutes = Math.min(minutes, (SENSOR_MAX_MGDL - latest.mgdl) / latest.rate);
    }
    if (latest.rate < 0) {
        minutes = Math.min(minutes, (SENSOR_MIN_MGDL - latest.mgdl) / latest.rate);
    }
    // Хвост короче минуты не читается — это уже не прогноз, а заусенец.
    if (minutes < 1) return null;

    return {
        from: { t: latest.t, mgdl: latest.mgdl },
        to: {
            t: latest.t + Math.round(minutes * 60),
            mgdl: latest.mgdl + latest.rate * minutes,
        },
    };
}

/* Порядок слоёв — контракт, по которому в каркас вставляются отрисовщики:
   профиль обычного дня → полоса нормы → сетка → подписи осей → полосы молчания
   сенсора → кривая с отсечениями по зонам (или дневные коробки) → одиночные
   точки → хвост прогноза → дорожки событий → перекрестие. Кто нарисован
   раньше, тот лежит ниже. */
function drawChart() {
    // Снимок прежнего сборщика может не знать окна «48 часов»: страница и
    // данные обновляются не одним щелчком, и минуту-другую после выкладки
    // здесь лежит старый data.json. Пустая панель на эту минуту — штатный
    // вид, а не падение на series.kind.
    const series = state.snapshot.series[state.activeRange] || { kind: "points", step: 5, points: [] };
    // Снимок прежнего сборщика приходит без kind: тогда рисуется прежняя
    // ломаная, ни одной ошибки в консоли.
    const daily = series.kind === "daily";
    const points = daily ? [] : series.points;
    const days = daily ? series.days : [];
    const lanes = daily ? [] : eventLanes();
    const tail = daily ? null : forecastTail();
    const gaps = daily ? [] : seriesGaps(series, points, tail);
    const artifacts = daily ? [] : seriesArtifacts();

    // Один предикат «есть ли что рисовать» на оба ранних выхода: у дневного
    // вида points не существует вовсе, и points.length здесь бы падал.
    const hasData = daily ? days.some((day) => day.count > 0) : points.length > 0;

    els.chartEmpty.hidden = hasData;
    els.canvas.setAttribute("aria-label", chartDescription(daily, hasData, tail, gaps, artifacts));

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

    if (!hasData) {
        // Легенда — до выхода: над надписью «данных нет» она не вправе
        // обещать ни одного ряда.
        renderLegend([]);
        state.geometry = null;
        hideTip();
        return;
    }

    // bottom с запасом на вторую строку подписи — дату на смене дня.
    const padding = { top: 12, right: 12, bottom: 38, left: 38 };
    const plotWidth = width - padding.left - padding.right;
    const lanesHeight = lanes.length * (LANE_HEIGHT + LANE_GAP);
    const plotHeight = height - padding.top - padding.bottom - lanesHeight;

    const now = state.snapshot.generated_at;
    const spanSeconds = SPAN_SECONDS[state.activeRange];
    const startTime = now - spanSeconds;
    // Правый край холста — не «сейчас», когда есть прогноз: будущему нужно
    // место, иначе хвост рисовать некуда. Метки оси при этом остаются на
    // долях самого окна — «сейчас» просто отходит от рамки на ширину хвоста.
    const endTime = tail ? Math.max(now, tail.to.t) : now;

    // Профиль обычного дня — до масштаба: края его коридора участвуют в
    // niceScale наравне с кривой, иначе коридор упирался бы в рамку.
    // Коридор дотягивается до конца хвоста прогноза, а не до «сейчас»:
    // «обычно» — климатология по времени суток, и ближайшие полчаса ей
    // известны не хуже прошедших. Пунктир без коридора рядом висел бы в
    // пустоте, которую страница только что уверенно называла «обычно».
    const profile = daily ? null : dayProfile();
    const runs = profile ? profileRuns(profile, startTime, endTime) : [];

    const scale = niceScale(
        daily
            ? days.filter((day) => day.count).flatMap((day) => [toMmol(day.p25), toMmol(day.p75)])
            : [
                  ...points.map((p) => toMmol(p[1])),
                  ...runs.flatMap((run) => run.flatMap((slot) => [toMmol(slot.p25), toMmol(slot.p75)])),
                  // Конец хвоста — в кадре наравне с кривой: обрезанный рамкой
                  // прогноз читался бы как «дальше неизвестно», а не «выше».
                  ...(tail ? [toMmol(tail.to.mgdl)] : []),
              ]
    );

    // Состав легенды собирает рисующая ветка — из нарисованного, а не из
    // state.snapshot.events: иначе месячное окно обещало бы «Углеводы» на графике
    // без единого столбика, а пустой профиль — коридор, которого нет.
    // Эпизоды, попавшие в окно: легенда называет полосу, только когда она на
    // холсте есть, — по тому же правилу, что и остальные её строки.
    const lowsShown =
        !daily &&
        (state.snapshot.lows || []).some(
            (low) => low.end >= startTime && low.start <= endTime
        );

    const legendItems = [];
    if (!daily && (lanes.length || runs.length || tail || lowsShown || gaps.length || artifacts.length)) {
        legendItems.push({ ...SERIES.glucose, line: true });
        // «Обычно» — сразу за измерением: коридор, с которым его сравнивают,
        // а не отметка на самой кривой, как всё, что ниже.
        if (runs.length) {
            /* С числом дней, по которым построен коридор. «Обычно» без него —
               обещание без выборки: коридор по семи дням и по четырнадцати
               выглядит одинаково уверенно, а значит разное. Число публикуется
               в снимке с самого начала (agp.py) и до сих пор нигде не читалось. */
            const days = state.snapshot.profile && state.snapshot.profile.days;
            legendItems.push(
                days
                    ? { ...SERIES.profile, label: `${SERIES.profile.label}, ${days} дн` }
                    : SERIES.profile
            );
        }
        // Дальше — хвост: он продолжение измерения, а не отдельная сущность.
        if (tail) {
            legendItems.push(SERIES.forecast);
        }
        // И следом — полоса ниже нормы: она про ту же кривую, а не про журнал.
        if (lowsShown) {
            legendItems.push(SERIES.low);
        }
        // Молчание сенсора — там же, среди знаков о кривой: узкая полоса
        // остаётся без подписи, и легенда для неё единственное имя.
        if (gaps.length) {
            legendItems.push(SERIES.gap);
        }
        // Кольца — тоже среди знаков о кривой: то же измерение, только с
        // оговоркой к числу, а не новый ряд.
        if (artifacts.length) {
            legendItems.push(SERIES.artifact);
        }
        for (const kind of [SERIES.meal, SERIES.insulin, SERIES.basal]) {
            if (lanes.some((lane) => lane.bars.some((bar) => bar.series === kind))) {
                legendItems.push(kind);
            }
        }
        // Метки — тем же правилом: строка есть, только когда метка на холсте.
        for (const kind of [SERIES.penBolus, SERIES.penBasal]) {
            if (lanes.some((lane) => (lane.marks || []).some((mark) => mark.series === kind))) {
                legendItems.push(kind);
            }
        }
    }
    renderLegend(legendItems);

    const x = (t) => padding.left + ((t - startTime) / (endTime - startTime)) * plotWidth;
    const y = (mmol) =>
        padding.top + plotHeight - ((mmol - scale.min) / (scale.max - scale.min)) * plotHeight;

    const styles = getComputedStyle(document.documentElement);

    const muted = readColor("--muted", "#8a90a6");
    const inRange = readColor("--in-range", "#7efcb0");
    const axisAlpha = readNumber("--axis-alpha", 0.85);

    // Профиль — самым нижним слоем (см. контракт порядка): «обычно» лежит
    // под всем, что случилось сегодня, включая подложку нормы.
    if (runs.length) drawDayProfile(ctx, runs, x, y);

    /* Три зоны — подложка, а не линии: так видно «сколько времени график
       провёл в какой», не считая пересечения глазами. Зелёная — норма натощак
       (до mid), жёлтая — допустимо после еды (mid–high), красная — выше цели;
       гипогликемия внизу красится своим красным, тем же, что и кривая в ней.
       Зоны режутся по краям панели: при узкой шкале y уходит за холст, и без
       обрезки красное закрашивало бы подписи осей. */
    const plotFloor = padding.top + plotHeight;
    const yTargetLow = y(toMmol(state.snapshot.target.low));
    const yTargetMid = y(toMmol(targetMid()));
    const yTargetHigh = y(toMmol(state.snapshot.target.high));
    const zones = [
        [yTargetMid, yTargetLow, inRange],
        [yTargetHigh, yTargetMid, readColor("--hyper", "#ffd166")],
        [padding.top, yTargetHigh, readColor("--high", "#f77e9b")],
        [yTargetLow, plotFloor, readColor("--hypo", "#ff5b5b")],
    ];
    ctx.globalAlpha = Number(styles.getPropertyValue("--band-alpha")) || 0.07;
    for (const [zoneTop, zoneBottom, color] of zones) {
        const from = Math.max(zoneTop, padding.top);
        const to = Math.min(zoneBottom, plotFloor);
        if (to - from <= 0) continue;
        ctx.fillStyle = color;
        ctx.fillRect(padding.left, from, plotWidth, to - from);
    }
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
    const ticks = plotWidth < 340 ? 2 : HOURLY_RANGES.has(state.activeRange) ? 4 : 5;
    let previousDay = null;

    // Дневной вид подписывает ось зоной снимка: его коробки нарезаны на
    // сервере, и дата под коробкой обязана совпадать с датой в её подсказке,
    // а не с константой страницы.
    const axisZone = daily ? displayTimezone() : TIMEZONE;

    for (let i = 0; i <= ticks; i += 1) {
        const t = startTime + (spanSeconds / ticks) * i;
        const date = new Date(t * 1000);
        const day = date.toLocaleDateString("ru-RU", {
            timeZone: axisZone,
            day: "numeric",
            month: "short",
        });

        // Почасовое окно пересекает полночь, и без даты непонятно, «02:35» —
        // это сегодня или вчера. Дата подписывается там, где день меняется,
        // а не у каждой метки: повторять её пять раз незачем.
        const labels = HOURLY_RANGES.has(state.activeRange)
            ? [
                  date.toLocaleTimeString("ru-RU", {
                      timeZone: TIMEZONE,
                      hour: "2-digit",
                      minute: "2-digit",
                  }),
              ]
            : [day];

        if (HOURLY_RANGES.has(state.activeRange) && day !== previousDay) {
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

    // Ширина коробки дня считается один раз и попадает в state.geometry: отрисовка
    // и подсветка наведения обязаны сходиться на одном прямоугольнике.
    const boxWidth = daily
        ? Math.min(BOX_MAX_WIDTH, Math.max(BOX_MIN_WIDTH, (x(86400) - x(0)) * BOX_SHARE))
        : null;

    if (daily) {
        drawDailyBoxes(ctx, days, x, y, padding.left, width - padding.right, boxWidth);
    } else {
        // До кривой: полоса гасит подложку зон и коридор «обычно», и кривая,
        // нарисованная раньше, тускнела бы у самого края молчания.
        drawGaps(
            ctx,
            gaps,
            x,
            padding.left,
            width - padding.right,
            padding.top,
            padding.top + plotHeight,
            muted,
            axisAlpha
        );
        drawSeriesLine(ctx, series, points, x, y, padding, plotWidth, plotHeight);
        if (tail) drawForecast(ctx, tail, x, y, padding.top, padding.top + plotHeight);
        // После кривой: отрезки лежат на линии порога и обязаны быть поверх
        // неё — под кривой их бы наполовину перекрыло ей же.
        drawLows(ctx, x, padding.left, width - padding.right, padding.top + plotHeight);
        // Кольца — последними из всего, что стоит на кривой: пометка обязана
        // быть видна поверх любой зоны и поверх отрезка гипогликемии под ней.
        drawArtifacts(ctx, artifacts, x, y);
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
    state.geometry = {
        kind: daily ? "daily" : "points",
        points,
        days,
        boxWidth,
        // Слоты профиля, дожившие до холста: подсказка не вправе говорить
        // «обычно» там, где пробег выброшен как одинокий островок.
        profileSlots: new Set(runs.flat().map((slot) => slot.idx)),
        // Ось времени остаётся линейной, поэтому hoverAt не меняется; день
        // ищется по границам его наблюдаемого куска.
        dayAt(t) {
            const found = days.find((day) => t >= day.start && t < day.end);
            const last = days[days.length - 1];
            return found || (last && t === last.end ? last : null);
        },
        laneBoxes,
        tail,
        // Промежутки молчания — наведению: «Измерение» из точки в получасе от
        // курсора приписывало бы замер часу, когда сенсора попросту не было.
        gaps,
        // Кольца — тоже наведению: подсказка обязана назвать причину пометки
        // рядом со значением, а не заставлять гадать по одному лишь кольцу.
        artifacts,
        x,
        y,
        startTime,
        // Наведению нужен нарисованный размах оси, с местом под хвост: доля
        // ширины холста переводится во время по нему, а не по длине окна.
        spanSeconds: endTime - startTime,
        plotTop: padding.top,
        plotBottom: padding.top + plotHeight,
        left: padding.left,
        right: width - padding.right,
        bottom: height - padding.bottom,
    };

    drawCrosshair(ctx, muted);
}

/* Тело кривой day- и week-окон: ломаная с отсечениями по зонам. Порог разрыва
   спрашивается у ряда, а не у каркаса: у дневного вида series.step не
   существует, NaN в сравнениях давал бы false и молча гасил все разрывы. */
/* Эпизоды ниже нормы — полосой по дну области графика, с длительностью рядом.

   Кривая и без них краснеет ниже 3,9, но отвечает она только на «было или не
   было». «Сколько раз» по ней не прочитать — два провала подряд сливаются в
   один росчерк, — а «сколько минут» не прочитать тем более: у недельной панели
   минута занимает четверть пикселя. Числа приходят из снимка посчитанными по
   сырым замерам (_lows в publish.py), поэтому отрезок не зависит от того, что
   уцелело при прореживании.

   По дну, а не по самой линии порога: там отрезок ложился ровно поперёк
   провала кривой, и два разных знака — «вот где кривая ушла вниз» и «вот
   сколько это длилось» — сливались в одну неразборчивую фигуру. Внизу полоса
   читается как отрезок времени, чем она и является.

   Минимальная ширина отрезка — три пикселя: двухминутный провал на недельном
   окне тоньше волоса, и без неё самый короткий эпизод был бы виден хуже всех,
   хотя ищут глазами как раз такие. */

const LOW_MIN_WIDTH = 3;

// Сколько места нужно подписи справа от отрезка. Она ставится сбоку, а не по
// центру: часовой эпизод на суточной панели занимает три десятка пикселей, то
// есть уже своей подписи, и по центру её не рисовал бы никто и никогда — как
// раз у коротких провалов длительность и есть главное, что о них известно.
const LOW_LABEL_SPACE = 42;

function drawLows(ctx, x, left, right, bottom) {
    const lows = state.snapshot.lows || [];
    if (!lows.length) return;

    // Полтора пикселя от дна: линия толщиной в три, и её нижняя половина
    // иначе срезалась бы краем области.
    const py = bottom - 2;
    const color = readColor("--hypo", "#ff5b5b");

    ctx.save();
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.lineCap = "round";
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 3;

    const visible = lows
        .map((low) => ({
            low,
            from: Math.max(left, x(low.start)),
            to: Math.min(right, x(low.end)),
        }))
        .filter((item) => item.to >= left && item.from <= right);

    for (const [index, item] of visible.entries()) {
        const width = Math.max(LOW_MIN_WIDTH, item.to - item.from);
        const end = item.from + width;

        ctx.beginPath();
        ctx.moveTo(item.from, py);
        ctx.lineTo(end, py);
        ctx.stroke();

        // Подпись молчит, когда следующий эпизод стоит слишком близко: число
        // поперёк соседнего провала хуже, чем отсутствие числа.
        const next = visible[index + 1];
        const room = Math.min(right, next ? next.from : right) - end;
        if (room >= LOW_LABEL_SPACE) {
            ctx.fillText(`${item.low.minutes} мин`, end + 4, py - 1);
        }
    }

    ctx.restore();
}

/* Разрыв длиннее трёх шагов ряда означает, что сенсор молчал: один пропуск —
   обычная задержка выгрузки, три подряд — уже молчание. Порог один на кривую и
   на полосу «нет сигнала»: разъехавшись, они нарисовали бы разрыв линии без
   объяснения — или объяснение там, где линия цела. */
const GAP_STEPS = 3;

function seriesGapSeconds(series) {
    return series.step * 60 * GAP_STEPS;
}

/* Промежутки, где сенсор молчал, — те же, по которым рвётся кривая.

   Считает их сборщик, по сырым замерам (_gaps в publish.py): границы корзины
   прореживания сдвигают края на свой шаг, и один и тот же промежуток на
   суточной панели назывался бы «1 ч 45 мин», а на двухсуточной — «1 ч 50 мин».
   Снимок прежнего сборщика их не несёт — тогда они собираются по точкам ряда,
   с этой самой точностью до корзины: приблизительная полоса лучше, чем
   необъяснённый разрыв кривой.

   Порог — не серверный, а свой на каждое окно: у двухсуточной панели шаг ряда
   вдвое крупнее, двадцатиминутный пропуск её кривую не рвёт, и объяснять на
   ней нечего.

   Последний промежуток — открытый: он кончится, когда сенсор заговорит, и
   отсчитывается от latest, точного времени последнего замера. Пока рядом
   нарисован прогноз, молчания нет по определению (хвост живёт только у свежего
   замера), и полоса, наползающая на пунктир, спорила бы с ним. */
function seriesGaps(series, points, tail) {
    if (!HOURLY_RANGES.has(state.activeRange) || !points.length) return [];

    const gapSeconds = seriesGapSeconds(series);
    const startTime = state.snapshot.generated_at - SPAN_SECONDS[state.activeRange];

    const counted = state.snapshot.gaps
        ? state.snapshot.gaps.map((gap) => ({ from: gap.start, to: gap.end }))
        : points
              .slice(1)
              .map((point, i) => ({ from: points[i][0], to: point[0] }));

    const gaps = counted.filter(
        (gap) => gap.to - gap.from > gapSeconds && gap.to >= startTime
    );

    const latest = state.snapshot.latest ? state.snapshot.latest.t : points[points.length - 1][0];
    if (!tail && state.snapshot.generated_at - latest > gapSeconds) {
        gaps.push({ from: latest, to: state.snapshot.generated_at, open: true });
    }

    return gaps;
}

/* Кольца рисует только суточная панель, не двое суток: снимок публикует
   их окном RANGES["day"] (см. publish.py), а страница выбирает более узко —
   решение оставить их редкой деталью суточного вида, не заводить вторую
   плотность на растянутой вдвое кривой. */
function seriesArtifacts() {
    if (state.activeRange !== "day" || !state.snapshot.artifacts) return [];

    const startTime = state.snapshot.generated_at - SPAN_SECONDS[state.activeRange];
    return state.snapshot.artifacts.filter((artifact) => artifact.t >= startTime);
}

// Полоса уже двух пикселей не читается как полоса, а разрыв в четверть часа на
// двухсуточном окне занимает как раз около того.
const GAP_MIN_WIDTH = 2;

/* Воздух вокруг подписи. Она набрана поперёк полосы, так что по ширине это
   зазор до пунктирных границ, а по высоте — до рамки области. */
const GAP_LABEL_PAD = 4;

// Высота строки 10px моно с запасом: по ней решается, влезла ли подпись в
// ширину полосы.
const GAP_LABEL_HEIGHT = 11;

const GAP_LABEL = "нет сигнала";

/* Молчание сенсора — полосой во всю высоту области графика, со словами внутри.
   Разрыв кривой виден и без неё, но отвечает только на «данных нет»; на «нет
   сегодня или нет совсем» и «надолго ли» по пустому месту не ответить, а как
   раз это и спрашивают, глядя на провал в линии.

   Полоса сначала гасит собой подложку зон и коридор «обычно» — цветом панели, —
   и лишь потом красится серым: иначе «обычно» продолжало бы обещать под ней
   знание, которого у страницы в эти часы нет. Дорожки событий полоса не
   трогает: журнал ведёт бот, и съеденное в час молчания сенсора записано
   ровно так же, как всё остальное.

   Подпись набрана поперёк полосы, снизу вверх. Вдоль не выходит: час молчания
   на суточном окне — полсотни пикселей, и «нет сигнала» в них не укладывается
   даже без длительности, а час — это ровно тот разрыв, ради которого полоса и
   заведена. Поперёк же места хватает почти всегда: высота области графика —
   три сотни пикселей против сотни, которую занимает вся строка.

   Уступает подпись по частям: в совсем узкой полосе остаётся одна
   длительность, а когда и та не влезает — полоса молчит, и называет её
   легенда. */
function drawGaps(ctx, gaps, x, left, right, top, bottom, muted, axisAlpha) {
    if (!gaps.length) return;

    const panel = readColor("--panel", "#0d0d14");
    const middleY = (top + bottom) / 2;

    ctx.save();
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.textAlign = "center";
    ctx.lineWidth = 1;
    ctx.strokeStyle = muted;

    for (const gap of gaps) {
        const from = Math.max(left, x(gap.from));
        const to = Math.min(right, x(gap.to));
        if (to <= left || from >= right) continue;

        const width = Math.max(GAP_MIN_WIDTH, to - from);

        ctx.globalAlpha = 0.7;
        ctx.fillStyle = panel;
        ctx.fillRect(from, top, width, bottom - top);

        ctx.globalAlpha = 0.1;
        ctx.fillStyle = muted;
        ctx.fillRect(from, top, width, bottom - top);

        // Границы — пунктиром, тем же, каким набран прогноз: где именно оборвался
        // сигнал, известно с точностью до корзины, и сплошная линия обещала бы
        // засечённый момент.
        ctx.globalAlpha = 0.4;
        ctx.setLineDash([3, 3]);
        for (const edge of [from, from + width]) {
            // Край окна — не край молчания: у открытого промежутка правой
            // границы нет, и рисовать её на рамке значит закрыть его.
            if (edge <= left || edge >= right) continue;
            ctx.beginPath();
            ctx.moveTo(Math.round(edge) + 0.5, top);
            ctx.lineTo(Math.round(edge) + 0.5, bottom);
            ctx.stroke();
        }
        ctx.setLineDash([]);

        // Полоса уже строки подписи немая при любой длительности: текст в ней
        // лёг бы на обе границы разом.
        if (width < GAP_LABEL_HEIGHT + GAP_LABEL_PAD * 2) continue;

        const span = formatSpan(gap.to - gap.from);
        const full = `${GAP_LABEL} · ${span}`;
        const room = bottom - top - GAP_LABEL_PAD * 2;
        const text = ctx.measureText(full).width <= room ? full : span;
        if (ctx.measureText(text).width > room) continue;

        ctx.save();
        ctx.globalAlpha = axisAlpha;
        ctx.fillStyle = muted;
        ctx.textBaseline = "middle";
        ctx.translate(from + width / 2, middleY);
        ctx.rotate(-Math.PI / 2);
        ctx.fillText(text, 0, 0);
        ctx.restore();
    }

    ctx.restore();
}

/* Пунктирное кольцо поверх кривой — не замена точке, а пометка рядом с ней:
   значение в базе не менялось, кольцо лишь говорит «этому скачку доверяй
   меньше обычного». Пунктир, а не заливка, — тем же приёмом, что и полоса
   молчания сенсора: форма отличает ряд, когда один только цвет на этом не
   вправе (см. SERIES.artifact).

   Цвет — --insulin, не --low: артефакт чаще всего сидит на жёлтой кривой
   выше нормы, и оранжевое кольцо на оранжевой линии не читалось вовсе
   (см. скриншот в истории коммита). Розовый рядом с жёлтым/зелёным/красным
   кривой различим в обеих темах; с дорожкой инсулина не путается — там
   квадраты и столбики, здесь пунктирный круг на самой кривой.

   Одна обводка, без тёмного контура под ней: кольцо и так не заливка,
   вторая обводка только размывала бы пунктир. */
function drawArtifacts(ctx, artifacts, x, y) {
    if (!artifacts.length) return;

    ctx.save();
    const insulin = readColor("--insulin", "#cc4fb0");
    for (const artifact of artifacts) {
        const cx = x(artifact.t);
        const cy = y(toMmol(artifact.mgdl));

        ctx.setLineDash([2.5, 2.5]);
        ctx.strokeStyle = insulin;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(cx, cy, 7, 0, Math.PI * 2);
        ctx.stroke();
    }
    ctx.restore();
}

function drawSeriesLine(ctx, series, points, x, y, padding, plotWidth, plotHeight) {
    // Соединять точки через молчание сенсора нельзя: пропуск выглядел бы
    // ровным трендом. Порог общий с полосой «нет сигнала» — см. GAP_STEPS.
    const gapSeconds = seriesGapSeconds(series);

    const traceSeries = () => {
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
    };

    ctx.strokeStyle = readColor("--accent", "#7eb8f7");
    ctx.lineWidth = 1.75;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    traceSeries();

    /* Гипогликемия — тот же путь, обведённый второй раз в границах полосы ниже
       нижнего порога. Красить по точкам нельзя: цвет менялся бы на замере, а не
       на пересечении 3,9, и при шаге в пять минут до пяти минут кривой уходило
       бы не в тот цвет — в обе стороны. Отсечение режет ровно по линии порога,
       так что граница цвета и есть порог.

       Порог берётся из target.low, а не из своей константы: подложка нормы
       нарисована по нему же, и разъехаться им нельзя — красное за пределами
       зелёной полосы читалось бы как ошибка графика. */
    const hypoTop = y(toMmol(state.snapshot.target.low));
    const plotBottom = padding.top + plotHeight;
    if (hypoTop < plotBottom) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(padding.left, hypoTop, plotWidth, plotBottom - hypoTop);
        ctx.clip();
        ctx.strokeStyle = readColor("--hypo", "#ff5b5b");
        traceSeries();
        ctx.restore();
    }

    /* Жёлтая полоса — между mid и high: сахар, допустимый после еды. Времени
       здесь набирается половина суток, так что это не тревога, а состояние —
       цвет спокойнее красных по краям. Пороги общие с зонами подложки: граница
       цвета кривой и граница зоны обязаны быть одной линией. */
    const hyperBottom = y(toMmol(state.snapshot.target.high));
    const midTop = y(toMmol(targetMid()));
    if (midTop > hyperBottom) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(padding.left, hyperBottom, plotWidth, midTop - hyperBottom);
        ctx.clip();
        ctx.strokeStyle = readColor("--hyper", "#ffd166");
        traceSeries();
        ctx.restore();
    }

    /* Выше high — красным всегда, была еда или нет. Не --hypo, а --high: тем
       же цветом страница зовёт «выше цели» в числе шапки и статистике, а
       красный гипогликемии остаётся только у неё. */
    if (hyperBottom > padding.top) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(padding.left, padding.top, plotWidth, hyperBottom - padding.top);
        ctx.clip();
        ctx.strokeStyle = readColor("--high", "#f77e9b");
        traceSeries();
        ctx.restore();
    }

    // Точка без соседей не даёт отрезка и не нарисовалась бы вовсе. Так
    // выглядит начало каждого нового сенсора — первые замеры одиночные.
    for (let i = 0; i < points.length; i += 1) {
        const previous = points[i - 1];
        const next = points[i + 1];
        const isolated =
            (!previous || points[i][0] - previous[0] > gapSeconds) &&
            (!next || next[0] - points[i][0] > gapSeconds);

        if (isolated) {
            ctx.fillStyle = readingColor(points[i][1]);
            ctx.beginPath();
            ctx.arc(x(points[i][0]), y(toMmol(points[i][1])), 2.5, 0, Math.PI * 2);
            ctx.fill();
        }
    }
}

/* Хвост прогноза: та же кривая тем же цветом, но штрихом — продолжение, а не
   замер. Одним цветом, без отсечений по зонам: окраска по порогам обещала бы
   линейной экстраполяции точность, которой у неё нет. Штрих в полную силу и
   чуть толще кривой: полупрозрачная версия терялась на подложке нормы.
   В конце — кольцо: куда прямая приводит через полчаса. Контур, а не заливка,
   тем же словарём, что у длинного инсулина, — «не замер».

   Полчаса на суточной оси — полтора десятка пикселей, на двухсуточной —
   меньше десяти: сама линия при любом штрихе читается заусенцем. Поэтому
   смысл несут кольцо и число рядом с ним, а штрих только связывает их с
   кривой — и набран мелко, чтобы в отведённые пиксели легло хоть несколько. */
function drawForecast(ctx, tail, x, y, plotTop, plotBottom) {
    const color = readColor("--accent", "#7eb8f7");
    const endX = x(tail.to.t);
    const endY = y(toMmol(tail.to.mgdl));

    ctx.strokeStyle = color;
    ctx.lineWidth = 2.25;
    ctx.lineCap = "butt";
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x(tail.from.t), y(toMmol(tail.from.mgdl)));
    ctx.lineTo(endX, endY);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.beginPath();
    ctx.arc(endX, endY, 3.5, 0, Math.PI * 2);
    // Заливка цветом панели: кольцо не должно терять контур там, где под ним
    // проходит коридор «обычно».
    ctx.fillStyle = readColor("--panel", "#0d0d14");
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.stroke();

    // Число у кольца — то же, что в подсказке, с той же оговоркой «≈»: без
    // него хвост нем, а наведение на десять пикселей — это упражнение, а не
    // интерфейс. Ставится с противоположной приходу хвоста стороны: падающий
    // приходит сверху — подпись вниз, растущий — вверх; у рамки прижимается
    // внутрь холста. Под буквами — ореол цветом панели, а не плашка: плашка
    // закрашивала прямоугольник подложки нормы, и в тёмной теме над зелёной
    // полосой висел чёрный короб. Ореол обводит ровно буквы — тот же приём,
    // что у выделенной кривой на оверлее.
    const text = `≈ ${formatMmol(tail.to.mgdl)}`;
    ctx.font = '11px "JetBrains Mono", monospace';
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const below = tail.to.mgdl <= tail.from.mgdl;
    const labelY = Math.min(
        Math.max(endY + (below ? 16 : -16), plotTop + 8),
        plotBottom - 8
    );
    ctx.lineJoin = "round";
    ctx.lineWidth = 3;
    ctx.strokeStyle = readColor("--panel", "#0d0d14");
    ctx.strokeText(text, endX - 1, labelY);
    ctx.fillStyle = color;
    ctx.fillText(text, endX - 1, labelY);
}

/* Горизонталь коробки дня: середина наблюдаемого куска, прижатая к рамке.
   Обрезанный окном крайний день наблюдается считанные пиксели, и без прижатия
   половина коробки вылезала бы в жёлоб оси слева или за «сейчас» справа. */
function dailyBoxSpan(day, x, plotLeft, plotRight, width) {
    const centre = (x(day.start) + x(day.end)) / 2;
    const left = Math.min(Math.max(centre - width / 2, plotLeft), plotRight - width);
    return { left, right: left + width };
}

/* Ширина коробки дня. Крайние дни окна — не сутки, а их кусок: сегодняшний
   обрезан «сейчас», первый — левым краем окна. Коробка полной ширины на куске
   в пару часов налезает на соседа: до пяти утра сегодняшняя карточка стояла
   бы поверх вчерашней, и никакой сдвиг это не чинит — до полудня справа от
   вчерашней коробки просто нет места на целую. Ширина по прожитой доле решает
   перекрытие геометрией и говорит правду: карточка куска суток уже карточки
   целых. Полные дни остаются одной ширины — 25-часовые сутки перехода никто
   не резал, и толще соседей они не становятся. */
function dailyBoxWidth(day, days, width) {
    if (day !== days[0] && day !== days[days.length - 1]) return width;
    return Math.max(BOX_MIN_WIDTH, width * Math.min(1, (day.end - day.start) / 86400));
}

/* Подневная форма месяца: коробка p25–p75 с засечкой медианы на каждый день.
   Коробка, а не столбик min–max, нарочно: час прогрева сенсора по 500 мг/дл
   уходит в p90+ и не двигает ни p75, ни медиану. Ширина у полных дней одна —
   от номинальных суток, а не от фактических: 25-часовой день перехода не
   должен выглядеть толще соседей. Обрезанные окном крайние дни уже —
   см. dailyBoxWidth. */
function drawDailyBoxes(ctx, days, x, y, plotLeft, plotRight, width) {
    const yLow = y(toMmol(state.snapshot.target.low));
    const yMid = y(toMmol(targetMid()));
    const yHigh = y(toMmol(state.snapshot.target.high));

    for (const day of days) {
        if (!day.count) continue;

        // Середина наблюдаемого куска: сегодняшняя коробка стоит в центре
        // прожитой части дня, то есть левее правого края холста.
        const boxWidth = dailyBoxWidth(day, days, width);
        const { left } = dailyBoxSpan(day, x, plotLeft, plotRight, boxWidth);
        const top = y(toMmol(day.p75));
        const bottom = y(toMmol(day.p25));

        // День с одним замером: p25 = p50 = p75, высота коробки ноль. Засечка
        // рисуется всегда, коробка — только когда её видно: день остаётся на
        // графике, но не притворяется разбросом.
        if (bottom - top >= 1) {
            /* Раскраска — пересечением с теми же полосами, которыми красится
               ломаная: граница цвета по-прежнему и есть порог. Пустые и
               отрицательные пересечения не рисуются — день целиком ниже
               нижнего порога выходит одноцветным, без полос-фантомов. */
            const parts = [
                [top, Math.min(bottom, yHigh), readColor("--high", "#f77e9b")],
                [Math.max(top, yHigh), Math.min(bottom, yMid), readColor("--hyper", "#ffd166")],
                [Math.max(top, yMid), Math.min(bottom, yLow), readColor("--accent", "#7eb8f7")],
                [Math.max(top, yLow), bottom, readColor("--hypo", "#ff5b5b")],
            ];

            ctx.globalAlpha = BOX_ALPHA;
            for (const [from, to, color] of parts) {
                if (to - from <= 0) continue;
                ctx.fillStyle = color;
                ctx.fillRect(left, from, boxWidth, to - from);
            }
            ctx.globalAlpha = 1;
        }

        // Засечка целиком цветом своей зоны — как одиночная точка на кривой:
        // резать её по порогу не во что, она лежит по одну его сторону.
        ctx.fillStyle = readingColor(day.p50);
        ctx.fillRect(left, y(toMmol(day.p50)) - 0.75, boxWidth, 1.5);

        /* Неполные сутки — штрихом под коробкой. Сама коробка не меняется:
           её квартили посчитаны честно, просто по куску дня, и перекрашивать
           их значило бы сказать, что день был другим. Штрих говорит ровно то,
           что есть, — этому дню верить меньше, чем соседям, — а сколько
           именно, называет подсказка при наведении. */
        if (day.partial) {
            ctx.fillStyle = readColor("--muted", "#8a90a6");
            ctx.globalAlpha = 0.55;
            ctx.fillRect(left, y(toMmol(day.p25)) + 4, boxWidth, 1);
            ctx.globalAlpha = 1;
        }
    }
}

/* Вертикаль через график и обе дорожки: она связывает столбик еды с точкой на
   кривой, ради чего всё это и рисуется рядом. У дневного вида вместо вертикали
   и кольца — подсветка слота суток: точки, вокруг которой рисовать кольцо,
   там нет. */
function drawCrosshair(ctx, muted) {
    if (state.hoverTime === null || !state.geometry) return;

    if (state.geometry.kind === "daily") {
        const day = state.geometry.dayAt(state.hoverTime);
        if (!day || !day.count) return;

        // Подсветка накрывает и наблюдаемый кусок, и коробку: у обрезанного
        // окном дня кусок — считанные пиксели, а прижатая к рамке коробка
        // стоит рядом с ним, и подсвечивать одно без другого значит
        // подсвечивать не то, на что смотрят.
        const boxWidth = dailyBoxWidth(day, state.geometry.days, state.geometry.boxWidth);
        const box = dailyBoxSpan(day, state.geometry.x, state.geometry.left, state.geometry.right, boxWidth);
        const from = Math.min(state.geometry.x(day.start), box.left);
        const to = Math.max(state.geometry.x(day.end), box.right);

        ctx.fillStyle = muted;
        ctx.globalAlpha = 0.08;
        ctx.fillRect(from, state.geometry.plotTop, to - from, state.geometry.plotBottom - state.geometry.plotTop);
        ctx.globalAlpha = 1;
        return;
    }

    const px = Math.round(state.geometry.x(state.hoverTime)) + 0.5;
    if (px < state.geometry.left || px > state.geometry.right) return;

    ctx.strokeStyle = muted;
    ctx.globalAlpha = 0.45;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px, state.geometry.plotTop);
    ctx.lineTo(px, state.geometry.bottom);
    ctx.stroke();
    ctx.globalAlpha = 1;

    const point = nearestPoint(state.hoverTime);
    if (!point) return;

    // Кольцо цветом панели: без него точка теряется там, где пересекает
    // собственную линию.
    ctx.beginPath();
    ctx.arc(state.geometry.x(point[0]), state.geometry.y(toMmol(point[1])), 4, 0, Math.PI * 2);
    // Тот же цвет, что у линии под точкой: синий кружок посреди красного
    // участка читался бы как «а вот это измерение в норме».
    ctx.fillStyle = readingColor(point[1]);
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = readColor("--panel", "#0d0d14");
    ctx.stroke();
}

/* ── Легенда и подсказка ───────────────────────────────────────────── */

function legendItem(series) {
    const item = document.createElement("li");
    item.className = "legend__item";

    const key = seriesKey(series);

    const text = document.createElement("span");
    text.textContent = series.label;

    item.append(key, text);
    return item;
}

/* Легенда есть всегда, когда рядов больше одного: опознавать их по цвету на
   глаз — единственный канал, который отказывает и при дальтонизме, и на
   распечатке. Один ряд легенды не требует — его называет заголовок.

   Состав приходит от рисующей ветки готовым списком: легенда не заглядывает
   в снимок сама, чтобы не обещать ряды, которых на холсте нет. */
function renderLegend(items) {
    if (items.length < 2) {
        els.legend.hidden = true;
        els.legend.replaceChildren();
        return;
    }

    els.legend.replaceChildren(...items.map(legendItem));
    els.legend.hidden = false;
}

function nearestPoint(t) {
    if (!state.geometry || !state.geometry.points.length) return null;

    let best = null;
    for (const point of state.geometry.points) {
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
    // Пунктирный ряд и в подсказке помечен пунктиром — см. SERIES.forecast.
    if (series.dashed) {
        key.style.background = "none";
        key.style.border = "1.5px dashed currentColor";
    }
    // Молчание сенсора — той же полосой с прорехой, что в легенде: в разрыве
    // подсказка показывает его строку рядом с «Обычно», а цвет у них общий.
    if (series.striped) {
        key.style.background =
            "repeating-linear-gradient(90deg, currentColor 0 2px, transparent 2px 4px)";
        key.style.opacity = "0.6";
    }
    // Метка смены ручки — кружком, как в легенде.
    if (series.marker) {
        key.style.borderRadius = "50%";
    }

    const label = document.createElement("span");
    label.textContent = text;

    row.append(key, label);
    return row;
}

function showTip(clientX) {
    if (state.hoverTime === null || !state.geometry) return hideTip();
    if (state.geometry.kind === "daily") return showDayTip(clientX);

    const point = nearestPoint(state.hoverTime);
    const rows = [];

    // Справа от последнего замера строка глюкозы уступает место прогнозу:
    // «Измерение» под курсором в будущем приписывало бы старой точке чужое
    // время.
    const tail = state.geometry.tail;
    const future = tail && state.hoverTime > tail.from.t;

    /* Внутри молчания сенсора замера нет — и строки о нём тоже. nearestPoint
       тянется на полчаса в обе стороны, так что без этой развилки край
       часового разрыва подписывался бы значением, снятым до него. */
    const gap = (state.geometry.gaps || []).find(
        (item) => state.hoverTime > item.from && state.hoverTime < item.to
    );

    // Порядок строк зафиксирован: глюкоза → профиль → кольцо → события.
    if (gap) {
        rows.push(tipRow(SERIES.gap, `Нет сигнала ${formatSpan(gap.to - gap.from)}`));
    } else if (point && !future) {
        // «Измерение» у любой точки, свежей и старой: одно слово на всю ось.
        // «Сейчас» у свежей было отвергнуто — две метки для одного ряда
        // читаются как два ряда.
        rows.push(
            tipRow(SERIES.glucose, `Измерение ${formatMmol(point[1])} ${SERIES.glucose.unit}`)
        );
    }

    if (future && state.hoverTime <= tail.to.t) {
        const share = (state.hoverTime - tail.from.t) / (tail.to.t - tail.from.t);
        const mgdl = tail.from.mgdl + (tail.to.mgdl - tail.from.mgdl) * share;
        // «≈» — единственная строка подсказки с оговоркой: остальные
        // пересказывают записи, эта — прямую, продлённую в будущее.
        rows.push(
            tipRow(SERIES.forecast, `Прогноз ≈ ${formatMmol(mgdl)} ${SERIES.forecast.unit}`)
        );
    }

    const profile = dayProfile();
    if (profile) {
        const idx = Math.floor(minutesOfDay(state.hoverTime) / profile.slot_min);
        const slot = profile.slots[idx];
        // Сверка с нарисованным: пробег из одного слота на холст не попал,
        // и подсказка не вправе говорить «обычно» там, где коридора нет.
        if (slot && state.geometry.profileSlots.has(idx)) {
            rows.push(
                tipRow(
                    SERIES.profile,
                    `Обычно ${formatMmol(slot[1])} · ${formatMmol(slot[0])}–${formatMmol(slot[2])} ${SERIES.profile.unit}`
                )
            );
        }
    }

    // Кольцо — после «Обычно», перед событиями журнала: оговорка к измерению
    // выше, а не запись в дорожке. Допуск уже, чем у lane-событий (900с):
    // метка стоит на своём замере, секунда в секунду с точностью до шага
    // записи, и получасовой люфт nearestPoint приписал бы её соседней точке
    // через полчаса тишины.
    if (point && !future && !gap) {
        const artifact = (state.geometry.artifacts || []).find(
            (item) => Math.abs(item.t - point[0]) <= 150
        );
        if (artifact) {
            const direction = artifact.dv > 0 ? "рост" : "падение";
            rows.push(
                tipRow(
                    SERIES.artifact,
                    `Возможный шум сенсора · ${direction} ${Math.abs(artifact.dv).toFixed(1)} ммоль/л за ${artifact.dsec} с`
                )
            );
        }
    }

    // Событие в пределах четверти часа от курсора: столбик и точка кривой
    // почти никогда не совпадают по времени секунда в секунду.
    for (const box of state.geometry.laneBoxes) {
        for (const bar of box.lane.bars) {
            if (Math.abs(bar.t - state.hoverTime) <= 900) {
                rows.push(tipRow(bar.series, `${formatAmount(bar.v)} ${bar.series.unit}`));
            }
        }
        // Метка смены ручки — тем же допуском. Числа у неё нет, строка
        // называет её словами легенды.
        for (const mark of box.lane.marks || []) {
            if (Math.abs(mark.t - state.hoverTime) <= 900) {
                rows.push(tipRow(mark.series, mark.series.label));
            }
        }
    }

    if (!rows.length) return hideTip();

    const time = document.createElement("p");
    time.className = "tip__time";
    // Зона названа здесь, а не у оси: подсказку читают, когда сверяют запись со
    // своими часами, и «02:43» без города в поездке значит два разных момента.
    time.textContent = `${new Date(state.hoverTime * 1000).toLocaleTimeString("ru-RU", {
        timeZone: TIMEZONE,
        hour: "2-digit",
        minute: "2-digit",
    })}, ${TIMEZONE_LABEL}`;

    els.tip.replaceChildren(time, ...rows);
    placeTip(els.tip, els.chart, els.canvas, state.geometry.plotTop, clientX);
}

/* Подсказка дневного вида: не мгновение, а сутки. Медиана с меткой цвета
   зоны, разброс середины, доля времени в диапазоне — те же числа, какими
   день покрашен на холсте, только словами. */
function showDayTip(clientX) {
    const day = state.geometry.dayAt(state.hoverTime);
    if (!day || !day.count) return hideTip();

    const time = document.createElement("p");
    time.className = "tip__time";
    // Дата — по зоне снимка, и зона названа вслух: «5 сентября» без города в
    // поездке значит два разных дня. Короткое имя города — только пока зоны
    // сходятся: подписывать московскую нарезку «Белградом» хуже, чем показать
    // сырое имя зоны.
    const zone = displayTimezone();
    const zoneLabel = zone === TIMEZONE ? TIMEZONE_LABEL : zone;
    time.textContent = `${new Date(day.start * 1000).toLocaleDateString("ru-RU", {
        timeZone: zone,
        day: "numeric",
        month: "long",
    })}, ${zoneLabel}`;

    // Метка медианы — тем же цветом, что её засечка на холсте.
    const median = tipRow(
        SERIES.glucose,
        `Медиана ${formatMmol(day.p50)} ${SERIES.glucose.unit}`
    );
    const key = median.querySelector(".tip__key");
    key.style.background = readingColor(day.p50);
    key.style.color = readingColor(day.p50);

    const spread = document.createElement("p");
    spread.className = "tip__row";
    spread.textContent = `25–75 %: ${formatMmol(day.p25)} – ${formatMmol(day.p75)}`;

    const tir = document.createElement("p");
    tir.className = "tip__row";
    tir.textContent = `В диапазоне ${percent(day.tir)}`;

    const note = document.createElement("p");
    note.className = "tip__note";
    note.textContent = `Ниже ${percent(day.below)} · выше ${percent(day.above)}`;

    const rows = [time, median, spread, tir, note];

    /* Неполный день говорит об этом вслух. Коробка у него той же ширины, что у
       целых суток, и квартили выглядят так же уверенно — а посчитаны они по
       обрезанному куску: по краям окна день входит хвостом, а в середине его
       может проредить молчание сенсора. Снимок отмечал это с самого начала
       (partial, coverage в _daily), но никто не читал. */
    if (day.partial) {
        // Две причины неполноты, и они означают разное: обрезанные окном сутки
        // — «это ещё не весь день», редкие замеры — «этому дню верить меньше».
        // Совпасть они тоже могут: сегодняшнее утро с молчавшим сенсором.
        const reasons = [];
        if (day.clipped) reasons.push("сутки показаны не целиком");
        if (day.coverage < 100) reasons.push(`покрытие ${percent(day.coverage)}`);

        const partial = document.createElement("p");
        partial.className = "tip__note tip__note--warn";
        partial.textContent = reasons.join(" · ");
        rows.push(partial);
    }

    els.tip.replaceChildren(...rows);
    placeTip(els.tip, els.chart, els.canvas, state.geometry.plotTop, clientX);
}

/* Позиция считается от карточки, а не от холста: у карточки есть внутренний
   отступ, и без поправки подсказка уезжает на его ширину.

   Карточка и холст приходят параметрами — тем же счётом живёт подсказка
   оверлея в разборе приёмов. Своё у неё только содержимое: прижать её к краю
   собственной карточки нужно ровно так же. */
function placeTip(tip, host, canvas, plotTop, clientX) {
    tip.hidden = false;

    const card = host.getBoundingClientRect();
    const box = canvas.getBoundingClientRect();
    const half = tip.offsetWidth / 2;
    const wanted = clientX - card.left;

    tip.style.left = `${Math.min(Math.max(wanted, half + 4), card.width - half - 4)}px`;
    tip.style.top = `${box.top - card.top + plotTop}px`;
}

function hideTip() {
    els.tip.hidden = true;
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

function hoverAt(event) {
    if (!state.geometry) return;

    const rect = els.canvas.getBoundingClientRect();
    const px = event.clientX - rect.left;
    if (px < state.geometry.left || px > state.geometry.right) {
        clearHover();
        return;
    }

    const share = (px - state.geometry.left) / (state.geometry.right - state.geometry.left);
    state.hoverTime = Math.round(state.geometry.startTime + share * state.geometry.spanSeconds);
    drawChart();
    showTip(event.clientX);
}

function clearHover() {
    if (state.hoverTime === null) return;
    state.hoverTime = null;
    hideTip();
    if (state.snapshot && state.snapshot.latest) drawChart();
}

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
