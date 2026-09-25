import { state } from "./state.js";
import { els } from "./dom.js";
import { SERIES, zoneColor, seriesKey } from "./series.js";
import { RANGE_LABELS, PREV_LABELS, HOURLY_RANGES } from "./ranges.js";
import {
    formatMmol,
    formatAgo,
    formatEventAgo,
    formatDateTime,
    formatAmount,
    percent,
    trendArrow,
} from "./format.js";

// Насколько свежим считается измерение. Сенсор отдаёт точку раз в 5 минут,
// graph отстаёт ещё на несколько — 20 минут отделяют «сейчас» от «сенсор снят»
// без ложных срабатываний на обычной задержке.
export const STALE_AFTER_MS = 20 * 60 * 1000;

export function renderNow() {
    const latest = state.snapshot.latest;

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

    // Журнал молчанию сенсора не подчиняется: когда замеры устарели, «что ел и
    // чем колол» — единственное, что на странице осталось свежим.
    renderNowEvents();

    els.now.hidden = false;
}

/* Три строки рядом с текущим значением: последнее из журнала за двое суток —
   столько событий несёт снимок (EVENT_WINDOW в publish.py). Ряды те же, что на
   графике, и метку берут оттуда же — цвет квадратика обязан значить в плашке
   ровно то же, что под холстом.

   Имена короче легендных: «Короткий инсулин» рядом с «5 ед» договаривает то,
   что уже сказано единицей, а колонка узкая. Еда осталась «Углеводами» —
   99,3 г это углеводы, а не вес тарелки, и «Еда» на этом месте врёт. */
const NOW_EVENTS = [
    { lane: "meals", series: SERIES.meal, label: "Углеводы", unit: "г" },
    { lane: "bolus", series: SERIES.insulin, label: "Короткий", unit: "ед" },
    { lane: "basal", series: SERIES.basal, label: "Длинный", unit: "ед" },
];

// Тот же порог, по которому разбор склеивает записи в один приём пищи
// (SAME_MEAL_GAP в analysis.py).
const SAME_MEAL_GAP_SEC = 30 * 60;

// Записи в дорожке идут от старых к новым — последняя и есть последняя.
function lastEvent(lane) {
    const entries = (state.snapshot.events || {})[lane] || [];
    if (!entries.length) return null;

    const [seconds, amount] = entries[entries.length - 1];
    return { t: seconds, amount };
}

/* Приём, записанный в несколько заходов, — одна еда, как и в таблице разбора:
   иначе плашка покажет последнюю добавку («14 г») вместо тарелки, к которой
   она была добавлена. Время — начало приёма, тем же правилом, что в разборе. */
function lastMeal() {
    const meals = (state.snapshot.events || {}).meals || [];
    if (!meals.length) return null;

    let start = meals.length - 1;
    let carbs = meals[start][1];
    while (start > 0 && meals[start][0] - meals[start - 1][0] <= SAME_MEAL_GAP_SEC) {
        start -= 1;
        carbs += meals[start][1];
    }

    return { t: meals[start][0], amount: carbs };
}

function nowEventRow(kind, event) {
    const item = document.createElement("li");
    item.className = "now__event";
    // Роль — руками: .now__event раскладывается display: contents, а он
    // вынимает строку из дерева доступности вместе со смыслом списка.
    item.setAttribute("role", "listitem");

    const label = document.createElement("span");
    label.textContent = kind.label;

    const value = document.createElement("span");
    value.className = "now__event-value";
    value.textContent = formatAmount(event.amount);

    const unit = document.createElement("span");
    unit.className = "now__event-unit";
    unit.textContent = kind.unit;

    const ago = document.createElement("span");
    ago.className = "now__event-ago";
    ago.textContent = formatEventAgo(Date.now() - event.t * 1000);

    item.append(seriesKey(kind.series), label, value, unit, ago);
    return item;
}

function renderNowEvents() {
    const rows = NOW_EVENTS.map((kind) => [
        kind,
        kind.lane === "meals" ? lastMeal() : lastEvent(kind.lane),
    ]).filter(([, event]) => event);

    // Строки «Длинный — нет данных» нет: она занимает место, не сообщая
    // ничего, кроме того, что журнал за двое суток пуст.
    els.nowEvents.replaceChildren(...rows.map(([kind, event]) => nowEventRow(kind, event)));
    els.nowEvents.hidden = rows.length === 0;
}

