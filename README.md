# GlucoseStat

Collects FreeStyle Libre 3 readings from LibreLinkUp into MySQL and renders a public dashboard from them — live at [glucose.gistrec.cloud](https://glucose.gistrec.cloud).

A single pm2 process does the lot: it polls LibreLinkUp every five minutes, stores whatever is new, rewrites `web/data.json`, and pushes a phone alert if the reading is low. nginx serves `web/` as plain static files, so a page view never touches MySQL and never reaches Abbott.

```
LibreLinkUp ──poll 5m──▶ main.py ──▶ MySQL ──▶ publish.py ──▶ web/data.json ──▶ nginx
                             └──────────────▶ notify.py ───▶ Pushover
```

## Configuration

Copy `.env.example` to `.env` and fill it in:

* `EMAIL`, `PASSWORD` — the LibreLinkUp account the Libre 3 app shares the sensor with.
* `MYSQL_*` — database connection. The collector creates its table on first start.
* `MYSQL_SSL_CA` — only for managed databases that require TLS.
* `FETCH_INTERVAL_MINUTES` — polling interval, defaults to `5`.
* `LLU_REGION` — which LibreLinkUp region to ask, defaults to `de`. Rarely needed: login follows a regional redirect and keeps the corrected region for as long as the session lasts, so a foreign account costs one extra request per sign-in rather than a setting.
* `PUSHOVER_TOKEN`, `PUSHOVER_USER` — turn on the [alerts](#alerts). Unset, the collector stores readings and alerts about nothing.

## Running

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python main.py          # collector loop
./venv/bin/python publish.py       # one-off snapshot rebuild
```

Both read `.env` themselves. The snapshot lands in `web/data.json`; to send it somewhere else, export `PUBLISH_PATH`.

The path resolves as the module loads, before any `.env` is read, so a line in that file reaches nothing at all. That includes the manual rebuild, which would still overwrite the file nginx serves:

```bash
PUBLISH_PATH=/tmp/snapshot.json ./venv/bin/python publish.py
```

In production it runs under pm2:

```bash
pm2 start ecosystem.config.js
```

## Preview

`preview.py` serves the page from a throwaway copy of `web/` with a synthetic snapshot, so a change can be looked at before it reaches the server — no database, no LibreLinkUp, nothing written to `web/data.json`:

```bash
./venv/bin/python preview.py                 # синтетика, открывает браузер
./venv/bin/python preview.py --real          # настоящий web/data.json
./venv/bin/python preview.py --shot shots/   # снимки светлой и тёмной тем
```

The synthetic fortnight is there for a reason. The real snapshot may carry no events at all, since the journal is the bot's, and against that file the event lanes and the meal review render as they did before any of them existed.

It fakes a low every eleventh meal and an oversized rise every seventh, because a tidy curve would show a review of nothing but "в ориентире".

`--shot` needs a headless Chrome; it looks in the Playwright cache first, then in `/Applications`, then on `PATH`. Without one it prints the URL and stops.

## Tests

The snapshot maths — time in range, GMI, variability, downsampling, trend — is pure functions over a list of readings, and so is the alert decision, so the tests need neither a database nor the network:

```bash
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m pytest
```

## What the page shows

Current value with a trend arrow, a chart over 24 / 48 hours / 7 days / 30 days, and per-period statistics: time in range, average, spread and coefficient of variation. GMI sits under the current value, outside the period tiles.

### Chart windows

The 48-hour window exists to answer "what was happening exactly a day ago". On the 24-hour one that moment sits on the very edge of the canvas, clipped by it.

The 30-day window is not a line but a box per local day: p25 to p75, with the median notched across it.

A day that is not whole carries a grey dash under its box, and the tooltip names which kind of "not whole" it is. Either the day is shown in part (clipped by the edge of the window, or still running), or the sensor was quiet through some of it and the coverage says how much. The box itself is not recoloured: its quartiles are honest, they just describe a slice of the day.

### Forecast

On the hourly windows the curve continues past "now" as a dashed forecast. When [GlucoseBot](https://github.com/gistrec/GlucoseBot) has written one, it is the model's: gradient boosting over the last hour of the curve, the insulin and carbohydrates still active from the journal, and the hour of day, retrained nightly on everything the journal holds and written to `glucose_forecasts` on every new reading (`ml/live.py` there). The snapshot carries it as `forecast`, with absolute points at thirty and sixty minutes; the page draws it as one dashed line through both, a small ring at the half hour and the value at the end. On its own data it beats the straight line after meals, where the straight line is worst (1.29 against 1.71 mmol/L RMSE at thirty minutes over six weeks).

Without a forecast, or with one computed from a reading more than fifteen minutes behind the latest, the tail is the old one: a linear extension of the same 15-minute rate that draws the trend arrow, capped at half an hour because food and insulin break the straight line sooner than it comes true.

The straight line does not vanish altogether. A sharp fall is the one thing it reads better than the model, which smooths rare dips toward the mean and did not call a single low half an hour ahead on the same data, while the line caught two thirds of them. So when the linear extension crosses the low threshold within its half hour and the model's tail does not, the line stays on the canvas in the low colour, thinner and without a number: a warning, not a second forecast.

Both disappear while the latest reading is stale: extending a curve that stopped moving would be lying twice.

### Thinned readings

Readings are thinned into buckets before they reach the browser — but a bucket that dipped below 70 mg/dL publishes its **minimum** instead of its average.

A two-minute drop to 3.4 mmol/L inside a quarter-hour bucket averages out to 6.2 and disappears; the curve would show calm where there was a low. The error is pushed to the safe side on purpose, and only downwards: highs last hours and land in a bucket whole, so peaks are still averaged.

### Out-of-range episodes

The same window also carries the out-of-range episodes themselves, counted from the raw readings: start, end, how far past the threshold, and duration. The curve alone answers "was there a low". It cannot answer "how many", since two dips in neighbouring buckets look like one, or "for how long", since a minute is a quarter of a pixel on the weekly panel.

Episodes are drawn as segments along the edges of the plot with the duration beside them: lows red along the floor, highs along the ceiling, each on the side the curve left towards. Not along the threshold line itself: there the segment lay across the dip, and the two marks, "here is where it went" and "here is how long it lasted", merged into one unreadable figure.

An episode ends where the reading comes back over the threshold, and ones separated by less than 15 minutes count as one: flapping around the line is one low lived through, not five. There is deliberately no minimum duration.

Both thresholds are the ones the curve and the zone bands are already painted by (`lows.py`), so a band can never appear where the graph still looks green.

### Sensor silence

Where the sensor went quiet, the hourly windows draw a grey band instead of just breaking the line, labelled "нет сигнала" and how long the silence lasted. A sensor change reads as a stated hour and a half rather than a hole the page declines to explain.

The threshold is the one that breaks the curve: three missed readings in a row, since a single miss is an ordinary upload delay.

The collector counts these windows from the raw readings, for the same reason it counts the low episodes there. A bucket edge would move the boundary by its own step, and one silence would be "1 ч 45 мин" on the daily panel and "1 ч 50 мин" on the 48-hour one.

A silence still running has no right edge: the page draws it from the last reading to the moment of the snapshot. It draws nothing while the forecast tail is there, since that tail only exists beside a fresh reading.

The weekly and monthly panels get no bands: an hour is three pixels wide there, with nothing to write in them.

### Sensor card

Below the statistics sits the sensor itself: how many days it has left, and how much of the last week it actually covered.

The days left are the question the page can answer before the app does. A sensor lasts two to three weeks, and learning that from one that has already stopped costs the day it takes a replacement to arrive.

The activation moment comes from LibreLinkUp, in the same `graph` response as the readings. The lifetime does not, and cannot: Abbott reports when a sensor was applied, never how long it will run. What the payload does carry is a product-type number, and `SENSOR_MODELS` is where that number turns into days.

The table is empty on purpose. Abbott publishes no such numbering, and a guess in it would repeat the bug that made this card call a Libre 3 Pro a plain Libre 3 and end its life a day early. The collector logs the number it actually sees, once per process, which is how a row gets added; until then the lifetime is the fallback constant.

The collector stores both the start and the computed end in `collector_state`, so a renderer on a read replica would publish the same dates without needing to know the model. The days left are counted in the browser, since a number baked into a snapshot would be yesterday's by morning.

The serial number sitting next to all this in Abbott's payload is dropped where it is parsed. The card names no model at all: the one in the page header is written by a human, where it reads as a claim rather than as data. Abbott does not always send the block; then the card says the date is unknown rather than guessing one.

The second half, data completeness, is counted from the readings alone. Time in range over a week with a day-long hole in it looks as solid as the honest kind, and the card is where the page admits the difference. Silence before the first reading and silence still running both count: a week does not become complete because the sensor is quiet right now.

### GMI

GMI ignores the selected period: it is always the estimated HbA1c over the last **14 days**, the window Bergenstal et al. (2018) calibrated the formula on. Tying it to the buttons would put two different numbers, a week's GMI and a month's, under one name, and neither would be what a clinician means by it. So it is not a period tile either: it stands under the current value and reads the same on every tab, where a tile beside "24 hours" figures would pass for one of them.

Below 70% CGM coverage over those two weeks the line disappears rather than showing a figure: an HbA1c estimated from three days looks as authoritative as one estimated from fourteen.

### Units and zones

Values are stored in mg/dL and displayed in mmol/L. The target range is 70–180 mg/dL (3.9–10.0 mmol/L), the standard CGM consensus range.

The chart splits it further at 130 mg/dL (7.2 mmol/L), the fasting target: green below that line, yellow up to 180 (acceptable after meals), and red beyond either end of the range. Time-in-range statistics still count the full 70–180 band; the three zones only change how the chart reads.

## Meals and insulin

The events come from the journal that [GlucoseBot](https://github.com/gistrec/GlucoseBot) writes: carbohydrates estimated from a photo and confirmed by hand, and insulin doses logged by hand. The collector only reads that table, never creates it: if the bot was never deployed, `journal_since` returns nothing and the page renders as before.

Events are drawn as two lanes under the glucose line, sharing its time axis: carbohydrates in grams and insulin in units, each growing from its own baseline. Not a second vertical scale on the same plot: the alignment of two y-axes is arbitrary, and a chart built that way invents a correlation that isn't in the data.

Short and long insulin share one lane because they share a unit; the short one is filled, the long one is an outline.

A new pen is an event too, though not an amount: `pen_bolus` and `pen_basal` entries carry no units, so they are not columns. They stand in the insulin lane as a vertical mark with a dot on top, filled for the short pen and hollow for the long one (the same rule the columns follow), and the hover names them.

The date of a new pen says more than a bare mark suggests. A pen is good for 28 days once opened, and a fresh one and one running out do not act alike, so a shift in the meal review after that mark points at the insulin before it points at the ratio.

The lane appears for a mark alone, without a single dose in the window: the mark has to stand somewhere.

Only the 24- and 48-hour windows show them. A month holds a hundred marks, and they merge into a solid band that says nothing.

## Fingersticks

A `fingerstick` entry is a blood reading from a meter, logged in the bot with `/sugar 5,2`.

It is not a lane and not a series: it measures the same quantity as the curve, in the same units, so it sits on the curve itself. It is drawn as a diamond at its own value, next to the sensor point it disagrees with. A second scale, or a lane of its own below, would break what is worth seeing here: the gap between them.

It never joins the sensor series. Time in range, AGP, the night summary and the meal review are all computed from the sensor alone: blood and interstitial fluid are different measurements, and averaging them would make all four numbers quietly untrue.

The colour is zonal — the same `readingColor` the line uses, no sixth hue on the canvas. That is what makes the interesting case readable: on a compression low the sensor dives into red while the diamond stays blue at 5,3, and the disagreement reads before any number does. The hover names both sides and the difference, because two shapes five pixels apart cannot be subtracted by eye.

The most recent one also stands as the first row beside the current value, above carbohydrates and insulin: `Глюкометр 5,3 · 12 мин назад`. It belongs there rather than among the journal rows because it is about the same quantity as the big number above it.

That row is the point of the whole feature for anyone reading the page over the patient's shoulder: from the curve alone, a compression low and a real one look identical.

Two days of them, like the other events, and the same two windows: on a month a diamond the size of a dip would lie about its own precision.

## Nights

Night is the stretch of the day nobody watches. A low at three in the afternoon announces itself; at three in the morning only the sensor notices, and only if someone looks afterwards.

Every other panel on the page hides it. The weekly curve is thinned into 15-minute buckets, so a dip to 3.6 mmol/L that lasted a few minutes is averaged with its neighbours and drawn as 4.3. The night block therefore counts from the **raw** readings in `nights.py`, not from what the chart shows.

It reports three figures over its own seven-day window: nights with hypoglycaemia, the median of the nightly minimums, and the median drift. The buttons above do not move it, same deal as the meal review and the day profile.

The drift is measured from 03:00 to 06:00, not from midnight. By three the supper bolus has finished working, so the difference describes basal insulin; from midnight it would describe the tail of the meal. On a live week the two choices disagree about the sign, median +0.3 from midnight against −0.7 from three, which is why the narrower window is the honest one.

The hypoglycaemia count carries its minutes, both per night and for the week. "1 of 7" says nothing about whether that night spent a minute under the line or half an hour, and those are different nights. The minutes come from the same episodes the chart draws (`lows.py`), so the two places can never disagree about how long a low lasted.

The page names both denominators. Hypoglycaemia counts and the minimum are computed over every night the sensor covered: they are measured facts, and filtering them would throw away the nights that matter most. A late supper makes a night more dangerous, not less interesting.

The drift is computed only over "fasting" nights, where the journal shows no food or bolus within four hours of 03:00 and none during the night itself. An evening with no journal entries at all counts as unknown rather than fasting: an empty evening means "not recorded", not "did not eat". Below three qualifying nights the median is not shown at all, only the count that fell short.

Each night also gets a sparkline. Its buckets carry the **minimum** of the bucket rather than the average, so the lowest point of the drawn line is the same reading as the number printed beside it. Smoothing the dip away is the mistake the block exists to correct.

## Meal review

For each recent meal the page reports what happened in the four hours after it: the rise above the level at the moment of eating, how long the peak took, whether glucose came back, and whether a low followed.

The journal is read a fortnight back, but the snapshot carries at most the two dozen freshest windows, so the page names the actual date its review starts from rather than promising the full fortnight.

Curves are overlaid on the moment of eating, normalised to that level. In absolute values the median comes out nearly flat, because lunch starts from one level and dinner from another, and the two cancel the very rise the chart exists to show.

### Groups

Above the curves sits a row of groups: all, no bolus, under 40 g, 40 to 70, 70 and over. They do not overlap and their counts add up to the total, because a meal without a shot is not put in a carb bucket. The rise after 30 g with insulin and after 30 g without are two answers to different questions, and averaging them answers neither.

Picking a group highlights it rather than filtering it out: the other curves fade but stay, since a group is worth nothing to look at without the rest to compare it against. The table below keeps showing the same meals as the canvas above it, which it has always done.

The median line is recomputed over the group. A button that changed only how pale the curves were, while the line everyone actually reads stayed about everybody, would be a button that lies. In a group of one or two there is no median at all (three curves is the minimum for it to mean anything), so the legend drops the line instead of promising one.

Under the canvas a line says what the group came to: how many meals, the median of their peak rises, and how many of them cleared the target.

### Bolus

Each meal also shows its bolus: the units and how far ahead of the meal the shot went in. A shot belongs to a meal when it lands within half an hour of it, and to that meal alone: a dose between two meals is credited to the nearer one. A split dose is summed and keeps the lead of its first shot, because that shot decides whether the insulin made it to the peak.

### Windows

A window that runs into the next meal is cut short at that meal: the points after it belong to two events at once, so only the clean prefix of the curve is kept.

Records less than half an hour apart are one meal, summed and reviewed from the first of them. A plate and the second helping after it, taken apart, leave the first a stub of a window and the second a baseline read mid-rise. The review table marks such a meal with a `Σ` that lists its records on hover; on the daily lane they still stand as separate columns.

Snacks of ten grams of carbs or less play no part in the review: they cut nothing and get no window of their own. A square of chocolate barely shows on the curve, it should not cost a whole lunch its review, and a window opened on it would re-measure the neighbouring meal's rise from a mid-excursion baseline.

Windows still open are drawn but left out of the summary medians: one that has not seen its four hours out does not know its peak.

A window cut short is left out only when its rise had already passed the target by then. Nobody saw where that one ended, while a rise that stayed within the target is an outcome the cut did not take away, and it counts like any other.

Lows are the exception: the hypoglycaemia count covers every window shown. The usual answer to a low is food, food cuts the window, and a count over clean windows only would drop the very lows that happened.

### Carb marks

Each carb figure carries two marks, and they answer different questions. The first is how the number was obtained: ⚖︎ weighed, ✎ spoken, ▣ estimated from a photo. The second is how much faith it deserves: ●●● certain, ●●○ roughly, ●○○ a guess. It comes from the person, who answers the bot's buttons after every meal.

Neither follows from the other: a weighed portion gets half eaten, and a spoken "30 g" may be read off a wrapper. Where nobody answered, the confidence falls back to the old rule: full for scales, middling for a spoken number, and for a photo as far as the model runs agreed with each other. A meal merged from several records takes the marks of its weakest one.

### Carb ratio

Below the list sits the carb ratio: grams of carbohydrate per unit of rapid insulin, taken from the records rather than from a rule. The same meals are cut two ways: by time of day, because insulin sensitivity follows the sun rather than the name of the meal, and by portion size.

A group of fewer than three meals is not shown at all. The median rise sits beside each ratio, because the same "10 g/u" with a rise inside the target and with one twice as high are different stories.

The portion boundaries are fixed at 40 g and 70 g, not recomputed as percentiles of the current sample: "under 40 g" has to mean the same thing next month, or there is nothing to compare it to. Those two numbers come from this journal: meals in it run from 14 to 100 g with a median of 50, so the pair splits them close to terciles.

Finer cuts have nothing to divide: snacks under `SNACK_CARBS` never enter the review, and meals past a hundred grams are rare enough to miss the quorum.

This describes outcomes and stops there. Whether a dose was right also depends on activity, on illness and on insulin still active from an earlier injection. None of that is in this data, and the page says so.

## Privacy

The LibreLinkUp payload also contains the patient's name, date of birth and sensor serial, and none of that belongs on a public URL, so `data.json` carries none of it.

It does carry the meal and insulin events, which are more personal than the curve itself: they show when the day starts, when it ends, and what the treatment looks like. The page is `noindex` but it is not access-controlled. If that is not acceptable, put the events behind basic auth in nginx or drop `journal_entries` from the collector's reach.

## Alerts

With Pushover configured, every poll checks the newest reading against four thresholds, two on each side:

| Reading | Level | Priority |
|---|---|---|
| below 55 mg/dL (3,0 mmol/L) | critically low | 2 — repeats every two minutes until acknowledged |
| below 70 mg/dL (3,9 mmol/L) | low | 1 — sounds during quiet hours |
| above 180 mg/dL (10,0 mmol/L) | high | 1 |
| above 288 mg/dL (16,0 mmol/L) | critically high | 2 |

The comparisons are strict on both sides: exactly 3,9 is not yet low and exactly 10,0 is not yet high.

180 is not a number of its own. It is `TARGET_HIGH_MGDL` from `publish.py`, the line above which the page already colours the curve and counts time above target, so the alert fires where the page already says "high".

Each level names its own sound instead of leaving it to whatever default tone the app happens to be set to, so the direction is audible before the phone is out of a pocket: `falling` and `siren` below, `climb` and `spacealarm` above.

Both priorities get past an iPhone's mute switch: Pushover has held an Apple Critical Alerts entitlement since February 2020, though the bypass is a separate toggle per priority in the iOS app. Verified live on 14.09.2026: with Critical Alerts enabled for High and Emergency, both ring through silent mode.

The same reading arrives on every poll, so the collector remembers the episode in `.alerts.json`. It lives in a file rather than in memory because pm2 restarts the process on any failure, and a forgotten episode means the phone buzzes again about a low it already reported. Within one episode it repeats at most every 30 minutes, and it escalates immediately if the level turns critical.

An episode closes only ten past the threshold: at 80 mg/dL coming up, at 170 coming down. Without that margin a reading hovering around the line would open a new episode, and send a new alert, every other poll. The margin is read from the side the episode opened on: 175 mg/dL ends a low episode and does not end a high one, though it is the same number.

A reversal is a new episode, not a deepening. Coming out of a hypo and overshooting into a high is two events, and the second speaks at once instead of sitting out the repeat interval left over from the first. Its "high for 40 minutes" counts from the turn, not from the start of the hypo.

Readings older than 15 minutes never alert. Otherwise a restart would fire an alarm over last week's hypo, still sitting in the database as the latest row.

This is an addition to the alarms of the Libre app, not a replacement: nothing fires while Abbott is unreachable, the sensor is off, or the collector is down.

### Silenced by blood

A fingerstick logged in the last 15 minutes and recovered past the margin on the side the alert is firing from closes the episode: no alert, and the remembered state is cleared.

The sensor measures interstitial fluid and lags blood, and if you roll onto it in your sleep it draws a low that never happened. A drop of blood in range means there is no low, whatever the sensor says. This is the single case where an alert is cancelled by an outside fact rather than by another reading.

The side matters, and getting it wrong would be worse than not checking at all. 16,6 mmol/L in blood is certainly "not low", and a rule that only looked for "not low" would let that reading silence the high alert it confirms. So the check is directional: 80 mg/dL or above against a low, 170 or below against a high, the same `_recovered` the sensor closes episodes with.

Three numbers guard it, and each of them fails toward alerting:

* **80 mg/dL, not 70.** A meter reading 3,9 confirms a low at the line, it does not refute one. It is the same recovery margin the episode already closes at, and 170 mirrors it above.
* **15 minutes.** Blood moves a couple of mmol/L in that time, after which the check no longer says anything about now. The episode is *closed*, not frozen, so the next poll after the check goes stale speaks up at once instead of sitting out the 30-minute repeat: checked, quiet, asked again a quarter of an hour later.
* **Absent means alert.** The journal may not exist and MySQL may be down; the lookup has its own `try` and yields `None`, and the alert then behaves as it did before. Staying silent because the collector could not ask is the failure that must not happen.

A check below the threshold changes nothing: it confirms the low. The level of the alert is still decided by the sensor: blood decides whether to speak, not how loudly.

## Staleness

Since nothing fires in those cases, silence is watched from outside the process. Every poll stamps `.last-reading` with the mtime of the newest reading in the database — not with the current time, or a collector that came back after a day off would report the data as fresh the moment it started. Netdata's `filecheck` reads that mtime and alerts when it stops moving:

```
/etc/netdata/go.d/filecheck.conf     # job "glucose" -> .last-reading
/etc/netdata/health.d/glucose.conf   # warn at 30m, crit at 1h, to fleetcrit
```

An hour-long gap is not always a fault: LibreLinkUp reports nothing at all while no sensor is on, so a sensor change shows up here as a hole the width of however long the new one took to go on.

## Roles

The two entry points are two roles, and only one of them may run twice:

| | collector — `main.py` | renderer — `publish.py` |
|---|---|---|
| Runs as | a pm2 loop, polling Abbott | a one-shot command, on a schedule |
| Copies | **exactly one**, anywhere | as many as there are hosts serving the page |
| Database | writes | reads only |
| Needs | the whole `.env` | the `MYSQL_*` block |
| Local state | `.llu-token.json`, `.alerts.json` | none |

The collector is a singleton for three separate reasons, any one of which is enough:

* a replica refuses its writes;
* LibreLinkUp hands out one session per account, so two logins evict each other;
* `.alerts.json` is per-process, so two copies would each alert about the same low.

The renderer is not. A second host with a read replica can rebuild the snapshot from its own copy of the data and serve its own `web/data.json`.

Production does not do that. One host runs the collector and serves the page; visitors from Russia, where the Finnish origin is throttled, reach it through a proxy on a Russian host over WireGuard, so there is one `data.json` and one place to deploy.

This is why `last_success`, the moment the collector last reached LibreLinkUp, lives in the `collector_state` table rather than only in the collector's memory. A renderer on another machine would have no memory to inherit it from, and without it the page there could never say "the numbers stopped moving". Nothing else tells fresh glucose from yesterday's.

## Notes

`librelinkup.py` is a hand-rolled client rather than the `pylibrelinkup` package. That package validates `TrendArrow` against an enum of 1–5 and raises on `TrendArrow: 0`, the value Abbott sends whenever the trend is unknown, including a fresh sensor's entire warm-up window.

The API returns two timestamps per reading, and only `FactoryTimestamp` is UTC; `Timestamp` is the patient's local time without a zone. Likewise `Value` follows the account's display units while `ValueInMgPerDl` does not, which is why rows are stored in mg/dL.

Everything LibreLinkUp reports is stored, including a sensor's warm-up hour, during which a freshly applied sensor sits at 500 mg/dL with `isHigh` set. Expect one such spike per sensor change. The only values dropped are those outside the sensor's own 40–500 mg/dL scale, which are not readings at all.

Abbott answers a burst of logins with HTTP 476 and a `Retry-After` of up to a day. The collector caps its wait at 10 minutes rather than honouring that literally: the block often lifts sooner, and a day of silence costs more than a few extra attempts.

An empty graph response is normal: LibreLinkUp only reports while a sensor is active, so with no sensor on, `graph` returns nothing and the page reports the data as stale.

The older `glucose_measurements` table comes from the 2025 version of this script, which stored mmol/L against local timestamps. It is left untouched; the current schema is `glucose_readings`.
