import { state } from "./state.js";
import { els, readColor, readNumber } from "./dom.js";
import { SERIES, targetMid, readingColor, seriesKey } from "./series.js";
import { RANGE_LABELS, SPAN_SECONDS, HOURLY_RANGES } from "./ranges.js";
import { STALE_AFTER_MS } from "./now-stats.js";
import {
    TIMEZONE,
    TIMEZONE_LABEL,
    displayTimezone,
    minutesOfDay,
    toMmol,
    formatMmol,
    percent,
    formatSpan,
    formatAmount,
} from "./format.js";

/* Горизонт пунктирного прогноза. Линейное продолжение по 15-минутной скорости
   честно на полчаса — дальше еда и инсулин ломают прямую раньше, чем она
   успевает сбыться. Скорость — та же latest.rate, по которой рисуется стрелка
   тренда: две подписи одного числа не вправе разойтись. */
export const FORECAST_MINUTES = 30;

/* Шкала сенсора: значений за её пределами Libre не отдаёт, и хвост, ушедший
   ниже 40 мг/дл, обещал бы замер, которого не может быть. На границе шкалы
   хвост обрезается, а не ложится горизонталью. */
export const SENSOR_MIN_MGDL = 40;
export const SENSOR_MAX_MGDL = 500;

// Высота холста без дорожек событий — та же, что была до их появления.
export const PLOT_HEIGHT = 340;
export const LANE_HEIGHT = 34;
export const LANE_GAP = 8;
export const COLUMN_WIDTH = 7;
// Кружок на макушке метки смены ручки. Чуть шире половины столбика: метка
// стоит между столбиками и не должна теряться рядом с ними.
export const MARK_RADIUS = 3.5;

// Просвет между соседними подписями на дорожке. Впритык поставленные числа
// читаются как одно: «45» и «60» в паре пикселей друг от друга — это «4560».
export const LABEL_GAP = 4;

/* Дневные коробки месячного вида. Ширина одна на все полные дни: коробка —
   карточка суток, а не их длительность, и 25-часовые сутки перехода на зимнее
   время не должны выглядеть толще соседей. Обрезанные окном крайние дни уже —
   по прожитой доле, см. dailyBoxWidth. Заливка полупрозрачная, чтобы засечка
   медианы читалась и поверх коробки собственного цвета. */
export const BOX_SHARE = 0.6;
export const BOX_MIN_WIDTH = 3;
export const BOX_MAX_WIDTH = 22;
export const BOX_ALPHA = 0.45;

/* ── График ────────────────────────────────────────────────────────── */

/* Что «видно» на графике, словами: сам холст для скринридера пуст, а
   пересказывать сотни точек бессмысленно — нужен итог. Числа окна — из
   stats, то есть по сырым замерам, а не по нарисованной сводке. */
