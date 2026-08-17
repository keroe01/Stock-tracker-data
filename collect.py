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


# ---- Cloud/AI-infra tracked companies: revenue, FCF, RPO/backlog ----
# Ported from the desktop app's app/services/cloud_data.py + sec_edgar.py —
# this used to be a live fetch the app ran itself on every start (Yahoo
# revenue + 5x SEC EDGAR XBRL round trips), which is what made the
# 클라우드 tab visibly slow to populate. Same idea as DRAM/NAND/target
# price above: collect it here on a schedule instead, so the desktop app
# just reads an already-fresh JSON snapshot.

SEC_HEADERS = {"User-Agent": "StockTrackerApp (personal project) contact@example.invalid"}
_SEC_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{concept}.json"

# Meta was requested too, but excluded: it doesn't disclose a backlog/RPO
# figure the way the other 5 do.
CLOUD_TICKERS = [
    {"symbol": "MSFT", "name": "Microsoft"},
    {"symbol": "AMZN", "name": "Amazon"},
    {"symbol": "GOOGL", "name": "Alphabet"},
    {"symbol": "CRWV", "name": "CoreWeave"},
    {"symbol": "NBIS", "name": "Nebius"},
]

CIK_BY_SYMBOL = {
    "MSFT": "0000789019",
    "AMZN": "0001018724",
    "GOOGL": "0001652044",
    "CRWV": "0001769628",
    "NBIS": "0001513845",
}

# Amazon stopped tagging its RPO in structured XBRL data around 2020 and
# has no dimensional/custom extension tag standing in for it either — its
# current AWS-specific backlog is disclosed only as prose in the 10-Q, not
# machine-readable, so it's excluded here and stays whatever was last
# manually researched into the app's own CloudBacklog sheet.
RPO_AUTO_SYMBOLS = ("MSFT", "GOOGL", "CRWV", "NBIS")

_OCF_CONCEPT = "NetCashProvidedByUsedInOperatingActivities"
_CAPEX_CONCEPTS = ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")
_RPO_CONCEPT = "RevenueRemainingPerformanceObligation"


def _fetch_sec_concept(cik: str, concept: str) -> list[dict]:
    try:
        resp = requests.get(_SEC_CONCEPT_URL.format(cik=cik, concept=concept), headers=SEC_HEADERS, timeout=15)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json().get("units", {}).get("USD", [])
    except Exception:
        print(f"failed to fetch SEC XBRL concept {concept} for CIK {cik}")
        return []


def _derive_quarterly_series(facts: list[dict]) -> list[dict]:
    """Not every company tags a genuine standalone-quarter duration fact
    for every quarter (Microsoft's 10-K never reports a lone Q4;
    CoreWeave's 10-Qs report cumulative year-to-date figures). Grouping by
    each fact's exact `start` date and differencing consecutive
    cumulative end-of-period values recovers each standalone quarter
    regardless of which reporting style a company uses."""
    by_start: dict[str, dict[str, tuple[float, str]]] = {}
    for f in facts:
        start, end, val, filed = f.get("start"), f.get("end"), f.get("val"), f.get("filed", "")
        if not start or not end or val is None:
            continue
        group = by_start.setdefault(start, {})
        existing = group.get(end)
        if existing is None or filed >= existing[1]:
            group[end] = (val, filed)

    out: dict[str, float] = {}
    out_filed: dict[str, str] = {}
    for start, ends in by_start.items():
        prev_val = 0.0
        for end, (val, filed) in sorted(ends.items(), key=lambda kv: kv[0]):
            quarterly = val - prev_val
            prev_val = val
            if end not in out or filed >= out_filed[end]:
                out[end] = quarterly
                out_filed[end] = filed
    return [{"end": end, "val": val} for end, val in sorted(out.items())]


