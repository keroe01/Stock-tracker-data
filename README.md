# Stock Tracker — cloud data collector

Runs on a GitHub Actions schedule, independent of whether the desktop app
or the PC it runs on is on. Captures data that has no free historical
backfill — DRAM/NAND spot prices, analyst target price/recommendation,
and US pre/post-market snapshots — so a PC that's off when that window
passes doesn't lose it permanently.

- `watchlist.json` — symbols to track. Kept in sync with the desktop
  app's watchlist manually for now.
- `data/*.json` — accumulated results, one file per data type.
- `collect.py` — the collector itself; also runnable locally
  (`python collect.py`) for testing.
- `.github/workflows/collect.yml` — the schedule (every 2 hours, UTC).

The desktop app (separate repo) pulls this repo and merges new rows into
its own Excel store.
