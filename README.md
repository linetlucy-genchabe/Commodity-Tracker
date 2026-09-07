# Commodity Tracker

A Django + PostgreSQL dashboard for MOH commodity/stock data, built for SCHMT, CHMT,
facility in-charge, and MEL lead users. This first cut implements **MOH 748**
end-to-end (upload → parse → dashboard → facility drilldown). MOH 721 and MOH S11
are registered as "coming soon" in the form switcher — see **Adding a new form**
below for how they slot in later without a redesign.

This matches the design draft you approved (the "Commodity Tracker" canvas):
header with a form switcher, cascading county/sub-county/ward/facility filters, KPI
tiles, a facility × commodity stock heatmap, a top-stockout ranked list, and a
days-out-of-stock trend chart.

## 1. Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Create a Postgres database and role (adjust names/password as you like):

```sql
CREATE USER cgd WITH PASSWORD 'cgd_dev_pw' CREATEDB;
CREATE DATABASE commodity_tracker OWNER cgd;
```

Copy `.env.example` to `.env` (or just export the same variables) and adjust if your
DB credentials differ from the defaults:

```bash
cp .env.example .env
```

Then:

```bash
python3 manage.py migrate
python3 manage.py seed_forms        # registers MOH 748 / 721 / S11 in the form switcher
python3 manage.py createsuperuser   # this is you — a superuser sees & can upload everything
python3 manage.py runserver
```

Log in at `/login/`, then **Upload** your MOH 748 monthly export.

### Optional: load the sample data I tested with

`sample_data_dump.sql` is a dump of this app's database as I left it after loading
your July 2026 Kisumu MOH 748 sample end-to-end (381 facilities, all 8 sub-counties,
795 commodity records) — no user accounts are included in it. To start from that
instead of empty:

```bash
psql -h 127.0.0.1 -U cgd -d commodity_tracker -f sample_data_dump.sql
python3 manage.py createsuperuser
```

## 2. Creating SCHMT / CHMT / facility in-charge / MEL lead accounts

As agreed, there's no self-service sign-up — you (a superuser) create every account
in **Django admin → Accounts → Users**, and set two things on each:

- **Role**: CHMT, SCHMT, Facility In-Charge, or MEL Lead.
- **Scope**: the one geography field that matches the role — County for CHMT/MEL
  Lead, Sub-County for SCHMT, Facility for Facility In-Charge. Leave the others
  blank.

A CHMT/SCHMT/Facility-In-Charge account only ever sees data inside its assigned
scope — the dashboard, the filters, and the facility drilldown all enforce this
server-side, not just by hiding UI. A **MEL Lead** account sees everything like a
CHMT for its county but, importantly, can also upload new monthly files — matching
what you said (you and the MEL lead both upload). Tick "Can upload" on any other
account you want to grant upload rights to without making them a MEL Lead.

A Django **superuser** (the "Permissions" section of the user form) bypasses all of
this and always sees and can upload everything, across every county — that's meant
for you and any future admins, not for SCHMT/CHMT users.

## 3. How the data model works (and why)

You chose **one table per form** over a shared cross-form table, so `MOH748Upload` /
`MOH748Record` are specific to this form and fully typed. To still get the "filter
for 748, filter for a county, see everything highlighted" experience you asked for
across forms, every form's parser also writes into one shared table,
`IndicatorSummary` (facility, period, commodity, value, red/amber/green status) —
that's what the dashboard's filter bar and heatmap actually query. Drilling into any
row still takes you to the real MOH748Record with all its native columns.
**MOH 721 / MOH S11 will follow the same pattern**: new model(s) shaped like that
form added to `core/models.py`, plus a parser function (its own file, alongside
`parsing.py`) that also writes into `IndicatorSummary` — the dashboard and filter
bar need no changes for that to work, and there's no new app to create.

Thresholds (the red/amber/green cutoffs) are **not hardcoded** — they live in the
`Threshold` model, editable in Django admin. Right now MOH 748's "Days Out of Stock"
threshold is seeded as a draft assumption (0 days = green, 1–6 = amber, 7+ = red,
flagged `is_draft`). Once you have MOH's real cutoffs, update that one row — no code
change, no redeploy.

