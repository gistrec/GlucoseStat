import { state } from "./state.js";
import { els, readColor, readNumber } from "./dom.js";
import { zoneColor, readingColor } from "./series.js";
import { formatMmol, formatDelta } from "./format.js";
import { statCard } from "./now-stats.js";

/* Ночь — единственная часть суток, которую человек не видит: днём низкий сахар
   замечают по себе, в три часа его замечает только сенсор. Числа приходят из
   nights.py посчитанными по сырым замерам — на прореженной кривой выше провал
   в пару минут усредняется корзиной и попросту исчезает.

   Знаменателей два, и каждый назван вслух: гипогликемии и минимум — по всем
   ночам, где сенсор отвечал, дрейф — только по ночам, где ужин успел
   отработать. Сложить их в один было бы удобнее и неверно: отфильтровав ночи
   с поздним ужином из счёта гипогликемий, страница выбросила бы как раз те
   ночи, ради которых счёт ведётся. */

const NIGHT_SPARK_HEIGHT = 44;

// Запас по краям ночной шкалы, мг/дл. Кривая, прижатая к рамке, читается как
// обрезанная: непонятно, где кончился провал и кончился ли.
const NIGHT_SPARK_PAD = 10;

export function renderNights() {
    const nights = state.snapshot.nights;
    if (!nights) {
        els.night.hidden = true;
        return;
    }

    els.nightNote.textContent =
        `${nightHour(nights.from)}–${nightHour(nights.to)} по каждой ночи за последнюю неделю. ` +
        `Гипогликемии и минимум — по всем ночам, где сенсор отвечал; дрейф — по ночам, ` +
        `где к ${nightHour(nights.drift_from)} ужин успел отработать.`;

    els.nightStats.replaceChildren(...nightCards(nights));
    els.night.hidden = false;

    // Строка ночей — после снятия hidden: у скрытой секции clientWidth равен
    // нулю, и холсты вышли бы нулевой ширины.
    renderNightStrip(nights);
}

function nightCards(nights) {
    /* Минуты рядом со счётом ночей: флаг отвечает «случалось ли», а решение
       требует «сколько». Минута под порогом и полчаса под ним — разные ночи, и
       одинаковое «1 из 7» у них читалось бы как одна и та же неделя. */
    const cards = [
        statCard(
            "Ночей с гипогликемией",
            `${nights.hypo_nights} из ${nights.counted}`,
            nights.hypo_minutes
                ? `ниже ${formatMmol(nights.hypo_mgdl)} ммоль/л · всего ${nights.hypo_minutes} мин за неделю`
                : `ниже ${formatMmol(nights.hypo_mgdl)} ммоль/л хотя бы раз за ночь`
        ),
    ];

    if (nights.min_median !== null) {
        cards.push(
            statCard(
                "Минимум за ночь, медиана",
                formatMmol(nights.min_median),
                `ммоль/л, по ${nights.counted} ночам`
            )
        );
    }

    /* Прочерк, а не пропуск карточки: у соседних медиан свой знаменатель, и
       исчезнувший дрейф читался бы как «дрейфа не было», а не как «ночей
       натощак не набралось». Причину называет подпись — тем же манером, каким
       строка сравнения периодов объясняет своё отсутствие. */
    const span = `${nightHour(nights.drift_from)} → ${nightHour(nights.to)}`;
    cards.push(
        nights.drift_median === null
            ? statCard(
                  `Дрейф ${span}`,
                  "—",
                  `ночей натощак слишком мало: ${nights.clean} из ${nights.counted}`
              )
            : statCard(
                  `Дрейф ${span}, медиана`,
                  formatDelta(nights.drift_median),
                  `ммоль/л, по ${nights.clean} ночам натощак`
              )
    );

    return cards;
}

// Час местного времени двумя знаками: «6:00» рядом с «00:00» читается как
// другой формат, а не как другой час.
function nightHour(hour) {
    return `${hour.toString().padStart(2, "0")}:00`;
}

