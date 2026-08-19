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
import os
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


# ---- 10Y/30Y government bond yields (US/KR/JP/CN) ----
# Each country's data comes straight from its own official source, not a
# financial-data reseller — the same "primary source over aggregator"
# reasoning as the DRAM/NAND scraper above and SEC EDGAR in the desktop
# app's cloud_data.py.

_ECOS_API_KEY = os.environ.get("ECOS_API_KEY", "sample")
_BOND_HISTORY_YEARS = 3


def _bond_cutoff_date() -> str:
    """Same calendar cutoff for every country, so all three lines start at
    the same point on a date-based x-axis — trimming each country to "the
    last N rows" instead left them starting on different dates, since each
    market has its own trading-day calendar (holidays, weekends)."""
    d = datetime.date.today()
    try:
        cutoff = d.replace(year=d.year - _BOND_HISTORY_YEARS)
    except ValueError:
        cutoff = d.replace(month=2, day=28, year=d.year - _BOND_HISTORY_YEARS)
    return cutoff.isoformat()


def _fetch_us_treasury_year(year: int) -> list[dict]:
    resp = requests.get(
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
        f"/{year}/all",
        params={"type": "daily_treasury_yield_curve", "field_tdr_date_value": year},
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    lines = resp.text.strip().splitlines()
    header = [h.strip().strip('"') for h in lines[0].split(",")]
    idx_10y, idx_30y = header.index("10 Yr"), header.index("30 Yr")
    out = []
    for line in lines[1:]:
        cells = [c.strip().strip('"') for c in line.split(",")]
        m, d, y = cells[0].split("/")
        iso_date = f"{y}-{int(m):02d}-{int(d):02d}"
        for maturity, idx in (("10Y", idx_10y), ("30Y", idx_30y)):
            val = cells[idx]
            if val:
                out.append({"country": "US", "maturity": maturity, "date": iso_date, "yield": float(val)})
    return out


def fetch_us_treasury_yields() -> list[dict]:
    """US Treasury's own official daily par yield curve — 10 Yr/30 Yr
    columns directly. The CSV is served one calendar year per request, so
    covering _BOND_HISTORY_YEARS back means fetching one request per
    calendar year in that span, then filtering to the shared cutoff date
    (see _bond_cutoff_date) so the US line starts on the same date as
    Japan/Korea's rather than at some arbitrary trading-day count."""
    cutoff = _bond_cutoff_date()
    this_year = datetime.date.today().year
    start_year = int(cutoff[:4])
    rows: list[dict] = []
    for year in range(start_year, this_year + 1):
        try:
            rows.extend(_fetch_us_treasury_year(year))
        except Exception:
            print(f"failed to fetch US Treasury yields for {year}")
    if not rows:
        return []
    rows = [r for r in rows if r["date"] >= cutoff]
    rows.sort(key=lambda r: r["date"])
    return rows


_JP_ERA_OFFSETS = {"M": 1867, "T": 1911, "S": 1925, "H": 1988, "R": 2018}


def _parse_japanese_era_date(s: str) -> str | None:
    """'R8.7.30' (令和8年7月30日) -> '2026-07-30'. Only the eras that can
    actually appear in this file's most recent rows matter in practice,
    but all five are handled since they're free to support."""
    s = s.strip()
    if not s or s[0] not in _JP_ERA_OFFSETS:
        return None
    try:
        year_part, month, day = s[1:].split(".")
        year = _JP_ERA_OFFSETS[s[0]] + int(year_part)
        return f"{year:04d}-{int(month):02d}-{int(day):02d}"
    except Exception:
        return None


def _fetch_mof_csv(url: str) -> list[dict]:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = "shift_jis"
    lines = resp.text.strip().splitlines()
    header = lines[1].split(",")
    idx_10y, idx_30y = header.index("10年"), header.index("30年")
    out = []
    for line in lines[2:]:
        cells = line.split(",")
        iso_date = _parse_japanese_era_date(cells[0])
        if iso_date is None:
            continue
        for maturity, idx in (("10Y", idx_10y), ("30Y", idx_30y)):
            val = cells[idx].strip()
            if val and val != "-":
                out.append({"country": "JP", "maturity": maturity, "date": iso_date, "yield": float(val)})
    return out


def fetch_japan_jgb_yields() -> list[dict]:
    """Japan's Ministry of Finance official daily JGB yield data — every
    maturity from 1年 to 40年, back to 1974.

    jgbcm_all.csv (the full-history archive) turns out to lag ~3 weeks
    behind today — it's a batch-updated archive, not the live file. MOF
    also publishes jgbcm.csv, a short current-month-only file that's kept
    current to within a day or two. Fetch both and let the current-month
    file's rows win on any overlapping date, then filter to the shared
    cutoff date (see _bond_cutoff_date) so this line starts on the same
    date as the US/Korea ones."""
    try:
        rows = _fetch_mof_csv("https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv")
    except Exception:
        print("failed to fetch Japan JGB yields (all-history file)")
        rows = []
    try:
        recent = _fetch_mof_csv("https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv")
    except Exception:
        recent = []
    if not rows and not recent:
        return []
    by_key = {(r["country"], r["maturity"], r["date"]): r for r in rows}
    by_key.update({(r["country"], r["maturity"], r["date"]): r for r in recent})
    cutoff = _bond_cutoff_date()
    merged = [r for r in by_key.values() if r["date"] >= cutoff]
    merged.sort(key=lambda r: r["date"])
    return merged


# Bank of Korea's own ECOS system, stat code 817Y002 (시장금리, 일별) —
# 국고채(10년)/국고채(30년) are separate item codes under it.
_ECOS_ITEM_CODES = {"10Y": "010210000", "30Y": "010230000"}


def fetch_korea_bond_yields() -> list[dict]:
    """Requires a real (free, self-registered) ECOS API key to pull more
    than 10 rows per call — falls back to the public "sample" key, which
    works but is capped, for local testing without one configured.

    Queried directly by the shared cutoff date (see _bond_cutoff_date), so
    this line starts on the same date as the US/JP ones. `count` is capped
    well above _BOND_HISTORY_YEARS worth of KRX trading days so the date
    range is never truncated by the row limit instead."""
    out = []
    today_str = today().replace("-", "")
    cutoff = _bond_cutoff_date()
    start_str = cutoff.replace("-", "")
    count = 10 if _ECOS_API_KEY == "sample" else 2000
    for maturity, item_code in _ECOS_ITEM_CODES.items():
        try:
            resp = requests.get(
                f"https://ecos.bok.or.kr/api/StatisticSearch/{_ECOS_API_KEY}/xml/kr/1/{count}/817Y002/D/"
                f"{start_str}/{today_str}/{item_code}",
                headers=HEADERS,
                timeout=20,
            )
            resp.raise_for_status()
            rows = []
            for m in re.finditer(r"<TIME>(\d{8})</TIME>\s*<DATA_VALUE>([\d.]+)</DATA_VALUE>", resp.text):
                date_raw, val = m.groups()
                iso_date = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}"
                rows.append({"country": "KR", "maturity": maturity, "date": iso_date, "yield": float(val)})
            rows.sort(key=lambda r: r["date"])
            out.extend(rows)
        except Exception:
            print(f"failed to fetch Korea {maturity} bond yield")
    return out


def collect_bond_yields():
    """Accumulating history (like DRAM/NAND), not a full-replace snapshot —
    each run just adds whatever (country, maturity, date) points aren't
    already recorded, from whichever countries actually returned new data.

    China (ChinaBond's 中债 yield curve) was tried too but dropped — its
    only free endpoint returns today's curve snapshot with no historical
    range, so unlike the other three it could only ever add a single point
    per run, which read as a lone dot rather than a real line for a long
    time (per feedback, not worth keeping given how sparse that stayed)."""
    existing = _load("bond_yields.json")
    seen = {(r["country"], r["maturity"], r["date"]) for r in existing}
    added = 0
    for fetch in (fetch_us_treasury_yields, fetch_japan_jgb_yields, fetch_korea_bond_yields):
        for row in fetch():
            key = (row["country"], row["maturity"], row["date"])
            if key in seen:
                continue
            existing.append({**row, "collected_at": now_iso()})
            seen.add(key)
            added += 1
    _save("bond_yields.json", existing)
    print(f"bond_yields.json: +{added} rows (total {len(existing)})")


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
    collect_bond_yields()
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
