# Stock Tracker — cloud data collector

Runs on a GitHub Actions schedule, independent of whether the desktop app
or the PC it runs on is on.

- DRAM/NAND spot prices, analyst target price/recommendation — captured
  because they have no free historical backfill, so a PC that's off when
  that window passes would otherwise lose them permanently. Accumulating
  history (`data/dram.json`, `data/nand.json`, `data/target_price.json`).
- 코스피200 저PER/저PBR/상승률 screener (`kr_screener.py`, same logic as
  the desktop app's `app/services/kr_screener.py`) — captured here for
  speed (running the scan on a GitHub Actions runner instead of the
  user's own PC avoids the GIL contention it caused with the desktop
  app's Qt event loop) and scoped to 코스피200 (not the whole ~960-name
  KOSPI market) so thin/illiquid micro-caps don't dominate the
  저PER/상승률 lists. Constituent codes come from Naver's public
  entryJongmok.naver listing — full KRX300 membership isn't available
  for free without a KRX data-portal login. Full-replace snapshot, not
  history (`data/screener_kr.json`).
- S&P500 counterpart (`us_screener.py`) — same ranking logic, sourced
  from Yahoo Finance quoteSummary per ticker. Constituent list comes
  from SPDR's public SPY holdings file (an ETF that tracks the S&P 500)
  since there's no free bulk "whole index" listing endpoint the way
  Naver provides for KOSPI. Full-replace snapshot (`data/screener_us.json`).

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
target-price rows into its own Excel store, and uses `screener_kr.json`/
`screener_us.json` directly in place of running its own local scan when
available.
