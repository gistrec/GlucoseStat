import { state } from "./state.js";
import { els } from "./dom.js";
import { formatMmol, formatShortDay, formatSpan, percent, plural } from "./format.js";
import { setFoldSummary } from "./fold.js";

/* Карточка сенсора: сколько ему осталось и сколько данных от него дошло.

   Две половины отвечают на два вопроса одного дня. Левая — «когда заказывать
   следующий»: срок сенсора кончается на двух-трёх неделях, и узнать об этом
   от приложения в момент, когда сенсор уже замолчал, значит остаться без
   данных на те сутки, пока едет новый. Правая — «можно ли верить числам
   выше»: доля времени в целевом диапазоне, посчитанная по неделе с дырой в
   сутки, выглядит ровно так же солидно, как честная.

   Обе половины — про измерение, а не про сахар, поэтому стоят отдельной
   секцией, а не плиткой среди статистики: там все числа отвечают на «что с
   глюкозой», и «осталось 5 дней» читалось бы как ещё одно такое число.

   Модель прибора карточка не называет. Названия в ответе Abbott нет — есть
   номер модели, и перевести его в слова можно только таблицей, которой у нас
   пока нет (SENSOR_MODELS в librelinkup.py). Зашитое же название однажды
   устаревает молча: страница две недели звала Libre 3 Plus просто Libre 3 и
   обрывала его срок на сутки раньше. Модель названа в шапке страницы, где её
   пишет человек и где видно, что это его слова, а не данные. */

// За сколько дней до конца срок перестаёт быть справкой и становится
// предупреждением. Сутки: столько едет замена, если заказать сегодня.
const WARN_DAYS = 1;

export function renderSensor() {
    const sensor = state.snapshot.sensor;
    renderBias(sensor && sensor.bias);
    if (!sensor) {
        els.sensor.hidden = true;
        return;
    }

    els.sensorCard.replaceChildren(lifeHalf(sensor), dataHalf(sensor));
    setFoldSummary("sensor", sensorSummary(sensor));
    els.sensor.hidden = false;
}

/* Плашка над текущим значением: сенсор устойчиво расходится с глюкометром.
   Над числом, а не в карточке сенсора: читать её нужно ровно тогда, когда
   смотришь на значение, а карточка внизу страницы и бывает свёрнута. Само
   значение не исправляется — смещение посчитано по горстке пар и на высоком
   сахаре бывает другим, поэтому страница предупреждает, а не подменяет.
   Число и порог появления считает publish.py (_bias): нет bias — нет плашки. */
function renderBias(bias) {
    if (!bias) {
        els.bias.hidden = true;
        return;
    }

    const low = bias.mgdl < 0;
    els.biasTitle.textContent = `⚠ Сенсор ${low ? "занижает" : "завышает"} показания`;
    els.biasText.textContent =
        `В среднем на ${formatMmol(Math.abs(bias.mgdl))} ммоль/л ${low ? "ниже" : "выше"} глюкометра ` +
        `по ${bias.pairs} ${plural(bias.pairs, "измерению", "измерениям", "измерениям")}. ` +
        (low
            ? "Низкие значения стоит проверять глюкометром."
            : "Высокие значения стоит проверять глюкометром.");
    els.bias.hidden = false;
}

/* Сводка свёрнутого раздела: сколько осталось и сколько данных дошло — те же
   два ответа, что у половин карточки, одной строкой. */
function sensorSummary(sensor) {
    const data = `данные ${percent(sensor.coverage)}`;
    if (!sensor.ends) return data;

    const leftMs = sensor.ends * 1000 - Date.now();
    const days = Math.floor(leftMs / 86400000);
    const left =
        leftMs <= 0
            ? "срок вышел"
            : days < 1
              ? `осталось ${formatSpan(leftMs / 1000)}`
              : `осталось ${days} ${plural(days, "день", "дня", "дней")}`;
    return `${left} · ${data}`;
}

/* Левая половина: срок. Дни считаются по часам браузера, а не приходят из
   снимка готовым числом: снимок перестраивается раз в минуту, но между двумя
   перестройками страница живёт сама, и «осталось 0 дней», посчитанное вчера,
   к утру было бы просто неправдой. */
