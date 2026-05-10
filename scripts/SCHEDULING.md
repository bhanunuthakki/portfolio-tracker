# Daily data refresh — scheduling guide

The portfolio-tracker pulls fresh holdings + transactions every time
`portfolio_tracker.jobs.daily_refresh` runs. Without a schedule, **the
chart and risk metrics stop updating the day you stop running it
manually**. The Plaid 24-month transaction-retention window means data
older than that gets silently lost too — the snapshotter is your only
guarantee of preserving history.

## What runs

`python -m portfolio_tracker.jobs.daily_refresh` does, in order:

1. **Plaid snapshot** — `/investments/holdings/get` for every linked
   Plaid Item, writes today's `holdings_snapshots` rows. Idempotent.
2. **SnapTrade sync** — for each configured profile (`primary`,
   `spouse`), pulls today's holdings + 24 months of activity and writes
   to the same tables. Skipped silently if a profile isn't configured.
3. **`portfolio_values_daily` cache** — refreshes today's row from the
   updated holdings, so the chart endpoint serves the new value
   immediately.

It exits 0 on full success, nonzero on any step failure (but always
attempts every step — a Plaid 500 doesn't block SnapTrade).

A 14-day-old log lives under `scripts/logs/`.

## Option 1 — Windows Task Scheduler (your machine must be on)

This is the simplest path. Caveat: the job only runs when your computer
is awake and logged in (or set to "run whether logged on or not" — see
below). If you sleep your laptop overnight, the morning run gets
deferred until you wake it up.

### One-time setup

1. Open **Task Scheduler** (Win + R → `taskschd.msc`).
2. Right pane → **Create Basic Task**.
3. Name: `Portfolio tracker daily refresh`. Description: `Pulls daily
   holdings from Plaid + SnapTrade.`
4. Trigger: **Daily** → start time `06:00 AM` (or whenever you're
   comfortable hitting the brokerage APIs — pre-market is fine).
5. Action: **Start a program**.
   * Program/script: `C:\path\to\portfolio-tracker\scripts\run_daily_refresh.bat`
   * Start in: `C:\path\to\portfolio-tracker`
6. Finish → check **Open the Properties dialog** → OK.
7. In Properties:
   * **General** tab → "Run whether user is logged on or not." (Asks for
     your password once — stored locally in the Task Scheduler vault.)
   * **Conditions** tab → uncheck "Start the task only if the computer
     is on AC power" if you want it to run on battery.
   * **Settings** tab → check "Run task as soon as possible after a
     scheduled start is missed." This catches up after sleep/restarts.
8. Test it: right-click the task → Run. Check
   `scripts/logs/daily_refresh_<today>.log` to confirm it succeeded.

### Sanity-checks

* `Get-ScheduledTask -TaskName "Portfolio tracker daily refresh" | Get-ScheduledTaskInfo`
  shows last run time, last result, next run time.
* Last result `0x0` = success. Anything else, look at the log.

## Option 2 — Cloud VM (truly self-running, ~$5/month)

If you want it to run regardless of whether your laptop is awake, host
the backend on a small VPS and use cron. Smallest viable specs:

* **DigitalOcean** $4/mo droplet, **Hetzner** €4/mo CX11, **Linode** $5
  Nanode, or **AWS Lightsail** $3.50 instance — any will do. 1 vCPU, 1
  GB RAM, 25 GB SSD is plenty for SQLite + a daily refresh.
* SSH in, install Python 3.13 + git, clone the repo, copy the `.env`
  with your Plaid + SnapTrade credentials, run `pip install -e .` and
  `alembic upgrade head`.
* Add a crontab entry:
  ```
  0 11 * * * cd ~/portfolio-tracker && .venv/bin/python -m portfolio_tracker.jobs.daily_refresh >> scripts/logs/daily_refresh.log 2>&1
  ```
  (11:00 UTC ≈ 6 AM Central or 4 AM Pacific — pick your time.)
* Optional: run uvicorn under a systemd service so the dashboard is
  reachable from anywhere; tunnel through Tailscale or wireguard for
  privacy. Don't expose `:8000` to the public internet — the API has no
  auth.

## Option 3 — GitHub Actions (free, no server)

Limit: GitHub Actions can't keep your DB. You'd need to push the
SQLite file back to a private repo or to S3 after each run. Doable but
unwise — the `.env` secrets sit in GitHub Secrets, the SQLite has all
your holdings.

Skip this option unless you really don't want to maintain a VPS.

## Option 4 — Raspberry Pi at home

If you have a Pi, this is the cleanest "always-on" setup that keeps the
data physically on your network. Same crontab pattern as Option 2.
~$5–15/year electricity, no recurring cloud bill.

## "Does my computer need to be on?"

* **Option 1**: yes — your computer must be awake when the task fires
  (or shortly after, if you set "run as soon as possible after a
  scheduled start is missed").
* **Options 2 / 3 / 4**: no — the cron host runs it.

## Verifying it's actually running

A daily check that's worth doing for the first week:

```powershell
Get-ChildItem "C:\path\to\portfolio-tracker\scripts\logs" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 3 Name, LastWriteTime, Length
```

If the most recent log file is older than yesterday, the schedule is
broken. Look at the file content for the actual error.
