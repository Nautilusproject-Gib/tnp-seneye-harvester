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

Per Seneye reading: temperature, pH and free ammonia (NH₃), plus the device's own
health flags (slide serial and expiry date, out-of-water, disconnected). A reading
describes the shared water of the tanks on that sump, not an individual tank, and
the dashboard says so.

The light metrics the reef units also report (PAR, lux, colour temperature) are
not collected. The probes sit in the sumps rather than in the lit tanks, so they
only ever read zero.

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

## Modelled values

Two figures on the dashboard are calculated rather than measured. Both are
labelled "modelled" on their tab and carry a note on the chart explaining what
they are. Neither is ever written into the same field as a measurement, and both
are recomputed on every export, so changing the model needs no re-harvesting.
`config.json` has a `derived` block to switch either off.

**Ammonium (NH₄⁺)** is a genuine derivation. Free ammonia and ammonium are two
sides of one equilibrium, so given the measured NH₃, pH and temperature, plus
salinity, the rest of the total ammonia pool follows:

```
I   = 19.973 S / (1000 - 1.2005109 S)
pKa = 0.0901821 + 2729.92/(T+273.2) + (0.1552 - 0.0003142 T) I
f   = 1/(10^(pKa - pH) + 1)              fraction present as free NH3
```

Total ammonia is NH₃/f and ammonium is the remainder, converted by molar mass.
At seawater pH only about 3% of the pool is free ammonia, so NH₄⁺ comes out far
larger than NH₃, which is expected rather than a fault. The fit is valid for
5–35 ppt, 5–35 °C and pH 7.8–8.3; outside that the value is withheld rather than
extrapolated.

**Oxygen at saturation** is *not* dissolved oxygen. It is how much oxygen the
water could hold at the measured temperature and the salinity in use, at
sea-level pressure. Real DO sits below this whenever respiration outpaces
exchange, and measuring it needs a probe. It is shown as a ceiling and the
dashboard says so in as many words.

```
DO0 = exp(-139.34411 + 1.575701e5/T - 6.642308e7/T^2
          + 1.243800e10/T^3 - 8.621949e11/T^4)     mg/L, fresh water, 1 atm
Fs  = exp(-S (0.017674 - 10.754/T + 2140.7/T^2))   salinity correction
DO  = DO0 * Fs                                     T in kelvin
```

Checked against the published tables: 9.08 mg/L at 20 °C fresh, 7.38 at 20 °C
and S=35, both matched to within 0.03 mg/L by the unit tests.

**Salinity** for both comes from the most recent in-situ sample for that sump.
Where a sump has no sample yet, `derived.default_salinity` is used. It is never
invented per reading, and both models return nothing rather than guess when an
input is missing.

**Sources**

Benson, B.B. and Krause, D. (1984) 'The concentration and isotopic fractionation
of oxygen dissolved in freshwater and seawater in equilibrium with the
atmosphere', *Limnology and Oceanography*, 29(3), pp. 620–632. Equation as
adopted by the U.S. Geological Survey, *Office of Water Quality Technical
Memorandum 2011.03*.

Bell, T.G., Johnson, M.T., Jickells, T.D. and Liss, P.S. (2007)
'Ammonia/ammonium dissociation coefficient in seawater: a significant numerical
correction', *Environmental Chemistry*, 4(3), pp. 183–186.
doi:10.1071/EN07032.

Florida Department of Environmental Protection, *Calculation of un-ionized
ammonia in fresh and saline water*, standard operating procedure, after
Whitfield (1974) and Bower and Bidwell (1978).

A caveat carried from Bell et al. (2007): expressions of this family, derived
from Khoo et al. (1977), can overstate free NH₃ under some conditions. Since the
harvester works the other way, from measured NH₃ to ammonium, an overstated pKa
would understate ammonium. Treat the NH₄⁺ figure as indicative.

## In-situ samples

The nursery is also sampled by hand at the sumps every week or two, with
handheld meters and test kits: nitrate, nitrite, phosphate, salinity, carbonate
hardness, calcium, magnesium, plus spot temperature and pH.

These are a different measurement method from the Seneye probes, on different
instruments with their own accuracy and resolution. They are stored in their own
table and shown as their own thing. Where both measure the same quantity the
hand sample is drawn on the sensor chart as a hollow diamond, for comparison
only: the two are never averaged, and a difference between them is two
instruments measuring, not an error to reconcile. The wording the dashboard uses
to say so lives in `config.json` under `nutrients.method_note`, so you can put it
in your own words.

### Reading the sheet

The harvester fetches the Google Sheet itself on every run. Set it up once:

