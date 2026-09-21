# TNP Seagrass Nursery Monitor

In-situ water data from the Seneye monitors in the Posidonia nursery at North Mole,
collected on a schedule, stored in a database and published as a dashboard for
students and the research side of the project.

It is the tank-side counterpart to `tnp-ocean-harvester`: same shape (a Python
harvester on GitHub Actions, a database-agnostic store, a static dashboard), a
different source.

```
Seneye in sump  ->  Seneye cloud  ->  harvester (every 30 min)  ->  database
                                                                      |
                                                     JSON export -> dashboard
```

## What gets collected

Each Seneye sits in a sump, and a sump serves several tanks:

| System | Sumps | Tanks |
|---|---|---|
| A | SA12, SA345 | A1–A5 |
| B | SB12, SB34 | B1–B4 |
| C | SC12, SC34 | C1–C4 |
| D | SD12, SD345 | D1–D5 |
| E | SE12 | E1, E2 |

Per reading: temperature, pH, free ammonia (NH₃), and — on reef units — PAR, lux
and colour temperature, plus the device's own health flags (slide serial and
expiry date, out-of-water, disconnected). A reading describes the shared water
of the tanks on that sump, not an individual tank, and the dashboard says so.

## Setup

**1. Find your device ids**

```bash
export SENEYE_USER="you@example.com"
export SENEYE_PWD="..."
python -m harvester.harvest --dry-run
```

This prints one block per device on the account, including its id.

**2. Map ids to sumps** in `config.json`:

```json
"devices": {
  "40281": { "sump": "SA12" },
  "40295": { "sump": "SD345" }
}
```

The sump supplies the system letter and the tank list, so that is the only line
you need per device. If you move a Seneye to a different sump, change its entry
here — readings already stored keep the mapping they were collected under.

**3. Run it**

```bash
python -m harvester.harvest
```

Nothing to install: the harvester is standard library only on the default
SQLite backend. Python 3.9+.

**4. On GitHub Actions**

Add two repository secrets, `SENEYE_USER` and `SENEYE_PWD`. The workflow in
`.github/workflows/harvest.yml` polls every 30 minutes, commits the updated
database and JSON export back to the repo, and publishes `dashboard/` to GitHub
Pages. GitHub's scheduler is best-effort, so a run is occasionally late or
skipped; the harvester is idempotent, so that only ever costs the one reading
the API was holding at that moment.

## Putting the dashboard on the TNP website

`dashboard/` is self-contained: `index.html` plus `data/nursery.json`. Copy both
onto the site (keeping the relative path), or point an iframe at the Pages URL.
The page loads no libraries and stores nothing in the browser; the only network
request it makes is for its own JSON.

For a snapshot with the data baked in — no server, no fetch — run:

```bash
python tools/build_artifact.py --out build/nursery-snapshot.html
```

## Where the data lives

`DATABASE_URL` decides, and nothing else in the code changes:

| Value | Backend |
|---|---|
| unset | `sqlite:///data/nursery.db` (default, committed by the workflow) |
| `postgresql://user:pwd@host:5432/db` | PostgreSQL — needs `psycopg` or `psycopg2` |
| `mysql://user:pwd@host:3306/db` | MySQL/MariaDB — needs `mysql-connector-python` or `PyMySQL` |

Tables are created on first run; `sql/` holds the same schema written out for
review. Readings are keyed on `(device_id, reading_time)`, so re-polling the
same last reading never duplicates a row, and re-running a harvest is always
safe.

## The export the dashboard reads

`dashboard/data/nursery.json` holds daily statistics for the whole record
(`window_days`, default 365) and raw readings for the recent window
(`raw_days`, default 30), so the file the browser downloads does not grow
without limit. Both are set in `config.json`. `dashboard/data/readings.csv` is
the full raw export for anyone who wants the numbers rather than the charts.

Daily statistics are n, minimum, maximum, mean and sample standard deviation per
device per day per parameter. The dashboard's daily view draws the mean with a
±1 SD band and a min–max envelope.

## Working ranges

`config.json` gives each parameter a `band` (the nursery's working range, shaded
on the charts) and `hard` limits (a reading outside them is flagged critical).
These are operating set points for this nursery, not published tolerances for
*Posidonia oceanica* — edit them to match the protocol, and remember the
dashboard states them as TNP's own.

## Limitations worth knowing

- The public Seneye API serves the **last** reading only; there is no historical
  endpoint. The record therefore starts the day the harvester starts, and its
  resolution is the polling interval, not the device's own.
- Credentials are the Seneye account e-mail and password sent as query
  parameters. That is what the API offers. Keep them in Actions secrets or an
  environment file; never in `config.json`.
- Seneye publish this interface for hobbyist use with no support and no stated
  rate limit. The harvester makes one request per poll and backs off on errors.
- If per-reading resolution becomes worth having, the alternative is the Seneye
  Local Data Exchange (`github.com/seneye/LDE`): the SWS or Connect app POSTs a
  JWT-signed payload for every reading to a URL you host. That needs an
  always-on receiver at the nursery end, which is why this version polls the
  cloud instead.

## Development

```bash
python -m unittest discover -s tests -v   # unit tests
python tools/mock_data.py --days 120      # fill a database with plausible readings
python -m harvester.harvest --export-only # rebuild the JSON from the database
```

Mock rows carry `slide_serial` values beginning `MOCK-`; clear them with
`DELETE FROM readings WHERE slide_serial LIKE 'MOCK-%';`.

## Layout

```
harvester/    seneye.py (API client) · store.py (database) · export.py (JSON) · harvest.py (CLI)
dashboard/    index.html + data/
sql/          schema for PostgreSQL and MySQL
tools/        mock_data.py · build_artifact.py
tests/        unit tests
```
