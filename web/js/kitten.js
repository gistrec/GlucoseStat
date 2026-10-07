/* Котёнок у нижнего края экрана. Ведёт себя по текущему сахару: в норме
   гуляет, выше середины цели ходит медленнее и чаще присаживается, выше
   нормы в основном лежит, в гипо носится, а когда сенсор молчит — сидит.
   Зону берёт у той же zone(), что красит цифру в шапке.

   Он не должен ни мешать, ни отвлекать: касания проходят сквозь него,
   реплик над головой нет, окрас серый — о сахаре говорит цифра, котёнок её
   только сопровождает. Спрятать его можно кнопкой в подвале; выбор хранится
   в localStorage, как тема и свёрнутые разделы.

   Рисунок — набор «Cat sprites» от Shepardskin, CC0:
   https://opengameart.org/content/cat-sprites. web/cat.gif лежит как есть,
   серым его делает перекраска палитры при загрузке. Лист нерегулярный:
   строки и кадры находятся по пустым полосам между ними. */

import { state } from "./state.js";
import { zone } from "./series.js";
import { STALE_AFTER_MS } from "./now-stats.js";

const STORAGE_KEY = "kitten";
/* Увеличение пикселей: ×3 на десктопе, ×2 на телефоне — там котёнок
   крупного размера закрывал бы заметную полосу текста при прокрутке. */
const narrowScreen = window.matchMedia("(max-width: 500px)");

/* Спрайту хватает 9–14 кадров в секунду. Цикл просыпается на каждом кадре
   экрана, но рисует не чаще 20 раз в секунду: на телефоне перерисовка на
   60–120 Гц ради такой анимации тратила бы батарею впустую. */
const STEP_MS = 50;

// Палитра исходника → серый, который не спорит ни с одной из тем.
const RECOLOR = {
    "56,56,56": [128, 133, 146],
    "28,28,28": [86, 90, 102],
    "143,52,160": [196, 142, 152],
    "95,160,48": [170, 160, 90],
};

/* Действие: [имя, вес, мин. секунд, макс. секунд]. walk — пиксели в секунду. */
const MOODS = {
    "in-range": { walk: 30, acts: [["walk", 5, 4, 9], ["sit", 3, 3, 6], ["lie", 1, 4, 8]] },
    hyper: { walk: 18, acts: [["walk", 3, 3, 6], ["sit", 4, 4, 8], ["lie", 2, 5, 10]] },
    high: { walk: 10, acts: [["lie", 6, 8, 16], ["walk", 1, 2, 4]] },
    hypo: { walk: 110, acts: [["run", 1, 2, 5]] },
    idle: { walk: 12, acts: [["sit", 3, 5, 10], ["lie", 2, 6, 12], ["walk", 1, 2, 4]] },
};

// Кадров в секунду; у ходьбы и бега — при скорости, под которую рисовали.
const FPS = { sit: 3, lie: 1, walk: 9, run: 14 };
const NATIVE_SPEED = { walk: 30, run: 110 };

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

let canvas = null;
let ctx = null;
let sheet = null;
let frames = null;
let toggle = null;

let mood = "idle";
let act = "sit";
let t = 0;
let until = 0;
let x = 24;
let dir = 1;
let last = 0;
let running = false;

function readHidden() {
    try {
        return localStorage.getItem(STORAGE_KEY) === "hidden";
    } catch (error) {
        return false;
    }
}

function saveHidden(hidden) {
    try {
        if (hidden) localStorage.setItem(STORAGE_KEY, "hidden");
        else localStorage.removeItem(STORAGE_KEY);
    } catch (error) {
        /* Забудется после закрытия вкладки — не повод ломать кнопку. */
    }
}

function prepare(img) {
    const c = document.createElement("canvas");
    c.width = img.width;
    c.height = img.height;
    const g = c.getContext("2d");
    g.drawImage(img, 0, 0);
    const data = g.getImageData(0, 0, c.width, c.height);
    const px = data.data;
    const [br, bg, bb] = [px[0], px[1], px[2]];
    for (let i = 0; i < px.length; i += 4) {
        if (px[i] === br && px[i + 1] === bg && px[i + 2] === bb) {
            px[i + 3] = 0;
            continue;
        }
        const swap = RECOLOR[`${px[i]},${px[i + 1]},${px[i + 2]}`];
        if (swap) [px[i], px[i + 1], px[i + 2]] = swap;
    }
    g.putImageData(data, 0, 0);
    return { canvas: c, px };
}

/* Строки — полосы непустых пикселей сверху вниз, кадры в строке — полосы
   непустых столбцов. Возвращает рамки кадров по строкам и общий размер. */
