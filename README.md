# Stock Tracker — cloud data collector

Runs on a GitHub Actions schedule, independent of whether the desktop app
or the PC it runs on is on.

- DRAM/NAND spot prices, analyst target price/recommendation — captured
  because they have no free historical backfill, so a PC that's off when
  that window passes would otherwise lose them permanently. Accumulating
  history (`data/dram.json`, `data/nand.json`, `data/target_price.json`).
- KOSPI 저PER/저PBR/상승률 screener (`kr_screener.py`, same logic as the
  desktop app's `app/services/kr_screener.py`) — captured here mainly for
  speed: it's a ~1 minute scan of the whole KOSPI market, and running it
  on a GitHub Actions runner instead of the user's own PC avoids the GIL
  contention that scan caused with the desktop app's Qt event loop.
  Full-replace snapshot, not history (`data/screener.json`).

This repo is public — it only ever holds market-data snapshots (prices,
screener results), nothing account-specific — so the desktop app (and any
copy of it, even one without cached git credentials) can always pull it.

- `watchlist.json` — symbols to track. Kept in sync with the desktop
  app's watchlist manually for now.
- `data/*.json` — one file per data type; see above for which accumulate
  history vs. get fully replaced each run.
- `collect.py` — the collector itself; also runnable locally
  (`python collect.py`) for testing.
- `.github/workflows/collect.yml` — the schedule (every 2 hours, UTC).

The desktop app (separate repo) pulls this repo: merges new DRAM/NAND/
target-price rows into its own Excel store, and uses `screener.json`
directly in place of running its own local scan when available.
