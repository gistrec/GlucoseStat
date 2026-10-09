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
        title: "Прогноз глюкозы на своих данных",
        meta: "6 октября 2026 · 45 дней · 10 819 проверочных точек",
        verdict:
            "Бустинг с контекстом еды и инсулина — первая модель, заметно обошедшая «сахар не изменится». Весь выигрыш — после еды.",
        kpis: [
            ["1,15", "ммоль/л — ошибка на 30 минут"],
            ["−0,10", "к наивному прогнозу"],
        ],
        bars: {
            caption: "Ошибка на 30 минут, ммоль/л (меньше — лучше)",
            rows: [
                ["Без изменений", 1.25],
                ["Тренд", 1.71],
                ["Ridge", 1.22],
                ["Бустинг", 1.15, true],
            ],
        },
        body: [
            [
                "p",
                "Лесенка моделей от «глюкоза не изменится» до бустинга с контекстом: активный инсулин, активные углеводы, час суток. Проверка walk-forward: каждый день модель учится заново на всех предыдущих.",
            ],
            [
                "table",
                ["Модель", "30 мин", "60 мин", "В ±1,1"],
                [
                    ["Без изменений", "1,25", "1,91", "72 %"],
                    ["Тренд", "1,71", "3,17", "61 %"],
                    ["Ridge + контекст", "1,22", "1,88", "71 %"],
                    ["Бустинг + контекст", "1,15", "1,74", "75 %"],
                ],
            ],
            [
                "p",
                "После еды бустинг снимает 0,15 ммоль/л на 30 минут и 0,23 на 60; без еды все модели в пределах 0,03 друг от друга. На последних двух неделях разрыв больше: качество растёт с журналом.",
            ],
            [
                "p",
                "Гипогликемию точечный прогноз не ловит: из 102 строк ниже 3,9 бустинг не угадал ни одной. Это отдельная задача на классификатор.",
            ],
        ],
    },
    {
        id: "icr",
        tab: "Углеводный коэффициент",
        title: "Углеводный коэффициент по журналу",
        meta: "14 сентября 2026 · 57 приёмов, 20 чистых событий",
        verdict:
            "Фиксированного УК сейчас нет: остаточная секреция закрывает значительную часть еды сама. Доза почти не влияет на исход.",
        kpis: [
            ["15–20", "г/ед — ориентир для приёмов от 60 г"],
            ["+0,1", "ммоль/л за 4 ч без болюса, 25–40 г"],
        ],
        bars: {
            caption: "Подъём за 4 часа, медиана, ммоль/л",
            rows: [
                ["Щедро", 1.7],
                ["Скупо", 1.9],
                ["Без укола", 0.1, true],
            ],
        },
        body: [
            [
                "p",
                "Период намеренного недокола — с 4 сентября болюсы упали с 15–25 до 3–10 единиц при тех же углеводах — сработал как естественный эксперимент.",
            ],
            [
                "table",
                ["Дозировка", "Событий", "Подъём за 4 ч"],
                [
                    ["Щедро, до 10 г/ед", "6", "+1,7"],
                    ["Скупо, 13–24 г/ед", "7", "+1,9"],
                    ["Без болюса, 25–40 г", "3", "+0,1"],
                ],
            ],
            [
                "p",
                "Бета-клетки ещё дозируют сами и сглаживают вмешательство в обе стороны. Любой посчитанный сейчас коэффициент устареет вместе с угасанием секреции — пересчитывать.",
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
        panel.scrollTop = 0;
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
