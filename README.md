# GlucoseStat

Collects FreeStyle Libre 3 readings from LibreLinkUp into MySQL and renders a
public dashboard from them — live at
[glucose.gistrec.cloud](https://glucose.gistrec.cloud).

A single pm2 process does both jobs: it polls LibreLinkUp every five minutes,
stores whatever is new, and rewrites `web/data.json`. nginx serves `web/` as
plain static files, so a page view never touches MySQL and never reaches
Abbott.

```
LibreLinkUp ──poll 5m──▶ main.py ──▶ MySQL ──▶ publish.py ──▶ web/data.json ──▶ nginx
```

## What the page shows

Current value with a trend arrow, a chart over 24 hours / 7 days / 30 days,
and per-period statistics: time in range, average, spread, coefficient of
variation, and GMI (estimated HbA1c, shown only for windows of a week or
more, where it is statistically meaningful).

Values are stored in mg/dL and displayed in mmol/L. The target range is
70–180 mg/dL (3.9–10.0 mmol/L), the standard CGM consensus range.

`data.json` carries nothing but timestamps and glucose values. The LibreLinkUp
payload also contains the patient's name, date of birth and sensor serial, and
none of that belongs on a public URL.

## Configuration

Copy `.env.example` to `.env` and fill it in:

* `EMAIL`, `PASSWORD` — the LibreLinkUp account the Libre 3 app shares the
  sensor with.
* `MYSQL_*` — database connection. The collector creates its table on first
  start.
* `MYSQL_SSL_CA` — only for managed databases that require TLS.
* `FETCH_INTERVAL_MINUTES` — polling interval, defaults to `5`.

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

## Tests

The snapshot maths — time in range, GMI, variability, downsampling, trend —
is pure functions over a list of readings, so the tests need neither a
database nor the network:

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