1. In the sheet: **File, Share, Publish to web**. Choose the **Nutrient** tab and
   **Comma-separated values (.csv)**, then Publish.
2. Put the sheet's URL in `config.json` under `nutrients.sheet_url`. Either the
   published link or the ordinary `/edit` URL works; the harvester rewrites it to
   the CSV endpoint.

After that, anything typed into the sheet is on the dashboard within half an
hour. Nothing to export, nothing to commit.

Publishing makes that tab readable by anyone with the link. If the sampling data
has to stay private, the alternative is a Google service account with the sheet
shared to it and its key in an Actions secret, which is more setup; the code
would need a `google-auth` dependency.

### What happens when the fetch fails

Nothing destructive. Samples already in the database are left exactly as they
are, so an outage or an unpublished sheet shows as a stale date on the dashboard
rather than an empty table. If a `data/nutrients.xlsx` is present it is used as a
fallback. Every successful fetch is also written to `data/nutrients_latest.csv`
and committed, which gives the repo a dated record of what the sheet said at the
time.

### What the parser copes with

Columns are found by their headings rather than their position, so inserting a
column in the sheet will not silently shift every value one field across.
Beyond that: the date entered once at the top of each sampling block and left
blank below it, dates both as real dates and as typed text like `21/9/26`, `N/A`
for anything not measured that round, blank separator rows, and the empty first
column. Rows are matched to sumps by the `Tank ID` column, where `A12` means
sump `SA12`, and keyed on date plus sump, so a corrected sheet overwrites rather
than duplicates. An analyte with no values anywhere is dropped from the export
rather than shown as an empty chart.

### Reference ranges and colouring

Each in-situ value is compared with ordinary local seawater and coloured on
three levels: green inside the typical range, amber between typical and the
outer bound, red beyond it. Colour is not the only cue — amber cells carry a
triangle and red cells a square, so the table still reads in greyscale.

The ranges live in `config.json` under `insitu_reference`, each with the basis
it was derived from. They describe **seawater**, not what *Posidonia* requires,
and a red cell is a prompt to check the reading and the test kit rather than
evidence of a problem.

| Analyte | Typical | Outer | Derived from |
|---|---|---|---|
| Temperature | 13–20 °C | 11–22 | TNP nursery set points, not a seawater range |
| Salinity | 35–38.5 ppt | 32–41 | Atlantic inflow ~36.2 to Mediterranean outflow ~38.4 at the Strait |
| pH | 8.0–8.2 | 7.5–8.5 | surface ocean ~8.1; outer bounds are the nursery set points |
| Carbonate hardness | 6.8–8.0 °dKH | 5–11 | Mediterranean surface alkalinity |
| Nitrate | 0–0.25 mg/L | 0–1.0 | western Mediterranean surface nitrate |
| Nitrite | 0–0.05 mg/L | 0–0.2 | upper-ocean nitrite |
| Ammonia | 0–0.05 mg/L | 0–0.2 | surface ammonium, a few µmol/L at most |
| Phosphate | 0–0.05 ppm | 0–0.3 | western Mediterranean surface phosphate |
| Calcium | 400–455 ppm | 360–520 | 412 ppm at S=35, scaled with salinity |
| Magnesium | 1250–1420 ppm | 1150–1600 | 1290 ppm at S=35, scaled with salinity |

**The conversions**, so the numbers can be checked rather than taken on trust:

- Alkalinity to dKH: 2600 µmol/kg × 1.027 kg/L ÷ 1000 = 2.67 meq/L; ÷ 0.3566 =
  7.5 °dKH. The 2500–2650 µmol/kg range gives 7.2–7.6 °dKH.
- Nutrients from µmol/L to mg/L as the ion: NO₃ × 62, NO₂ × 46, PO₄ × 95,
  NH₄ × 18, all ÷ 1000. So 4 µmol/L nitrate = 0.25 mg/L, and 0.16 µmol/L
  phosphate = 0.015 mg/L.
- Major ions scale with salinity: Ca 412 and Mg 1290 at S=35 become ~452 and
  ~1415 at S=38.4.
- Phosphate, calcium and magnesium are read in ppm. In seawater 1 ppm is about
  1.03 mg/L, because a litre weighs roughly 1.026 kg, so the same figures serve
  for both units and the harvester converts nothing.

**Sources**

Belgacem, M., Schroeder, K., Barth, A., Troupin, C., Pavoni, B., Raimbault, P.,
Garcia, N., Borghini, M. and Chiggiato, J. (2021) 'Climatological distribution
of dissolved inorganic nutrients in the western Mediterranean Sea (1981–2017)',
*Earth System Science Data*, 13, pp. 5915–5949. doi:10.5194/essd-13-5915-2021.