function statCard(label, value, hint, compare) {
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

    // Строка сравнения с предыдущим периодом — не у каждой карточки, поэтому
    // четвёртый аргумент необязателен, а на объект statCard не переводится.
    if (compare) {
        const compareEl = document.createElement("p");
        compareEl.className = compare.strong
            ? "stat__delta stat__delta--strong"
            : "stat__delta";
        compareEl.textContent = compare.text;
        card.append(compareEl);
    }
    return card;
}

/* Строка «против предыдущего периода» для карточки. Причину отсутствия
   сравнения называет сборщик, а не пустота на странице: «предыдущего периода
   нет» и «в нём слишком мало измерений» — разные фразы, и выбирает между ними
   тот, кто знает счёт. Направление несёт стрелка, оценку — слово, значимость —
   сила цвета: новых токенов нет, зелёный и красный на этой странице заняты
   зонами гликемии, и «лучше» рядом с ними читалось бы как «в диапазоне». */
function compareRow(prev, key, formatDelta, formatWas) {
    if (!prev) return null;

    if (prev.reason) {
        // Одной фразы достаточно — на первой карточке, а не на каждой.
        if (key !== "tir") return null;
        return {
            text:
                prev.reason === "no_data"
                    ? "предыдущего периода нет"
                    : "в предыдущем периоде слишком мало измерений",
            strong: false,
        };
    }

    const metric = prev[key];
    if (!metric) return null;

    const arrow = metric.delta > 0 ? "↑" : metric.delta < 0 ? "↓" : "→";
    const verdict =
        metric.better === true ? " лучше" : metric.better === false ? " хуже" : "";

    return {
        text: `${arrow} ${formatDelta(Math.abs(metric.delta))}${verdict} · ${
            PREV_LABELS[state.activeRange]
        } ${formatWas(metric.was)}`,
        strong: metric.significant,
    };
}

export function renderStats() {
    const stats = state.snapshot.stats[state.activeRange];
    els.stats.replaceChildren();

    if (!stats) {
        els.stats.hidden = true;
        return;
    }

    const prev = stats.prev;
    const cards = [
        statCard("В целевом диапазоне", percent(stats.tir),
            `ниже ${percent(stats.below)} · выше ${percent(stats.above)}`,
            compareRow(prev, "tir",
                (delta) => `${formatAmount(delta)} %`,
                (was) => percent(was))),
        statCard("Среднее", formatMmol(stats.avg), "ммоль/л",
            compareRow(prev, "avg",
                (delta) => `${formatMmol(delta)} ммоль/л`,
                (was) => formatMmol(was))),
        statCard("Разброс", `${formatMmol(stats.min)} – ${formatMmol(stats.max)}`, "ммоль/л, минимум и максимум"),
    ];

    // cv приходит null, если среднее нулевое. Случай почти невозможный
    // (сенсор не отдаёт значений ниже 40 мг/дл), но обращение к методу у null
    // уронило бы отрисовку целиком — вместе с графиком и текущим значением.
    // Строки сравнения у вариабельности нет нарочно — см. _compare в
    // publish.py: при падающем среднем cv растёт чисто арифметически.
    if (stats.cv !== null && stats.cv !== undefined) {
        cards.push(statCard("Вариабельность", percent(stats.cv),
            stats.cv <= 36 ? `стабильно, норма ≤ ${percent(36)}` : `выше нормы ≤ ${percent(36)}`));
    }

    /* GMI живёт по своему окну в две недели, а не по выбранному периоду: под
       одним названием иначе оказывались бы два разных числа. Снимок отдаёт
       null, когда за две недели набралось меньше 70 % измерений — тогда
       карточки просто нет, вместо солидно выглядящей выдумки. На почасовых
       панелях не показываем: они про сегодня и вчера, а GMI — про две недели. */
    const gmi = state.snapshot.gmi;
    if (!HOURLY_RANGES.has(state.activeRange) && gmi) {
        cards.push(
            statCard("GMI", percent(gmi.value), `расчётный HbA1c за ${gmi.days} дней`)
        );
    }

    cards.push(statCard("Измерений", stats.count.toLocaleString("ru-RU"), RANGE_LABELS[state.activeRange]));

    els.stats.append(...cards);
    els.stats.hidden = false;
}
