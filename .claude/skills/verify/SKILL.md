---
name: verify
description: Drive GlucoseStat's real surfaces — collector process, publish CLI, dashboard page — to observe a change at runtime. Use when verifying a diff rather than running tests.
---

# Verifying GlucoseStat at runtime

Three surfaces. Pick the ones the diff reaches; none of them need prod.

Never point a drive at prod MySQL: `store_readings` would write test values
into real health data. Everything below uses a throwaway database.

## Throwaway MySQL

```bash
mysqld --initialize-insecure --datadir=$SCRATCH/mysql/data \
  --basedir=/opt/homebrew/Cellar/mysql@8.4/8.4.11_2
mysqld --datadir=$SCRATCH/mysql/data --basedir=/opt/homebrew/Cellar/mysql@8.4/8.4.11_2 \
  --port=13306 --socket=/tmp/gsdrive.sock --mysqlx=OFF --bind-address=127.0.0.1 &
mysql --socket=/tmp/gsdrive.sock -u root -e "CREATE DATABASE glucose;
  CREATE USER 'glucose'@'%' IDENTIFIED BY 'glucosepw';
  GRANT ALL ON glucose.* TO 'glucose'@'%';"
```

Killing this process mid-run is how you reproduce a database outage.
`journal_entries` belongs to the bot and `init_schema()` does **not** create
it — create it by hand when the drive needs journal rows.

## Collector process (`main.py`)

Stub the two outbound hosts instead of reaching them. A `sitecustomize.py` on
`PYTHONPATH` that rewrites `https://api-de.libreview.io` and
`https://api.pushover.net` to a local HTTP server is enough: `main.py`,
`run_once`, `notify` and the LibreLinkUp client all still run as themselves,
and no notification can reach the real phone.

Plant `.llu-token.json` with a far-future `expires` to skip login. Always set
`PUBLISH_PATH` to a temp file — otherwise the run overwrites the tracked
`web/data.json`.

```bash
PYTHONPATH=$DRIVE MYSQL_USER=glucose MYSQL_PASSWORD=glucosepw \
  MYSQL_HOST=127.0.0.1 MYSQL_PORT=13306 MYSQL_DB=glucose \
  PUSHOVER_TOKEN=stub PUSHOVER_USER=stub FETCH_INTERVAL_MINUTES=1 \
  PUBLISH_PATH=$DRIVE/data.json venv/bin/python main.py
```

The alert path is worth driving on any change near it: serve a low value from
the stub, kill MySQL, and check that the stub still records a Pushover POST.
Remember `init_schema()` runs before the loop, so the database has to be up at
startup — take it down after the first cycle.

Clean up afterwards: `.llu-token.json`, `.alerts.json`, `.last-reading` are
written into the repo root.

## Publish CLI (`publish.py`)

`PUBLISH_PATH=$DRIVE/page.json venv/bin/python publish.py` is the documented
one-off rebuild, and the honest way to check snapshot contents. Compare
against pre-fix behaviour by running the same command with the diff stashed.

## Dashboard page

`PUBLISH_PATH=<snapshot> venv/bin/python preview.py --real --shot <dir>`
renders the real assets in chrome-headless-shell against any snapshot you
craft — the fastest way to see cards, tables and the overlay.

For behaviour that needs more than one fetch (the page reloads `data.json`
every 60 s), serve the site yourself and use virtual time:

```bash
chrome-headless-shell --headless --virtual-time-budget=75000 \
  --dump-dom http://127.0.0.1:PORT/          # or --screenshot=out.png
```

`--dump-dom` is better evidence than a screenshot for element state — the
`hidden` attribute on `#empty` tells you whether the error banner cleared.
A server that fails the first `data.json` request and then succeeds
reproduces a transient blip; restart it between runs, since its state is
per-process.

## Gotchas

* MySQL's socket path must stay under ~100 characters — keep it in `/tmp`.
* macOS has no `timeout`; a command using it exits 127 and the drive proves
  nothing. Run long processes in the background and kill them instead.
* Check the port is actually free before starting a stub. "Address already in
  use" kills the stub silently and the collector then just fails to poll,
  which looks like a passing result.
* Always read raw process output. Grepping for the line you hope to see hides
  the run that never happened.