Geography (`County` / `SubCounty` / `Ward` / `Facility`) is built to hold all four of
your counties, and gets populated automatically from whatever's in each upload — so
Busia, Bungoma, and Vihiga will appear the first time you upload data for them.

## 4. What I found testing against your sample file

Parsing your `MoH_748.xlsx` (July 2026, Kisumu) worked cleanly — every commodity
column was recognized (0 unclassified columns), 381 real facilities across all 8
sub-counties, 795 commodity records. Two things I flagged in an earlier pass turned
out to be parsing issues on my end, not data problems, and both are now fixed:

- **Sub-county-level aggregate rows** (e.g. a row named exactly "Kisumu East Sub
  County" or "Seme Sub County", not a real facility — a DHIS2 aggregate/unallocated
  org unit) are now detected and skipped during upload rather than counted as
  facilities. You'll see a short note for each skipped row on the upload result
  page if you want to double check them.
- **"Days out of stock" values above 31 days** turned out to be a column-mapping
  bug on my side, not your data — the real values top out at 31 (one full month),
  which is what you'd expect from a calendar-day metric.

## 5. Project layout

One Django app, one file per concern — no per-feature app split:

```
config/                     settings, root urls
core/
  models.py                 every model: geography, accounts, forms registry, MOH 748
  views.py                  every view, plus the dashboard query helpers and the upload form
  urls.py                   every URL pattern
  admin.py                  every admin registration
  parsing.py                MOH 748 Excel parser (kept separate — it's an ETL
                             concern, not a request/response one)
  management/commands/
    seed_forms.py            registers MOH 748 / 721 / S11 in the form switcher
templates/                  HTML templates (matches the design draft's palette/type)
static/css/                 dashboard.css
```

When MOH 721 / MOH S11 land, they add to these same five files (plus their own
`parsing_*.py`) rather than creating new apps.

## 6. Deploying to Railway

The repo is ready to push straight to GitHub and deploy — `Procfile`,
`requirements.txt` (now includes `gunicorn`, `whitenoise`, `dj-database-url`),
`.python-version`, and production-aware settings are already in place.

**Push to GitHub** (this folder is already a git repo with one commit):

```bash
git remote add origin https://github.com/<you>/commodity-tracker.git
git push -u origin main
```

**On Railway:**

1. New Project → Deploy from GitHub repo → pick this repo. Railway detects
   Python via `.python-version` and `requirements.txt` automatically.
2. Add a **Postgres** plugin to the project (New → Database → PostgreSQL).
   Railway injects `DATABASE_URL` into your app service automatically —
   `config/settings.py` reads it with no extra config from you.
3. On the app service, set these **Variables**:
   - `DJANGO_SECRET_KEY` — a long random string (Railway can generate one).
   - `DJANGO_DEBUG` — `False`.
   - `DJANGO_ALLOWED_HOSTS` — can leave unset; the app already trusts
     Railway's own `RAILWAY_PUBLIC_DOMAIN` automatically. Set this if you
     attach a custom domain, to that domain.
4. Deploy. The `Procfile`'s `web` command runs `migrate`, `collectstatic`,
   then starts `gunicorn` — so a fresh deploy sets up its own database
   schema with no manual step.
5. Once it's live, open a **Railway shell** (or `railway run`) on the app
   service and run:
   ```bash
   python manage.py createsuperuser
   ```
   That's you — from there, create SCHMT/CHMT/facility in-charge/MEL lead
   accounts the same way described in section 2, just against the live
   Postgres instead of your local one.

**Loading the sample data on Railway** (optional): connect to the Railway
Postgres from your machine with `psql "$DATABASE_URL" -f sample_data_dump.sql`
(get the connection string from the Postgres plugin's Variables tab), the
same way as the local instructions in section 1.

## 7. Not built yet (flagging rather than guessing)

- MOH 721 / MOH S11 ingestion — waiting on sample files with confirmed structure.
- Async cascading filters (county → sub-county reloads the page today rather than
  updating in place via JS) — works correctly, just not as slick as it could be.
- Facility name fuzzy-matching across uploads (the CHW Gaps dashboard's approach)
  — today's exact-match-within-ward is enough for a clean DHIS2 export like this
  one, but worth adding if a future form's facility names are messier.
