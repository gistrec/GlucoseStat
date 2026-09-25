/* Два состояния. Системную тему кнопка не предлагает — она лишь берётся при
   первом заходе, пока выбор не сделан. */
export const THEMES = [
    { id: "light", glyph: "☀", label: "Светлая", next: "тёмную" },
    { id: "dark", glyph: "☾", label: "Тёмная", next: "светлую" },
];

// Те же значения, что у --bg в style.css: сюда попадает цвет панели Safari.
export const THEME_BG = { light: "#f4f5f9", dark: "#07070b" };

export function systemPrefersDark() {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/* Хранилище недоступно в приватном режиме Safari, и обращение к нему там
   бросает исключение. Тема — не та вещь, ради которой страница вправе не
   открыться, поэтому оба обращения обёрнуты. */
export function storedTheme() {
    try {
        const saved = localStorage.getItem("theme");
        return THEMES.some((item) => item.id === saved) ? saved : null;
    } catch (error) {
        return null;
    }
}

/* Пока выбор не сделан, страница открывается в системной теме: попасть на
   белый экран ночью только потому, что настройка ещё не тронута, — плохое
   первое впечатление. После первого нажатия решает кнопка. */
export function initialTheme() {
    return storedTheme() || (systemPrefersDark() ? "dark" : "light");
}

export function rememberTheme(id) {
    try {
        localStorage.setItem("theme", id);
    } catch (error) {
        /* Забудется после закрытия вкладки — не повод ломать переключение. */
    }
}
