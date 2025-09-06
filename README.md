# GlucoseStat

A small utility that periodically fetches glucose measurements from
LibreLinkUp and stores them in a MySQL database.

Only measurements from the last six hours are persisted. The application
keeps an in-memory cache of timestamps for the past twelve hours to avoid
inserting duplicates.

## Configuration

The application expects the following environment variables:

* `EMAIL` and `PASSWORD` – credentials for LibreLinkUp.
* `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DB` –
  database connection settings.
* `FETCH_INTERVAL_MINUTES` – optional interval for fetching data (defaults
  to `5`).

Run the script with:

```bash
python main.py
```
