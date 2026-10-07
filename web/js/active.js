import { state } from "./state.js";
import { els } from "./dom.js";
import { TZ_MINUTES, formatAmount, formatSpan, plural } from "./format.js";

/* Плашка «Активный инсулин и углеводы»: сколько короткого ещё работает и
   сколько съеденного ещё усваивается. Числа считает сборщик (active.py) по
   тем же кривым, по которым IOB и COB получает модель прогноза, — здесь
   только подписи к ним.

   Половина без остатка не рисуется: «0 г ещё усваивается» в три часа ночи —
   место, занятое ради нуля. Нет обеих — нет и плашки. */
export function renderActive() {
    const active = state.snapshot.active || {};
    const halves = [
        active.insulin && insulinHalf(active.insulin),
        active.carbs && carbsHalf(active.carbs),
    ].filter(Boolean);

    els.active.replaceChildren(...halves);
    els.active.hidden = halves.length === 0;
}

function insulinHalf(insulin) {
    const now = Date.now();
    const since = formatSpan((now - insulin.last * 1000) / 1000);
    // При одном уколе «из 5 ед, 2 ч 18 мин» читается верно. При нескольких то
    // же время рядом с суммой выглядело бы как «7 ед вкололи 20 мин назад»,
    // а это лишь последний из трёх.
    const count = insulin.count || 1;
    const aside =
        count === 1
            ? `из ${formatAmount(insulin.of)} ед, ${since}`
            : `из ${formatAmount(insulin.of)} ед за ${count} ${plural(count, "укол", "укола", "уколов")}, последний ${since} назад`;
    const peak = insulin.peak * 1000 - now;
    // Пик последнего укола: до него сахар ещё не видел полной силы дозы.
    // Временем, а не одним «прошёл»: по часам его сверяют с кривой.
    const peakText =
        peak > 0
            ? `пик в ${clock(insulin.peak)}, через ${formatSpan(peak / 1000)}`
            : `пик был в ${clock(insulin.peak)}`;

    return half({
        token: "--insulin",
        title: "Активный инсулин",
        aside,
        value: formatAmount(insulin.left),
        unit: "ед ещё работает",
        share: insulin.left / insulin.of,
        hint: `Действие до ${clock(insulin.until)}, ${peakText}`,
    });
}

function carbsHalf(carbs) {
    return half({
        token: "--meal",
        title: "Активные углеводы",
        aside: `из ${formatAmount(carbs.of)} г`,
        // Целыми граммами: десятые доли у оценки по линейной кривой — шум.
        value: String(Math.round(carbs.left)),
        unit: "г ещё усваивается",
        share: carbs.left / carbs.of,
        hint: `Усвоение до ${clock(carbs.until)}`,
    });
}

function clock(seconds) {
    return TZ_MINUTES.format(new Date(seconds * 1000));
}

function half({ token, title, aside, value, unit, share, hint }) {
    const root = document.createElement("div");
    root.className = "active__half";

    const label = document.createElement("p");
    label.className = "active__label";
    const name = document.createElement("span");
    name.textContent = title;
    const side = document.createElement("span");
    side.className = "active__aside";
    side.textContent = aside;
    label.append(name, side);

    const reading = document.createElement("p");
    reading.className = "active__reading";
    const number = document.createElement("span");
    number.className = "active__value";
    number.style.color = `var(${token})`;
    number.textContent = value;
    const words = document.createElement("span");
    words.className = "active__unit";
    words.textContent = unit;
    reading.append(number, words);

    const track = document.createElement("div");
    track.className = "active__track";
    track.style.setProperty("--tone", `var(${token})`);
    const fill = document.createElement("div");
    fill.className = "active__fill";
    fill.style.width = `${Math.round(Math.min(1, Math.max(0, share)) * 100)}%`;
    track.append(fill);

    const note = document.createElement("p");
    note.className = "active__hint";
    note.textContent = hint;

    root.append(label, reading, track, note);
    return root;
}
