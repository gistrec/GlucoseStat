/* Исследования — окно поверх страницы, по вкладке на разбор.

   Отдельно от страницы с данными намеренно: разборы — снимок на дату, а всё
   остальное здесь живое, и смешанные на одном полотне они читались бы как
   ещё одна плитка статистики, которая обновится через минуту. Числа ниже
   переписаны из docs/research в репозитории GlucoseBot и меняются только
   вместе с новым разбором — data.json их не несёт.

   <dialog>, а не самодельная подложка: Esc, ловушку фокуса и возврат фокуса
   на кнопку браузер делает сам. */

const STUDIES = [
    {
        id: "forecast",
        tab: "Прогноз",
        title: "Насколько точно модель предсказывает сахар",
        meta: "6 октября 2026 · 45 дней данных · 10 819 измерений",
        verdict:
            "Модель, которая учитывает еду и инсулин, предсказывает сахар на полчаса вперёд точнее, чем прогноз «останется как сейчас». Почти весь выигрыш приходится на время после еды.",
        kpis: [
            ["1,15", "ммоль/л, средняя ошибка прогноза на 30 минут"],
            ["−0,10", "ммоль/л по сравнению с «останется как сейчас»"],
        ],
        bars: {
            caption: "Ошибка прогноза на 30 минут, ммоль/л. Чем меньше, тем лучше",
            rows: [
                ["Как сейчас", 1.25],
                ["По наклону", 1.71],
                ["Формула", 1.22],
                ["Модель", 1.15, true],
            ],
        },
        body: [
            [
                "p",
                "Я сравнил несколько способов предсказать сахар: от самого простого («останется как сейчас») до модели, которая знает про активный инсулин, съеденные углеводы и время суток. Проверял честно: каждый день модель училась только на прошлых днях и предсказывала следующий.",
            ],
            [
                "table",
                ["Способ", "30 мин", "60 мин", "Точнее 1,1"],
                [
                    ["Останется как сейчас", "1,25", "1,91", "72 %"],
                    ["Продолжит по наклону", "1,71", "3,17", "61 %"],
                    ["Формула + еда и инсулин", "1,22", "1,88", "71 %"],
                    ["Модель + еда и инсулин", "1,15", "1,74", "75 %"],
                ],
            ],
            [
                "p",
                "В первых двух столбцах средняя ошибка в ммоль/л. В последнем доля прогнозов, которые ошиблись не больше чем на 1,1 ммоль/л.",
            ],
            [
                "p",
                "После еды модель ошибается меньше на 0,15 ммоль/л на 30 минут вперёд и на 0,23 на 60. Когда еды нет, все способы почти одинаковые, разница не больше 0,03. За последние две недели отрыв вырос: модель становится точнее по мере того, как пополняется журнал.",
            ],
            [
                "p",
                "Гипо модель предсказывать не умеет. В проверке было 102 момента с сахаром ниже 3,9, и она не угадала ни одного. Для этого нужна отдельная модель, которая отвечает только на вопрос «упадёт ли сахар ниже 3,9».",
            ],
        ],
    },
    {
        id: "icr",
        tab: "Углеводный коэффициент",
        title: "Сколько углеводов закрывает единица инсулина",
        meta: "14 сентября 2026 · 57 приёмов еды, 20 подошли для разбора",
        verdict:
            "Постоянного коэффициента у меня сейчас нет. Поджелудочная ещё вырабатывает свой инсулин и закрывает заметную часть еды сама, поэтому размер укола почти не влияет на то, как растёт сахар.",
        kpis: [
            ["15–20", "г на единицу, ориентир для еды от 60 г"],
            ["+0,1", "ммоль/л за 4 часа после 25–40 г без укола"],
        ],
        bars: {
            caption: "На сколько вырос сахар за 4 часа после еды, медиана, ммоль/л",
            rows: [
                ["Много инсулина", 1.7],
                ["Мало инсулина", 1.9],
                ["Без укола", 0.1, true],
            ],
        },
        body: [
            [
                "p",
                "С 4 сентября я стал колоть меньше: на те же углеводы 3–10 единиц вместо 15–25. Получился готовый эксперимент, в котором видно, что бывает при разных дозах.",
            ],
            [
                "table",
                ["Доза", "Приёмов", "Рост за 4 ч"],
                [
                    ["Много: до 10 г на единицу", "6", "+1,7"],
                    ["Мало: 13–24 г на единицу", "7", "+1,9"],
                    ["Без укола, 25–40 г", "3", "+0,1"],
                ],
            ],
            [
                "p",
                "Когда я колол мало, сахар рос не сильнее, чем когда колол много. А еда без укола дала самую ровную кривую. Похоже, бета-клетки ещё подстраиваются сами и сглаживают и лишний инсулин, и недостающий. Когда своя выработка угаснет, коэффициент придётся считать заново.",
            ],
        ],
    },
];