export function chartDescription(daily, hasData, tail, gaps, artifacts) {
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
export function niceScale(values) {
    // Нижняя и верхняя границы целевого диапазона всегда в кадре: без этого
    // ровный график «висел бы» без опоры, и зона не читалась бы.
    const min = Math.min(3, ...values);
    const max = Math.max(11, ...values);

    return { min: Math.floor(min) - 0.5, max: Math.ceil(max) + 0.5 };
}

/* Событийные дорожки рисуются только на почасовых окнах. За месяц отметок
   набирается сотня: они сливаются в сплошную полосу, из которой ничего не
   прочитать. На длинных окнах за события отвечает разбор ниже, а не график. */
export function eventLanes() {
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
export function columnPath(ctx, left, top, width, bottom) {
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

export function drawLane(ctx, lane, box, x, muted, axisAlpha) {
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
export function drawMarks(ctx, marks, box, x) {
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

export function dayProfile() {
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
export function profileRuns(profile, startTime, endTime) {
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
export function drawDayProfile(ctx, runs, x, y) {
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
export function forecastTail() {
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
export function drawChart() {
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

export const LOW_MIN_WIDTH = 3;

// Сколько места нужно подписи справа от отрезка. Она ставится сбоку, а не по
// центру: часовой эпизод на суточной панели занимает три десятка пикселей, то
// есть уже своей подписи, и по центру её не рисовал бы никто и никогда — как
// раз у коротких провалов длительность и есть главное, что о них известно.
export const LOW_LABEL_SPACE = 42;

export function drawLows(ctx, x, left, right, bottom) {
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
export const GAP_STEPS = 3;

export function seriesGapSeconds(series) {
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
export function seriesGaps(series, points, tail) {
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
export function seriesArtifacts() {
    if (state.activeRange !== "day" || !state.snapshot.artifacts) return [];

    const startTime = state.snapshot.generated_at - SPAN_SECONDS[state.activeRange];
    return state.snapshot.artifacts.filter((artifact) => artifact.t >= startTime);
}

// Полоса уже двух пикселей не читается как полоса, а разрыв в четверть часа на
// двухсуточном окне занимает как раз около того.
export const GAP_MIN_WIDTH = 2;

/* Воздух вокруг подписи. Она набрана поперёк полосы, так что по ширине это
   зазор до пунктирных границ, а по высоте — до рамки области. */
export const GAP_LABEL_PAD = 4;

// Высота строки 10px моно с запасом: по ней решается, влезла ли подпись в
// ширину полосы.
export const GAP_LABEL_HEIGHT = 11;

export const GAP_LABEL = "нет сигнала";

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
export function drawGaps(ctx, gaps, x, left, right, top, bottom, muted, axisAlpha) {
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
export function drawArtifacts(ctx, artifacts, x, y) {
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

export function drawSeriesLine(ctx, series, points, x, y, padding, plotWidth, plotHeight) {
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
export function drawForecast(ctx, tail, x, y, plotTop, plotBottom) {
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
export function dailyBoxSpan(day, x, plotLeft, plotRight, width) {
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
export function dailyBoxWidth(day, days, width) {
    if (day !== days[0] && day !== days[days.length - 1]) return width;
    return Math.max(BOX_MIN_WIDTH, width * Math.min(1, (day.end - day.start) / 86400));
}

/* Подневная форма месяца: коробка p25–p75 с засечкой медианы на каждый день.
   Коробка, а не столбик min–max, нарочно: час прогрева сенсора по 500 мг/дл
   уходит в p90+ и не двигает ни p75, ни медиану. Ширина у полных дней одна —
   от номинальных суток, а не от фактических: 25-часовой день перехода не
   должен выглядеть толще соседей. Обрезанные окном крайние дни уже —
   см. dailyBoxWidth. */
export function drawDailyBoxes(ctx, days, x, y, plotLeft, plotRight, width) {
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
export function drawCrosshair(ctx, muted) {
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

export function legendItem(series) {
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
export function renderLegend(items) {
    if (items.length < 2) {
        els.legend.hidden = true;
        els.legend.replaceChildren();
        return;
    }

    els.legend.replaceChildren(...items.map(legendItem));
    els.legend.hidden = false;
}

export function nearestPoint(t) {
    if (!state.geometry || !state.geometry.points.length) return null;

    let best = null;
    for (const point of state.geometry.points) {
        const distance = Math.abs(point[0] - t);
        if (best === null || distance < best[0]) best = [distance, point];
    }
    // Дальше получаса — это уже другой участок кривой, а не то, на что навели.
    return best[0] > 1800 ? null : best[1];
}

export function tipRow(series, text) {
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

export function showTip(clientX) {
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
export function showDayTip(clientX) {
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
export function placeTip(tip, host, canvas, plotTop, clientX) {
    tip.hidden = false;

    const card = host.getBoundingClientRect();
    const box = canvas.getBoundingClientRect();
    const half = tip.offsetWidth / 2;
    const wanted = clientX - card.left;

    tip.style.left = `${Math.min(Math.max(wanted, half + 4), card.width - half - 4)}px`;
    tip.style.top = `${box.top - card.top + plotTop}px`;
}

export function hideTip() {
    els.tip.hidden = true;
}

export function hoverAt(event) {
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

export function clearHover() {
    if (state.hoverTime === null) return;
    state.hoverTime = null;
    hideTip();
    if (state.snapshot && state.snapshot.latest) drawChart();
}