function slice(width, height, px) {
    const solid = (x, y) => px[(y * width + x) * 4 + 3] > 0;
    const runs = (length, isSolid) => {
        const found = [];
        let start = -1;
        for (let i = 0; i <= length; i++) {
            const any = i < length && isSolid(i);
            if (any && start < 0) start = i;
            if (!any && start >= 0) {
                found.push([start, i]);
                start = -1;
            }
        }
        return found;
    };

    let w = 0;
    let h = 0;
    const rows = runs(height, (y) => {
        for (let x = 0; x < width; x++) if (solid(x, y)) return true;
        return false;
    }).map(([y0, y1]) => {
        h = Math.max(h, y1 - y0);
        return runs(width, (x) => {
            for (let y = y0; y < y1; y++) if (solid(x, y)) return true;
            return false;
        }).map(([x0, x1]) => {
            w = Math.max(w, x1 - x0);
            return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
        });
    });
    return { rows, w, h };
}

function currentMood() {
    const latest = state.snapshot && state.snapshot.latest;
    if (!latest) return "idle";
    if (Date.now() - latest.t * 1000 > STALE_AFTER_MS) return "idle";
    return zone(latest.mgdl);
}

function pick() {
    const options = MOODS[mood].acts;
    const total = options.reduce((sum, option) => sum + option[1], 0);
    let roll = Math.random() * total;
    let chosen = options[0];
    for (const option of options) {
        roll -= option[1];
        if (roll <= 0) {
            chosen = option;
            break;
        }
    }

    act = chosen[0];
    // Без перемещения шаги выглядели бы бегом на месте.
    if (reducedMotion.matches && (act === "walk" || act === "run")) act = "sit";
    if ((act === "walk" || act === "run") && Math.random() < 0.4) dir = -dir;
    t = 0;
    until = chosen[2] + Math.random() * (chosen[3] - chosen[2]);
}

function draw(list, rate) {
    const box = list[Math.floor(t * rate) % list.length];
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    // По центру и по дну: кадры разной ширины не дёргают котёнка.
    ctx.drawImage(sheet, box.x, box.y, box.w, box.h,
        Math.floor((canvas.width - box.w) / 2), canvas.height - box.h, box.w, box.h);
}

function frame(now) {
    if (!running) return;
    if (last && now - last < STEP_MS) {
        requestAnimationFrame(frame);
        return;
    }
    const dt = last ? Math.min(0.1, (now - last) / 1000) : 0;
    last = now;
    t += dt;
    if (t >= until) pick();

    const max = Math.max(0, window.innerWidth - canvas.offsetWidth);
    let rate = FPS[act];
    if (act === "walk" || act === "run") {
        const speed = MOODS[mood].walk;
        x += dir * speed * dt;
        if (x <= 0) {
            x = 0;
            dir = 1;
        } else if (x >= max) {
            x = max;
            dir = -1;
        }
        // Шаг подстраивается под скорость, чтобы лапы не скользили.
        rate *= Math.max(0.4, speed / NATIVE_SPEED[act]);
    }
    x = Math.min(x, max);

    draw(frames[act], rate);
    canvas.style.translate = `${Math.round(x)}px 0`;
    // В исходнике котёнок смотрит влево.
    canvas.classList.toggle("kitten--right", dir > 0);
    requestAnimationFrame(frame);
}

function start() {
    if (running || !frames || canvas.hidden) return;
    running = true;
    last = 0;
    requestAnimationFrame(frame);
}

function setHidden(hidden) {
    canvas.hidden = hidden;
    document.body.classList.toggle("has-kitten", !hidden);
    toggle.textContent = hidden ? "Позвать котёнка" : "Спрятать котёнка";
    if (hidden) running = false;
    else start();
}

/* Настроение по последнему снимку. Зовётся из render() раз в минуту; смена
   зоны прерывает текущее действие, а та же зона его не трогает. */
export function renderKitten() {
    const next = currentMood();
    if (next === mood) return;
    mood = next;
    until = 0;
}

export function initKitten() {
    canvas = document.createElement("canvas");
    canvas.className = "kitten";
    canvas.setAttribute("aria-hidden", "true");
    canvas.hidden = true;
    document.body.append(canvas);

    toggle = document.getElementById("kitten-toggle");
    toggle.addEventListener("click", () => {
        const hidden = !canvas.hidden;
        saveHidden(hidden);
        setHidden(hidden);
    });

    const img = new Image();
    img.onload = () => {
        const prepared = prepare(img);
        sheet = prepared.canvas;
        const { rows, w, h } = slice(img.width, img.height, prepared.px);
        const [stand, walk, run] = rows;
        frames = {
            sit: stand.slice(0, 3),
            lie: [stand[4] || stand[3]],
            walk,
            run,
        };
        canvas.width = w;
        canvas.height = h;
        const resize = () => {
            const scale = narrowScreen.matches ? 2 : 3;
            canvas.style.width = `${w * scale}px`;
            canvas.style.height = `${h * scale}px`;
        };
        resize();
        narrowScreen.addEventListener("change", resize);
        ctx = canvas.getContext("2d");
        toggle.hidden = false;
        setHidden(readHidden());
    };
    // Без рисунка котёнка просто нет — и кнопки для него тоже.
    img.onerror = () => console.warn("Котёнок не загрузился: нет web/cat.gif");
    img.src = "cat.gif";
}