Gemayel, E., Hassoun, A.E.R., Benallal, M.A., Goyet, C., Rivaro, P.,
Abboud-Abi Saab, M., Krasakopoulou, E., Touratier, F. and Ziveri, P. (2015)
'Climatological variations of total alkalinity and total dissolved inorganic
carbon in the Mediterranean Sea surface waters', *Earth System Dynamics*, 6,
pp. 789–800. doi:10.5194/esd-6-789-2015.

Zakem, E.J., Al-Haj, A., Church, M.J., van Dijken, G.L., Dutkiewicz, S.,
Foster, S.Q., Fulweiler, R.W., Mills, M.M. and Follows, M.J. (2018)
'Ecological control of nitrite in the upper ocean', *Nature Communications*, 9,
1206. doi:10.1038/s41467-018-03553-w.

Major-ion concentrations at S=35 follow the standard seawater composition
reported in oceanographic reference texts and summarised by the Global Seafood
Alliance, *Typical chemical characteristics of full-strength seawater*.

Two caveats worth keeping in view. The ammonia row assumes the kit reports
total ammonia; if it reports free NH₃ the typical range should be an order of
magnitude lower. And the nursery is a closed system on collected seawater, so
nitrate and phosphate can legitimately sit above open-water values without
anything being wrong.

## Maintenance and issue tracking

A hub for anyone working at the nursery: faults people find, and the planned
jobs that fall due. Two tabs in the maintenance sheet, read the same way as the
sampling data.

**Maintenance** - one row per fault:

| Issue ID | Date | Time | System / Sump | Equipment | Fault / problem | Severity | Status | Assigned to | Action taken | Date resolved | Reported by | Notes |

**Schedule** - one row per recurring job:

| Task ID | Task | System / Sump | Equipment | Frequency (days) | Last done | Done by | Next due | Notes |

`templates/Nautilus_Maintenance_template.xlsx` has both tabs with the headings
and one example row. Import it into the maintenance sheet, delete the examples,
and publish each tab to the web as CSV as you did for the sampling data. Put
each tab's URL (or its `gid`) in `config.json` under `maintenance`.

### How the state is worked out

Status is read from the sheet and mapped onto open, in progress and resolved.
People type all sorts, so `closed`, `done`, `fixed` and `complete` all count as
resolved, `WIP`, `started` and `awaiting parts` as in progress. Anything
unrecognised is kept as written and treated as outstanding, so a typo never
hides a fault from the board. A row with a resolution date but no status counts
as resolved; a row with neither counts as open. A row with no description is not
an issue at all and is skipped, which is what lets blank separator rows through.

Severity is optional. Fill it in and the board sorts by it; leave it out and
everything sorts by age.

For planned jobs, next due comes from the sheet when it is filled in, otherwise
from last done plus the frequency. A job with neither is listed with no due date
rather than a guessed one. Overdue jobs sort to the top.

The sheet is the record of truth, so each refresh replaces the tables wholesale:
delete a row in the sheet and it disappears from the board. A tab that cannot be
read is left alone rather than emptied.

### Where the board goes

The board is a page of its own at `board/index.html`, reading
`board/data/maintenance.json`. It names the people who reported and carried out
work, so by default it is **neither committed nor published**: `board/data/` is
in `.gitignore`, and the Pages workflow only uploads `dashboard/`.

That default exists because the repo is public. Three ways to give the team
access to it:

1. **A separate private repo** with Pages, once the free nonprofit Team plan is
   in place. Pages on a private repo needs a paid or nonprofit plan. This is the
   only option that is genuinely private and still a live URL.
2. **Anonymised on the public site.** Set `maintenance.anonymise` to true and
   `publish` to true. Faults, status and due dates become public; who reported
   and who fixed them are stripped from the export.
3. **Keep it in Google.** Everyone who can see the sheet can see the log. No
   board, but no setup either.

Until you pick one, the board is generated locally by a harvest run and can be
opened straight from disk.

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
harvester/    seneye.py (API client) · nutrients.py (sheet + workbook reader)
              maintenance.py (issues + planned jobs) · store.py (database)
              export.py (JSON) · harvest.py (CLI)
dashboard/    index.html + data/   (public)
board/        index.html + data/   (internal maintenance board, not published)
templates/    maintenance sheet template
sql/          schema for PostgreSQL and MySQL
tools/        mock_data.py · build_artifact.py
tests/        unit tests
```