// Ссылка вида …/#research открывает окно сразу.
const HASH = "#research";

function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text != null) element.textContent = text;
    return element;
}

function kpi([value, label]) {
    const box = node("div", "research__kpi");
    box.append(node("span", "research__kpi-value", value), node("span", "research__kpi-label", label));
    return box;
}

function bars({ caption, rows }) {
    const box = node("figure", "research__bars");
    const max = Math.max(...rows.map(([, value]) => value));
    for (const [label, value, best] of rows) {
        const row = node("div", best ? "research__bar research__bar--best" : "research__bar");
        const track = node("span", "research__track");
        const fill = node("span", "research__fill");
        fill.style.width = `${(100 * value) / max}%`;
        track.append(fill);
        track.setAttribute("aria-hidden", "true");
        row.append(
            node("span", "research__bar-label", label),
            track,
            node("span", "research__bar-value", String(value).replace(".", ","))
        );
        box.append(row);
    }
    box.append(node("figcaption", "research__caption", caption));
    return box;
}

function block([kind, ...rest]) {
    if (kind === "p") return node("p", "research__text", rest[0]);

    const [head, rows] = rest;
    const wrap = node("div", "research__table-wrap");
    const table = node("table", "research__table");
    const thead = node("thead");
    const headRow = node("tr");
    for (const title of head) {
        const cell = node("th", null, title);
        cell.scope = "col";
        headRow.append(cell);
    }
    thead.append(headRow);
    const tbody = node("tbody");
    for (const cells of rows) {
        const tr = node("tr");
        for (const text of cells) tr.append(node("td", null, text));
        tbody.append(tr);
    }
    table.append(thead, tbody);
    wrap.append(table);
    return wrap;
}

function study(item) {
    const article = node("article", "research__study");
    const kpis = node("div", "research__kpis");
    kpis.append(...item.kpis.map(kpi));
    const summary = node("div", "research__summary");
    summary.append(kpis, bars(item.bars));
    article.append(
        node("p", "research__meta", item.meta),
        node("h3", "research__title", item.title),
        node("p", "research__verdict", item.verdict),
        summary,
        ...item.body.map(block)
    );
    return article;
}

export function initResearch() {
    const dialog = document.getElementById("research");
    const opener = document.getElementById("research-open");
    const tabs = document.getElementById("research-tabs");
    const panel = document.getElementById("research-panel");
    if (!dialog || !opener) return;

    const buttons = STUDIES.map((item, index) => {
        const button = node("button", "research__tab", item.tab);
        button.type = "button";
        button.setAttribute("role", "tab");
        button.setAttribute("aria-controls", "research-panel");
        button.addEventListener("click", () => select(index));
        return button;
    });
    tabs.replaceChildren(...buttons);

    function select(index) {
        buttons.forEach((button, i) => button.setAttribute("aria-selected", String(i === index)));
        panel.replaceChildren(study(STUDIES[index]));
        dialog.scrollTop = 0;
    }

    // Стрелки переключают вкладки, как в любом tablist.
    tabs.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        const current = buttons.findIndex((button) => button.getAttribute("aria-selected") === "true");
        const next = (current + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
        select(next);
        buttons[next].focus();
    });

    function open() {
        select(0);
        if (!dialog.open) dialog.showModal();
        if (location.hash !== HASH) history.replaceState(null, "", HASH);
    }

    opener.addEventListener("click", open);
    document.getElementById("research-close").addEventListener("click", () => dialog.close());
    // Клик по подложке — мимо окна — закрывает его.
    dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("close", () => {
        if (location.hash === HASH) history.replaceState(null, "", location.pathname + location.search);
    });

    if (location.hash === HASH) open();
}