function renderNightStrip(nights) {
    // Шкала общая на все ночи: у каждой своя они сравнивались бы формой, а не
    // высотой, и ночь на восьми выглядела бы копией ночи на четырёх.
    const values = nights.nights.flatMap((night) =>
        (night.points || []).map(([, mgdl]) => mgdl)
    );
    if (!values.length) {
        els.nightStrip.replaceChildren();
        return;
    }

    // Порог гипогликемии всегда в кадре: линия, ушедшая под него, обязана быть
    // видимой как пересечение, а не как касание нижней рамки.
    const scale = {
        min: Math.min(...values, state.snapshot.target.low) - NIGHT_SPARK_PAD,
        max: Math.max(...values, state.snapshot.target.low) + NIGHT_SPARK_PAD,
    };

    els.nightStrip.replaceChildren(
        ...nights.nights.map((night) => nightCell(night, scale))
    );

    // Холсты рисуются после вставки: до неё у них нет ширины.
    for (const [index, night] of nights.nights.entries()) {
        const canvas = els.nightStrip.children[index].querySelector("canvas");
        if (canvas) drawNightSpark(canvas, night, scale);
    }
}

function nightCell(night, scale) {
    const item = document.createElement("li");
    item.className = night.count ? "nights__item" : "nights__item nights__item--empty";
    item.setAttribute("role", "listitem");

    if (night.count) {
        const canvas = document.createElement("canvas");
        canvas.className = "nights__spark";
        canvas.setAttribute("role", "img");
        canvas.setAttribute(
            "aria-label",
            `Ночь ${nightLabel(night)}: минимум ${formatMmol(night.min)} ммоль/л` +
                (night.low_minutes
                    ? `, ${night.low_minutes} мин ниже ${formatMmol(state.snapshot.target.low)}`
                    : "")
        );
        item.append(canvas);
    } else {
        const blank = document.createElement("p");
        blank.className = "nights__blank";
        blank.textContent = "сенсор молчал";
        item.append(blank);
    }

    const date = document.createElement("p");
    date.className = "nights__date";
    date.textContent = nightLabel(night);

    const low = document.createElement("p");
    low.className = "nights__min";
    low.textContent = night.count ? formatMmol(night.min) : "—";
    if (night.count) low.style.color = zoneColor(night.min);

    item.append(date, low);

    // Длительность — только у ночей, где под порогом побывали: у остальных эта
    // строка была бы нулём, который ничего не сообщает, но занимает место под
    // каждой из семи ячеек.
    if (night.low_minutes) {
        const minutes = document.createElement("p");
        minutes.className = "nights__below";
        // Коротко — «ниже» без «нормы»: ячейка шириной в сотню пикселей, а
        // полная фраза рвала бы строку. Чего именно ниже, сказано рядом: тем
        // же порогом подписана плитка слева, и красное число прямо над этой
        // строкой — тот самый минимум. Полностью фраза живёт в подсказке и в
        // метке для скринридера, где место есть.
        minutes.textContent = `${night.low_minutes} мин ниже`;
        minutes.title = `${night.low_minutes} мин ниже ${formatMmol(state.snapshot.target.low)} ммоль/л`;
        item.append(minutes);
    }

    return item;
}

/* Дата ночи — та, в чьё утро она перешла: ночь с 13-го на 14-е подписана 14.09,
   как её и называет сводка. Метка берётся из ISO-даты, а не из new Date(start):
   start — это полночь в зоне сборщика, и в браузере с другой зоной она
   превратилась бы во вчерашний вечер. */
function nightLabel(night) {
    const [, month, day] = night.date.split("-");
    return `${day}.${month}`;
}

function drawNightSpark(canvas, night, scale) {
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = NIGHT_SPARK_HEIGHT;
    if (!width) return;

    canvas.width = width * ratio;
    canvas.height = height * ratio;

    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const span = night.end - night.start;
    const range = scale.max - scale.min;
    const x = (seconds) => ((seconds - night.start) / span) * width;
    const y = (mgdl) => height - ((mgdl - scale.min) / range) * height;

    // Полоса нормы подложкой — та же, что под большим графиком: без неё
    // маленькая кривая не говорит, высоко она идёт или низко.
    ctx.fillStyle = readColor("--in-range", "#7efcb0");
    ctx.globalAlpha = readNumber("--band-alpha", 0.07);
    const top = y(Math.min(scale.max, state.snapshot.target.high));
    ctx.fillRect(0, top, width, y(Math.max(scale.min, state.snapshot.target.low)) - top);
    ctx.globalAlpha = 1;

    ctx.beginPath();
    for (const [index, [seconds, mgdl]] of night.points.entries()) {
        const px = x(seconds);
        const py = y(mgdl);
        if (index === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
    }
    // Цвет ночи — по её минимуму: строка читается сверху вниз одним взглядом,
    // и красная линия среди зелёных называет ночь, о которой речь.
    ctx.strokeStyle = readingColor(night.min);
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.stroke();
}