function lifeHalf(sensor) {
    const half = document.createElement("div");
    half.className = "sensor__half";

    if (!sensor.started || !sensor.ends) {
        // Дату установки знает только Abbott, и присылает он её не всегда.
        // Пустая половина честнее выдуманной: полнота данных рядом считается
        // по самим показаниям и остаётся верной.
        half.append(
            label("Срок сенсора"),
            value("Неизвестен", "muted"),
            hint("Сборщик ещё не видел даты установки")
        );
        return half;
    }

    const ends = new Date(sensor.ends * 1000);
    const started = new Date(sensor.started * 1000);
    const leftMs = ends.getTime() - Date.now();
    const days = Math.floor(leftMs / 86400000);
    const expiring = leftMs <= 0 || days <= WARN_DAYS;

    half.append(
        label(
            `Срок сенсора${sensor.lifetime_days ? `, ${sensor.lifetime_days} ${plural(sensor.lifetime_days, "день", "дня", "дней")}` : ""}`,
            `поставлен ${formatShortDay(started)}`
        ),
        value(leftText(leftMs, days, ends), expiring ? "warn" : null),
        bar(sensor, started, ends, expiring)
    );
    return half;
}

/* Фраза о сроке. Три разных состояния, и каждое читается само по себе:
   «0 дней осталось» в последние часы звучит как «уже всё», хотя сенсор ещё
   работает, а у просроченного числа дней нет вовсе — есть дата. */
function leftText(leftMs, days, ends) {
    if (leftMs <= 0) return `Срок вышел ${formatShortDay(ends)}`;
    if (days < 1) return `Осталось ${formatSpan(leftMs / 1000)}`;
    return `${days} ${plural(days, "день", "дня", "дней")} осталось, до ${formatShortDay(ends)}`;
}

/* Полоса прожитого срока. Она дублирует число слева — и в этом смысл: число
   отвечает на «сколько осталось», полоса на «много это или мало», а вместе
   они отвечают без арифметики в уме. */
function bar(sensor, started, ends, expiring) {
    const track = document.createElement("div");
    track.className = "sensor__track";

    const total = ends.getTime() - started.getTime();
    const lived = Math.min(Math.max(Date.now() - started.getTime(), 0), total);

    const fill = document.createElement("div");
    fill.className = expiring ? "sensor__fill sensor__fill--warn" : "sensor__fill";
    fill.style.width = `${(100 * lived) / total}%`;

    // Полоса — картинка числа, стоящего рядом, и для скринридера это повтор.
    track.setAttribute("aria-hidden", "true");
    track.append(fill);
    return track;
}

/* Правая половина: полнота данных. Процент — главное число, разрывы под ним
   объясняют, из чего он сложился: 97 % одной сутками длящейся дырой и 97 %
   десятком пятиминутных — разные недели. */
function dataHalf(sensor) {
    const half = document.createElement("div");
    half.className = "sensor__half";

    half.append(
        label(`Полнота данных за ${sensor.window_days} ${plural(sensor.window_days, "день", "дня", "дней")}`),
        value(percent(sensor.coverage))
    );

    half.append(
        hint(
            sensor.quiet
                ? `${sensor.quiet} ${plural(sensor.quiet, "разрыв", "разрыва", "разрывов")}, всего ${formatSpan(sensor.quiet_minutes * 60)}`
                : "Без разрывов"
        )
    );
    return half;
}

function label(text, aside) {
    const element = document.createElement("p");
    element.className = "sensor__label";
    element.textContent = text;

    if (aside) {
        const extra = document.createElement("span");
        extra.className = "sensor__aside";
        extra.textContent = aside;
        element.append(extra);
    }
    return element;
}

function value(text, kind) {
    const element = document.createElement("p");
    element.className = kind ? `sensor__value sensor__value--${kind}` : "sensor__value";
    element.textContent = text;
    return element;
}

function hint(text) {
    const element = document.createElement("p");
    element.className = "sensor__hint";
    element.textContent = text;
    return element;
}
