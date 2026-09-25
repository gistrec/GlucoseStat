/* Дашборд читает единственный статический data.json — ни бэкенда, ни запросов
   к Abbott со стороны браузера. */

import { state, MEALS_PAGE } from "./js/state.js";
import { els } from "./js/dom.js";
import { THEMES, THEME_BG, initialTheme, rememberTheme, storedTheme } from "./js/theme.js";
import { renderNow, renderStats } from "./js/now-stats.js";
import { renderNights } from "./js/nights.js";
import { drawChart, hoverAt, clearHover } from "./js/chart.js";
import { renderReview, overlayHoverAt, clearOverlayHover } from "./js/meals.js";
import { TIMEZONE, formatAgo, formatDateTime } from "./js/format.js";

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
