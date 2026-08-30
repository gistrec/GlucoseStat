# GlucoseStat

Collects FreeStyle Libre 3 readings from LibreLinkUp into MySQL and renders a
public dashboard from them — live at
[glucose.gistrec.cloud](https://glucose.gistrec.cloud).

A single pm2 process does the lot: it polls LibreLinkUp every five minutes,
stores whatever is new, rewrites `web/data.json`, and pushes a phone alert if
the reading is low. nginx serves `web/` as plain static files, so a page view
never touches MySQL and never reaches Abbott.

```
LibreLinkUp ──poll 5m──▶ main.py ──▶ MySQL ──▶ publish.py ──▶ web/data.json ──▶ nginx
                             └──────────────▶ notify.py ───▶ Pushover
```

## What the page shows

Current value with a trend arrow, a chart over 24 hours / 7 days / 30 days,
and per-period statistics: time in range, average, spread, coefficient of
variation, and GMI.

GMI is the one figure that ignores the selected period: it is always the
estimated HbA1c over the last **14 days**, the window Bergenstal et al. (2018)
calibrated the formula on. Tying it to the buttons would put two different
numbers — a week's GMI and a month's — under one name, and neither would be
what a clinician means by it. Below 70% CGM coverage over those two weeks the
card disappears rather than showing a figure: an HbA1c estimated from three
days looks exactly as authoritative as one estimated from fourteen.

Values are stored in mg/dL and displayed in mmol/L. The target range is
70–180 mg/dL (3.9–10.0 mmol/L), the standard CGM consensus range.

The LibreLinkUp payload also contains the patient's name, date of birth and
sensor serial, and none of that belongs on a public URL, so `data.json` carries
none of it.

It does now carry meal and insulin events (see below), which are more personal
than the curve itself: they show when the day starts, when it ends, and what the
treatment looks like. The page is `noindex` but it is not access-controlled — if
that is not acceptable, put the events behind basic auth in nginx or drop
`journal_entries` from the collector's reach.

## Meals and insulin

The events come from the journal that [GlucoseBot](https://github.com/gistrec/GlucoseBot)
writes — carbohydrates estimated from a photo and confirmed by hand, and insulin
doses logged by hand. The collector only reads that table, never creates it: if
the bot was never deployed, `journal_since` returns nothing and the page renders
exactly as before.

Events are drawn as two lanes under the glucose line, sharing its time axis:
carbohydrates in grams and insulin in units, each growing from its own baseline.
Not a second vertical scale on the same plot — the alignment of two y-axes is
arbitrary, and a chart built that way invents a correlation that isn't in the
data. Short and long insulin share one lane because they share a unit; the short
one is filled, the long one is an outline.

Only the 24-hour window shows them. A month holds a hundred marks, and they
merge into a solid band that says nothing.

## Meal review

For each meal over the last two weeks the page reports what happened in the four
hours after it: the rise above the level at the moment of eating, how long the
peak took, whether glucose came back, and whether a low followed. Curves are
overlaid on the moment of eating, normalised to that level — in absolute values
the median comes out nearly flat, because lunch starts from one level and dinner
from another, and the two cancel exactly the rise the chart exists to show.

Each meal also shows its bolus: the units and how far ahead of the meal the shot
went in. A shot belongs to a meal when it lands within half an hour of it, and
to that meal alone — a dose between two meals is credited to the nearer one. A
split dose is summed and keeps the lead of its first shot: that shot decides
whether the insulin made it to the peak.

A window that runs into the next meal is cut short at that meal: the points
after it belong to two events at once, so only the clean prefix of the curve is
kept. Snacks of ten grams of carbs or less cut nothing — a square of chocolate
barely shows on the curve, and it should not cost a whole lunch its review.
Windows still open and windows cut short are drawn but excluded from the
medians: neither has seen its four hours out, so neither knows its peak.

This describes outcomes and stops there. Whether a dose was right also depends
on activity, on illness and on insulin still active from an earlier injection —
none of which is in this data, and the page says so.

## Configuration

Copy `.env.example` to `.env` and fill it in:

* `EMAIL`, `PASSWORD` — the LibreLinkUp account the Libre 3 app shares the
  sensor with.
* `MYSQL_*` — database connection. The collector creates its table on first
  start.
* `MYSQL_SSL_CA` — only for managed databases that require TLS.
* `FETCH_INTERVAL_MINUTES` — polling interval, defaults to `5`.
* `PUSHOVER_TOKEN`, `PUSHOVER_USER` — turn on the low-glucose alerts below.
  Unset, the collector stores readings and alerts about nothing.

## Alerts

With Pushover configured, every poll checks the newest reading and sends a
notification when it is low: below 70 mg/dL (3.9 mmol/L) at priority 1, which
arrives even during quiet hours, and below 55 mg/dL (3.0 mmol/L) at priority 2,
which Pushover keeps repeating every two minutes until it is acknowledged in
the app.

The same low arrives on every poll, so the collector remembers the episode in
`.alerts.json` — in a file rather than in memory, because pm2 restarts the
process on any failure and a forgotten episode means the phone buzzes again
about a low it already reported. Within one episode it repeats at most every
30 minutes, and it escalates immediately if a low turns critical. The episode
closes only at 80 mg/dL, ten above the threshold: without that margin a reading
hovering around 70 would open a new episode — and send a new alert — every
other poll.

Readings older than 15 minutes never alert. Otherwise a restart would fire an
alarm over last week's hypo, still sitting in the database as the latest row.

Each level names its sound — `falling` for a low, `siren` for a critical one —
instead of leaving it to whatever default tone the app happens to be set to,
which also tells the two apart before the phone is out of a pocket. Neither
gets past an iPhone's mute switch: Pushover holds no critical-alert
entitlement, so a silenced phone stays silent, priority 2 included.

This is an addition to the alarms of the Libre app, not a replacement: nothing
fires while Abbott is unreachable, the sensor is off, or the collector is down.

## Staleness

Which is why silence is watched from outside the process. Every poll stamps
`.last-reading` with the mtime of the newest reading in the database — not
with the current time, or a collector that came back after a day off would
report the data as fresh the moment it started. Netdata's `filecheck` reads
that mtime and alerts when it stops moving:

```
/etc/netdata/go.d/filecheck.conf     # job "glucose" -> .last-reading
/etc/netdata/health.d/glucose.conf   # warn at 30m, crit at 1h, to fleetcrit
```

An hour-long gap is not always a fault: LibreLinkUp reports nothing at all
while no sensor is on, so a sensor change shows up here as a hole the width of
however long the new one took to go on.

## Running

```bash
python -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python main.py          # collector loop
./venv/bin/python publish.py       # one-off snapshot rebuild
```

In production it runs under pm2:

```bash
pm2 start ecosystem.config.js
```

## Preview

`preview.py` serves the page from a throwaway copy of `web/` with a synthetic
snapshot, so a change can be looked at before it reaches the server — no
database, no LibreLinkUp, nothing written to `web/data.json`:

```bash
./venv/bin/python preview.py                 # синтетика, открывает браузер
./venv/bin/python preview.py --real          # настоящий web/data.json
./venv/bin/python preview.py --shot shots/   # снимки светлой и тёмной тем
```

The synthetic fortnight matters more than it looks: the real snapshot may carry
no events at all — the journal is the bot's — and against that file the event
lanes and the meal review render exactly as they did before any of them existed.
It fakes a low every eleventh meal and an oversized rise every seventh, because
a tidy curve would show a review of nothing but "в ориентире".

`--shot` needs a headless Chrome; it looks in the Playwright cache first, then in
`/Applications`, then on `PATH`. Without one it prints the URL and stops.

## Tests

The snapshot maths — time in range, GMI, variability, downsampling, trend —
is pure functions over a list of readings, and so is the alert decision, so
the tests need neither a database nor the network:

```bash
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m pytest
```

## Notes

`librelinkup.py` is a hand-rolled client rather than the `pylibrelinkup`
package, which validates `TrendArrow` against an enum of 1–5 and raises on
`TrendArrow: 0` — the value Abbott sends whenever the trend is unknown,
including a fresh sensor's entire warm-up window.

The API returns two timestamps per reading, and only `FactoryTimestamp` is
UTC — `Timestamp` is the patient's local time without a zone. Likewise `Value`
follows the account's display units while `ValueInMgPerDl` does not, which is
why rows are stored in mg/dL.

Everything LibreLinkUp reports is stored, including a sensor's warm-up hour —
during which a freshly applied sensor sits at 500 mg/dL with `isHigh` set, so
expect one such spike per sensor change. The only values dropped are those
outside the sensor's own 40–500 mg/dL scale, which are not readings at all.

Abbott answers a burst of logins with HTTP 476 and a `Retry-After` of up to a
day. The collector caps its wait at 30 minutes rather than honouring that
literally: the block often lifts sooner, and a day of silence costs more than
a few extra attempts.

An empty graph response is normal: LibreLinkUp only reports while a sensor is
active, so with no sensor on, `graph` returns nothing and the page reports the
data as stale.

The older `glucose_measurements` table comes from the 2025 version of this
script, which stored mmol/L against local timestamps. It is left untouched;
the current schema is `glucose_readings`.
