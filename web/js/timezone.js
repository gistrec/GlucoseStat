import { els } from "./dom.js";
import {
    PINNED_ZONES,
    TIMEZONE,
    TIMEZONE_LABEL,
    setTimezone,
    zoneLabel,
    zoneOffset,
    zoneOffsetMinutes,
} from "./format.js";

/* Пояс просмотра: кнопка с часами в шапке и меню под ней.

   По умолчанию «Авто» — пояс устройства. В поездке телефон переходит на
   местное время сам, и страница совпадает с его часами без единого клика.
   Ручной выбор нужен, когда смотрят с чужого устройства или хотят видеть
   белградское время из Новосибирска.

   Выбор хранится в localStorage, а не в куке: страница статическая, сервер
   его не читает. Хранится сам выбор («auto» или имя зоны), а не зона, в
   которую он развернулся: «Авто», сохранённое в Москве, в Новосибирске
   обязано стать Новосибирском. */

const STORAGE_KEY = "timezone";
const AUTO = "auto";

let choice = AUTO;
let onChange = () => {};

function deviceZone() {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function resolve(value) {
    return value === AUTO ? deviceZone() : value;
}

/* Зона из хранилища могла устареть: браузер обновился, и имя исчезло из его
   базы. Такую тихо заменяем на «Авто», а не роняем страницу. */
function valid(zone) {
    try {
        new Intl.DateTimeFormat("ru-RU", { timeZone: zone });
        return true;
    } catch (error) {
        return false;
    }
}

function readChoice() {
    try {
        const saved = localStorage.getItem(STORAGE_KEY);
        if (saved && (saved === AUTO || valid(saved))) return saved;
    } catch (error) {
        /* Приватный режим Safari: выбор не запомнится, страница откроется. */
    }
    return AUTO;
}

function saveChoice(value) {
    try {
        if (value === AUTO) localStorage.removeItem(STORAGE_KEY);
        else localStorage.setItem(STORAGE_KEY, value);
    } catch (error) {
        /* См. readChoice. */
    }
}

const ALL_ZONES = (() => {
    const pinned = new Set(PINNED_ZONES.map(([zone]) => zone));
    const all = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
    // По смещению, затем по городу: так соседи по часам стоят рядом, и
    // «UTC+7» в поиске находит их одним куском.
    return all
        .filter((zone) => !pinned.has(zone))
        .map((zone) => ({ zone, offset: zoneOffsetMinutes(zone), name: zoneLabel(zone) }))
        .sort((a, b) => a.offset - b.offset || a.name.localeCompare(b.name))
        .map((item) => item.zone);
})();

function clockIn(zone) {
    return new Intl.DateTimeFormat("ru-RU", {
        timeZone: zone,
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
    }).format(new Date());
}

export function renderZoneButton() {
    const name = choice === AUTO ? `Авто · ${TIMEZONE_LABEL}` : TIMEZONE_LABEL;

    const label = document.createElement("span");
    label.className = "zone__label";
    label.textContent = name;

    const clock = document.createElement("span");
    clock.className = "zone__clock";
    clock.textContent = clockIn(TIMEZONE);

    els.zone.replaceChildren(label, clock);
    const hint = `Часовой пояс: ${name}, ${zoneOffset(TIMEZONE)}. Сменить`;
    els.zone.setAttribute("aria-label", hint);
    els.zone.title = hint;
}

function option(value, name) {
    const zone = resolve(value);
    const item = document.createElement("button");
    item.type = "button";
    item.className = "zone__option";
    item.dataset.zone = value;
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(value === choice));

    const title = document.createElement("span");
    title.textContent = name;
    const side = document.createElement("span");
    side.className = "zone__offset";
    side.textContent = `${zoneOffset(zone)} · ${clockIn(zone)}`;

    item.append(title, side);
    return item;
}

function group(title) {
    const head = document.createElement("p");
    head.className = "zone__group";
    head.textContent = title;
    return head;
}

function renderList(query) {
    const q = query.trim().toLowerCase();
    const rows = [];

    if (!q) {
        rows.push(group("Устройство"), option(AUTO, `Авто · ${zoneLabel(deviceZone())}`));
        rows.push(group("Часто"), ...PINNED_ZONES.map(([zone, name]) => option(zone, name)));
        rows.push(group(`Все пояса · ${ALL_ZONES.length}`), ...ALL_ZONES.map((zone) => option(zone, zoneLabel(zone))));
    } else {
        // Ищется и русское имя закреплённых, и английское из идентификатора, и
        // смещение: «новос», «Novosibirsk», «UTC+7» находят одно и то же.
        const hits = [...PINNED_ZONES.map(([zone]) => zone), ...ALL_ZONES].filter((zone) =>
            `${zoneLabel(zone)} ${zone} ${zoneOffset(zone)}`.toLowerCase().includes(q)
        );
        if (hits.length) {
            rows.push(...hits.map((zone) => option(zone, zoneLabel(zone))));
        } else {
            const empty = document.createElement("p");
            empty.className = "zone__empty";
            empty.textContent = "Ничего не нашлось. Попробуйте английское имя города или «UTC+7».";
            rows.push(empty);
        }
    }

    els.zoneList.replaceChildren(...rows);
}

function open() {
    els.zoneSearch.value = "";
    renderList("");
    els.zoneMenu.hidden = false;
    els.zone.setAttribute("aria-expanded", "true");
    // Фокус в поиск — только с мышью. На телефоне он поднимал клавиатуру
    // поверх списка, хотя чаще всего нужен тап по одному из трёх городов;
    // поиск остаётся в одном касании.
    if (window.matchMedia("(pointer: fine)").matches) els.zoneSearch.focus();
    // Выбранный пояс в длинном списке — в поле зрения, а не за прокруткой.
    const picked = els.zoneList.querySelector('[aria-selected="true"]');
    if (picked) picked.scrollIntoView({ block: "nearest" });
}

function close(focusButton) {
    if (els.zoneMenu.hidden) return;
    els.zoneMenu.hidden = true;
    els.zone.setAttribute("aria-expanded", "false");
    if (focusButton) els.zone.focus();
}

function pick(value) {
    choice = value;
    saveChoice(value);
    setTimezone(resolve(value));
    renderZoneButton();
    close(true);
    onChange();
}

/* Зона ставится до первой отрисовки: иначе страница мелькнула бы белградским
   временем и тут же перерисовалась бы своим. */
export function initZonePicker(callback) {
    onChange = callback;
    choice = readChoice();
    setTimezone(resolve(choice));
    renderZoneButton();

    els.zone.addEventListener("click", () => (els.zoneMenu.hidden ? open() : close(false)));
    els.zoneSearch.addEventListener("input", () => renderList(els.zoneSearch.value));
    els.zoneList.addEventListener("click", (event) => {
        const item = event.target.closest("[data-zone]");
        if (item) pick(item.dataset.zone);
    });
    // Enter в поиске берёт первую найденную строку — набрал «новос», нажал Enter.
    els.zoneSearch.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        const first = els.zoneList.querySelector("[data-zone]");
        if (first) pick(first.dataset.zone);
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") close(true);
    });
    document.addEventListener("click", (event) => {
        if (!event.target.closest(".zone")) close(false);
    });
}
