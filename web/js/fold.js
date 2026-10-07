/* Свёртка второстепенных разделов: сенсор, ночи, разбор приёмов пищи и
   углеводный коэффициент. Текущее значение, график и плитки под ним не
   сворачиваются — ради них страницу и открывают.

   Свёрнутый раздел не исчезает, а сжимается до заголовка и строки-сводки с
   главными числами: «гипо 0 из 6» говорит, всё ли в порядке, и разворачивать
   раздел нужно, только если нет. Сводку пишет рендерер раздела (setFoldSummary)
   — он и так считает эти числа.

   Выбор хранится в localStorage, как часовой пояс и тема: страница
   статическая, сервер его не читает. Хранятся свёрнутые разделы, а не
   развёрнутые: новый раздел, которого в списке ещё нет, по умолчанию открыт. */

const STORAGE_KEY = "folded";

let folded = new Set();
let onOpen = () => {};

function readFolded() {
    try {
        const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
        return new Set(Array.isArray(saved) ? saved : []);
    } catch (error) {
        /* Приватный режим Safari или испорченное значение: всё развёрнуто. */
        return new Set();
    }
}

function saveFolded() {
    try {
        if (folded.size) localStorage.setItem(STORAGE_KEY, JSON.stringify([...folded]));
        else localStorage.removeItem(STORAGE_KEY);
    } catch (error) {
        /* Забудется после закрытия вкладки — не повод ломать свёртку. */
    }
}

function apply(button) {
    const id = button.dataset.fold;
    const isFolded = folded.has(id);
    button.setAttribute("aria-expanded", String(!isFolded));
    document.getElementById(id).classList.toggle("is-folded", isFolded);
}

export function isFolded(id) {
    return folded.has(id);
}

/* opened(id) зовётся при разворачивании: холсты внутри свёрнутого раздела
   рисовались в нулевую ширину, и их надо перерисовать уже видимыми. */
export function initFolds(opened) {
    folded = readFolded();
    onOpen = opened;

    for (const button of document.querySelectorAll("[data-fold]")) {
        apply(button);
        button.addEventListener("click", () => {
            const id = button.dataset.fold;
            if (folded.has(id)) folded.delete(id);
            else folded.add(id);
            saveFolded();
            apply(button);
            if (!folded.has(id)) onOpen(id);
        });
    }
}

export function setFoldSummary(id, text) {
    document.getElementById(`${id}-summary`).textContent = text;
}
