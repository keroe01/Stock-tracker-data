"""Runs on a schedule via GitHub Actions, independent of whether the local
app/PC is on — captures point-in-time-only data (DRAM/NAND spot prices,
analyst target price/recommendation, pre/post-market snapshots) that has
no free historical backfill, so a PC that's off when this data window
passes would otherwise lose it permanently.

Reads watchlist.json for the symbol list, writes/merges into the JSON
files under data/. The local desktop app pulls this repo and merges new
rows into its own Excel store separately.
"""

import datetime
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import kr_screener
import us_screener

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

DRAM_URL = "https://www.trendforce.com/price/dram/dram_spot"
NAND_URL = "https://www.trendforce.com/price/flash/flash_spot"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
QUOTE_SUMMARY_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"


def _load(name: str) -> list[dict]:
    path = DATA_DIR / name
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(name: str, rows: list[dict]):
    path = DATA_DIR / name
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.date.today().isoformat()


# ---- TrendForce DRAM/NAND spot prices ----


def _safe_float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _parse_spot_table(html: str, section_id: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    section = soup.select_one(f"#{section_id}")
    if section is None:
        return []
    table = section.select_one("table.price-table")
    if table is None:
        return []
    rows = []
    for tr in table.select("tbody tr"):
        cells = tr.select("td")
        if len(cells) < 6:
            continue
        item = cells[0].get_text(strip=True)
        try:
            avg = float(cells[5].get_text(strip=True))
        except ValueError:
            continue
        change_text = cells[6].get_text(strip=True)
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", change_text)
        change_percent = float(m.group(1)) if m else 0.0
        trend_span = cells[6].select_one("span")
        trend_classes = trend_span.get("class", []) if trend_span else []
        if "fall-trend" in trend_classes:
            change_percent = -abs(change_percent)
        rows.append(
            {
                "item": item,
                "daily_high": _safe_float(cells[1].get_text(strip=True)),
                "daily_low": _safe_float(cells[2].get_text(strip=True)),
                "session_avg": avg,
                "change_percent": change_percent,
            }
        )
    return rows


def fetch_dram_spot() -> list[dict]:
    try:
        resp = requests.get(DRAM_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return _parse_spot_table(resp.text, "dram_spot")
    except Exception:
        print("failed to fetch DRAM spot prices")
        return []


def fetch_nand_spot() -> list[dict]:
    try:
        resp = requests.get(NAND_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return _parse_spot_table(resp.text, "flash_spot")
    except Exception:
        print("failed to fetch NAND spot prices")
        return []


def collect_memory_spot():
    for sheet, fetch in (("dram.json", fetch_dram_spot), ("nand.json", fetch_nand_spot)):
        existing = _load(sheet)
        seen = {(r["item"], r["date"]) for r in existing}
        rows = fetch()
        added = 0
        for row in rows:
            key = (row["item"], today())
            if key in seen:
                continue
            existing.append({**row, "date": today(), "collected_at": now_iso()})
            seen.add(key)
            added += 1
        _save(sheet, existing)
        print(f"{sheet}: +{added} rows")


# ---- Yahoo Finance: target price / recommendation + pre/post market ----

_session = None
_crumb = None


def _get_session_and_crumb():
    global _session, _crumb
    if _session is not None and _crumb:
        return _session, _crumb
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get("https://fc.yahoo.com", timeout=10)
        crumb = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10).text
    except Exception:
        print("failed to fetch yahoo crumb")
        crumb = ""
    _session, _crumb = s, crumb
    return s, crumb


def _raw(field) -> float | None:
    return field.get("raw") if isinstance(field, dict) else None


def fetch_quote_summary(symbol: str, modules: str) -> dict | None:
    session, crumb = _get_session_and_crumb()
    try:
        resp = session.get(
            QUOTE_SUMMARY_URL.format(symbol=symbol),
            params={"modules": modules, "crumb": crumb},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()["quoteSummary"]["result"]
        return result[0] if result else None
    except Exception:
        print(f"failed to fetch quoteSummary for {symbol}")
        return None


def collect_target_price(watchlist: list[dict]):
    existing = _load("target_price.json")
    seen = {(r["symbol"], r["date"]) for r in existing}
    added = 0
    for stock in watchlist:
        symbol = stock["symbol"]
        key = (symbol, today())
        if key in seen:
            continue
        data = fetch_quote_summary(symbol, "financialData,recommendationTrend")
        if data is None:
            continue
        fd = data.get("financialData", {})
        target_mean = _raw(fd.get("targetMeanPrice"))
        if target_mean is None:
            # No analyst coverage at all (e.g. ETFs) — nothing to record.
            continue
        rec_trend = data.get("recommendationTrend", {}).get("trend", [])
        current = next((t for t in rec_trend if t.get("period") == "0m"), None)
        existing.append(
            {
                "symbol": symbol,
                "date": today(),
                "target_mean_price": target_mean,
                "target_high_price": _raw(fd.get("targetHighPrice")),
                "target_low_price": _raw(fd.get("targetLowPrice")),
                "num_analysts": _raw(fd.get("numberOfAnalystOpinions")),
                "recommendation_mean": _raw(fd.get("recommendationMean")),
                "recommendation_key": fd.get("recommendationKey"),
                "strong_buy": current.get("strongBuy") if current else None,
                "buy": current.get("buy") if current else None,
                "hold": current.get("hold") if current else None,
                "sell": current.get("sell") if current else None,
                "strong_sell": current.get("strongSell") if current else None,
                "collected_at": now_iso(),
            }
        )
        seen.add(key)
        added += 1
        time.sleep(0.5)
    _save("target_price.json", existing)
    print(f"target_price.json: +{added} rows")


def collect_prepost(watchlist: list[dict]):
    """Pre/post-market snapshots — only meaningful for US-listed tickers;
    Yahoo doesn't track this for KRX names (confirmed empty)."""
    existing = _load("prepost.json")
    # Keep at most one row per (symbol, date, market_state) — overwrite
    # with the freshest snapshot for that state instead of piling up near-
    # duplicate rows every run.
    by_key = {(r["symbol"], r["date"], r["market_state"]): r for r in existing}
    changed = 0
    for stock in watchlist:
        if stock.get("market") != "US":
            continue
        symbol = stock["symbol"]
        data = fetch_quote_summary(symbol, "price")
        if data is None:
            continue
        price = data.get("price", {})
        market_state = price.get("marketState", "")
        pre_price = _raw(price.get("preMarketPrice"))
        post_price = _raw(price.get("postMarketPrice"))
        if pre_price is None and post_price is None:
            continue
        row = {
            "symbol": symbol,
            "date": today(),
            "market_state": market_state,
            "regular_open": _raw(price.get("regularMarketOpen")),
            "regular_high": _raw(price.get("regularMarketDayHigh")),
            "regular_low": _raw(price.get("regularMarketDayLow")),
            "regular_price": _raw(price.get("regularMarketPrice")),
            "pre_market_price": pre_price,
            "pre_market_change_percent": _raw(price.get("preMarketChangePercent")),
            "post_market_price": post_price,
            "post_market_change_percent": _raw(price.get("postMarketChangePercent")),
            "collected_at": now_iso(),
        }
        by_key[(symbol, today(), market_state)] = row
        changed += 1
        time.sleep(0.5)
    _save("prepost.json", list(by_key.values()))
    print(f"prepost.json: {changed} snapshots updated")


# ---- 코스피200 / S&P500 저PER/저PBR/상승률 screeners ----


def collect_kr_screener():
    """Unlike the other collectors here, this is a full-replace snapshot
    (not an accumulating history) — each run's result fully supersedes the
    last, same as how the desktop app already persists its own local scan
    to the ScreenerResults sheet. Written straight in the shape
    screen_kr_market() returns (plus generated_at) so the desktop app can
    use it directly with no reshaping."""
    result = kr_screener.screen_kr_market()
    result["generated_at"] = now_iso()
    _save("screener_kr.json", result)
    total = sum(len(v) for k, v in result.items() if k != "generated_at")
    print(f"screener_kr.json: {total} entries across {len(result) - 1} lists")


def collect_us_screener():
    """S&P500 counterpart to collect_kr_screener() — same full-replace
    snapshot shape, written to its own file so the desktop app can load
    either market independently."""
    result = us_screener.screen_us_market()
    result["generated_at"] = now_iso()
    _save("screener_us.json", result)
    total = sum(len(v) for k, v in result.items() if k != "generated_at")
    print(f"screener_us.json: {total} entries across {len(result) - 1} lists")


def main():
    with open(ROOT / "watchlist.json", encoding="utf-8") as f:
        watchlist = json.load(f)
    collect_memory_spot()
    collect_target_price(watchlist)
    collect_kr_screener()
    collect_us_screener()
    # collect_prepost(watchlist) — no longer collected; the desktop app's
    # 시간외 feature that consumed this was removed as not useful in
    # practice, so this is now dead weight rather than something worth
    # spending API calls to keep growing.


if __name__ == "__main__":
    main()
