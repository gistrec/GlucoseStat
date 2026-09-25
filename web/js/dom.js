export const els = {
    now: document.getElementById("now"),
    nowValue: document.getElementById("now-value"),
    nowArrow: document.getElementById("now-arrow"),
    nowMeta: document.getElementById("now-meta"),
    nowEvents: document.getElementById("now-events"),
    empty: document.getElementById("empty"),
    ranges: document.getElementById("ranges"),
    chart: document.getElementById("chart"),
    chartEmpty: document.getElementById("chart-empty"),
    canvas: document.getElementById("canvas"),
    stats: document.getElementById("stats"),
    night: document.getElementById("night"),
    nightNote: document.getElementById("night-note"),
    nightStats: document.getElementById("night-stats"),
    nightStrip: document.getElementById("night-strip"),
    legend: document.getElementById("legend"),
    tip: document.getElementById("tip"),
    review: document.getElementById("review"),
    reviewNote: document.getElementById("review-note"),
    reviewStats: document.getElementById("review-stats"),
    mealsMore: document.getElementById("meals-more"),
    ratio: document.getElementById("ratio"),
    ratioNote: document.getElementById("ratio-note"),
    ratioTable: document.getElementById("ratio-table"),
    ratioSizeTable: document.getElementById("ratio-size-table"),
    ratioSizeWrap: document.getElementById("ratio-size-wrap"),
    reviewPanel: document.getElementById("review-panel"),
    overlay: document.getElementById("overlay"),
    overlayTip: document.getElementById("overlay-tip"),
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
export function readColor(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return /^(#|rgb|hsl)/.test(value) ? value : fallback;
}

export function readNumber(name, fallback) {
    return Number(getComputedStyle(document.documentElement).getPropertyValue(name)) || fallback;
}