def fetch_quarterly_fcf(symbol: str, quarters: int = 4, max_age_days: int = 370) -> list[dict]:
    """Single-quarter FCF points from roughly the last year:
    [{"quarter_end", "fcf"}], computed as operating cash flow minus capex."""
    cik = CIK_BY_SYMBOL.get(symbol)
    if cik is None:
        return []
    ocf = _derive_quarterly_series(_fetch_sec_concept(cik, _OCF_CONCEPT))
    capex_by_end: dict[str, float] = {}
    for concept in _CAPEX_CONCEPTS:
        for f in _derive_quarterly_series(_fetch_sec_concept(cik, concept)):
            capex_by_end.setdefault(f["end"], f["val"])
    out = []
    for f in ocf:
        end = f["end"]
        if end not in capex_by_end:
            continue
        out.append({"quarter_end": end, "fcf": f["val"] - capex_by_end[end]})
    out = out[-quarters:]
    cutoff = datetime.date.today() - datetime.timedelta(days=max_age_days)
    return [f for f in out if datetime.date.fromisoformat(f["quarter_end"]) >= cutoff]


def fetch_rpo_history(symbol: str, quarters: int = 4) -> list[dict]:
    """Last `quarters` disclosed RPO/backlog points: [{"quarter_end",
    "rpo"}]. Amazon returns nothing (see RPO_AUTO_SYMBOLS)."""
    cik = CIK_BY_SYMBOL.get(symbol)
    if cik is None:
        return []
    facts = _fetch_sec_concept(cik, _RPO_CONCEPT)
    by_end: dict[str, dict] = {}
    for f in facts:
        end = f.get("end")
        if not end:
            continue
        existing = by_end.get(end)
        if existing is None or f.get("filed", "") >= existing.get("filed", ""):
            by_end[end] = f
    points = sorted(by_end.values(), key=lambda f: f["end"])
    return [{"quarter_end": f["end"], "rpo": f["val"]} for f in points[-quarters:]]


def collect_cloud_growth():
    """Full-replace snapshot (like the screeners below), not an
    accumulating history — each run's result fully supersedes the last.
    Revenue comes from Yahoo (same as before); FCF/RPO from SEC EDGAR XBRL,
    the only source with genuine per-quarter history now that Yahoo's
    quoteSummary dropped real quarterly cash-flow/balance-sheet data."""
    rows = []
    for t in CLOUD_TICKERS:
        symbol = t["symbol"]
        row = {
            "symbol": symbol,
            "name": t["name"],
            "last_fy_revenue": None,
            "last_fy_end": None,
            "latest_q_revenue": None,
            "latest_q_end": None,
            "fcf_history": fetch_quarterly_fcf(symbol),
            "rpo_history": fetch_rpo_history(symbol) if symbol in RPO_AUTO_SYMBOLS else [],
        }
        data = fetch_quote_summary(symbol, "incomeStatementHistory,incomeStatementHistoryQuarterly")
        if data is not None:
            ish = data.get("incomeStatementHistory", {}).get("incomeStatementHistory", [])
            ishq = data.get("incomeStatementHistoryQuarterly", {}).get("incomeStatementHistory", [])
            last_fy = ish[0] if ish else {}
            last_q = ishq[0] if ishq else {}
            row.update(
                last_fy_revenue=_raw(last_fy.get("totalRevenue")),
                last_fy_end=(last_fy.get("endDate") or {}).get("fmt"),
                latest_q_revenue=_raw(last_q.get("totalRevenue")),
                latest_q_end=(last_q.get("endDate") or {}).get("fmt"),
            )
        rows.append(row)
        time.sleep(0.5)
    _save("cloud_growth.json", {"rows": rows, "generated_at": now_iso()})
    print(f"cloud_growth.json: {len(rows)} companies")


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


def collect_nasdaq100_screener():
    """나스닥100 counterpart — narrower/more tech-heavy than S&P500, added
    per request since S&P500 alone let too many small-caps into the
    lists."""
    result = us_screener.screen_nasdaq100_market()
    result["generated_at"] = now_iso()
    _save("screener_nasdaq100.json", result)
    total = sum(len(v) for k, v in result.items() if k != "generated_at")
    print(f"screener_nasdaq100.json: {total} entries across {len(result) - 1} lists")


def main():
    with open(ROOT / "watchlist.json", encoding="utf-8") as f:
        watchlist = json.load(f)
    collect_memory_spot()
    collect_target_price(watchlist)
    collect_cloud_growth()
    collect_kr_screener()
    collect_us_screener()
    collect_nasdaq100_screener()
    # collect_prepost(watchlist) — no longer collected; the desktop app's
    # 시간외 feature that consumed this was removed as not useful in
    # practice, so this is now dead weight rather than something worth
    # spending API calls to keep growing.


if __name__ == "__main__":
    main()
