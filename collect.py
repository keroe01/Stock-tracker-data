"""Runs on a schedule via GitHub Actions, independent of whether the local
app/PC is on — captures point-in-time-only data (DRAM/NAND spot prices,
analyst target price/recommendation) that has no free historical backfill,
so a PC that's off when this data window passes would otherwise lose it
permanently.

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


def _fetch_chart(symbol: str, range_: str) -> dict | None:
    try:
        resp = requests.get(
            CHART_URL.format(symbol=symbol), headers=HEADERS, params={"interval": "1d", "range": range_}, timeout=15
        )
        resp.raise_for_status()
        result = resp.json()["chart"]["result"]
        return result[0] if result else None
    except Exception:
        print(f"failed to fetch chart for {symbol}")
        return None


def fetch_quote(symbol: str) -> dict | None:
    """Ported from the desktop app's app/services/yahoo.py — latest
    price/change/volume for a symbol, no crumb needed."""
    chart = _fetch_chart(symbol, "5d")
    if chart is None:
        return None
    closes = [c for c in chart["indicators"]["quote"][0]["close"] if c is not None]
    volumes = chart["indicators"]["quote"][0]["volume"]
    if not closes:
        return None
    price = closes[-1]
    prev_close = closes[-2] if len(closes) > 1 else chart["meta"].get("chartPreviousClose", price)
    change = price - prev_close
    change_percent = (change / prev_close * 100) if prev_close else 0.0
    return {
        "price": float(price),
        "change": float(change),
        "change_percent": float(change_percent),
        "volume": int((volumes and volumes[-1]) or 0),
        "currency": chart["meta"].get("currency", ""),
    }


def fetch_ohlc_last(symbol: str) -> dict | None:
    """Just today's open/high/low, to pair with fetch_quote()'s close —
    same chart endpoint the app's fetch_ohlc_history() uses, trimmed to
    the single most recent candle since the cloud snapshot only needs
    "today", not a full year (the app keeps its own full local history)."""
    chart = _fetch_chart(symbol, "5d")
    if chart is None:
        return None
    q = chart["indicators"]["quote"][0]
    for i in range(len(chart["timestamp"]) - 1, -1, -1):
        if q["close"][i] is not None:
            return {"open": q["open"][i], "high": q["high"][i], "low": q["low"][i]}
    return None


_BUNDLE_MODULES = "defaultKeyStatistics,summaryDetail,earningsTrend,financialData"


def fetch_bundle(symbol: str) -> dict:
    """Trimmed port of the app's yahoo.fetch_bundle() — just the fields
    financials.json/quarterly_eps.json need (per/pbr/eps/forward figures/
    market cap/roe/fcf/shares outstanding). Target price + recommendation
    trend are deliberately left to the existing collect_target_price()
    above rather than duplicated here."""
    out = {
        "eps": None, "forward_eps": None, "per": None, "forward_pe": None, "pbr": None,
        "market_cap": None, "shares_outstanding": None, "roe": None, "fcf": None,
        "forward_eps_revisions": [],
    }
    data = fetch_quote_summary(symbol, _BUNDLE_MODULES)
    if data is None:
        return out
    dks = data.get("defaultKeyStatistics", {})
    sd = data.get("summaryDetail", {})
    fd = data.get("financialData", {})
    out.update(
        eps=_raw(dks.get("trailingEps")),
        forward_eps=_raw(dks.get("forwardEps")),
        per=_raw(sd.get("trailingPE")),
        forward_pe=_raw(sd.get("forwardPE")),
        pbr=_raw(dks.get("priceToBook")),
        market_cap=_raw(sd.get("marketCap")),
        shares_outstanding=_raw(dks.get("sharesOutstanding")),
        roe=_raw(fd.get("returnOnEquity")),
        fcf=_raw(fd.get("freeCashflow")),
    )
    trend = data.get("earningsTrend", {}).get("trend", [])
    next_year = next((t for t in trend if t.get("period") == "+1y"), None)
    if next_year:
        eps_trend = next_year.get("epsTrend", {})
        for key, days_ago in {"current": 0, "7daysAgo": 7, "30daysAgo": 30, "60daysAgo": 60, "90daysAgo": 90}.items():
            value = _raw(eps_trend.get(key))
            if value is not None:
                out["forward_eps_revisions"].append({"days_ago": days_ago, "eps_estimate": value})
    return out


TIMESERIES_URL = "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{symbol}"


def fetch_fundamentals_timeseries(symbol: str, types: str, years: int = 2) -> dict[str, list]:
    """Direct port of yahoo.fetch_fundamentals_timeseries() — real quarterly
    FCF/net income/stockholders' equity, an unauthenticated endpoint
    separate from quoteSummary."""
    now = int(time.time())
    try:
        resp = requests.get(
            TIMESERIES_URL.format(symbol=symbol),
            params={"type": types, "period1": str(now - years * 365 * 86400), "period2": str(now), "merge": "false"},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("timeseries", {}).get("result", [])
    except Exception:
        print(f"failed to fetch fundamentals timeseries for {symbol}")
        return {}
    out: dict[str, list] = {}
    for item in results:
        type_names = item.get("meta", {}).get("type") or []
        if not type_names:
            continue
        type_name = type_names[0]
        points = []
        for e in item.get(type_name) or []:
            if e is None:
                continue
            date = e.get("asOfDate")
            value = (e.get("reportedValue") or {}).get("raw")
            if date is not None and value is not None:
                points.append((date, float(value)))
        out[type_name] = points
    return out


# ---- Naver Finance: KR fundamentals + investor flow ----
# Ported from the desktop app's app/services/naver_finance.py. The real
# weighted-average-share EPS divisor (WiseReport scrape) is deliberately
# NOT ported here — it's a fragile multi-step AJAX scrape and the payoff
# (a few % EPS precision) isn't worth the extra maintenance surface in a
# second codebase; quarterly_eps.json here uses Yahoo's shares_outstanding
# divisor, same fallback the desktop app itself uses when the real divisor
# isn't available. The app's own live collection still gets full precision
# when it's running.

NAVER_MAIN_URL = "https://finance.naver.com/item/main.naver"
FRGN_URL = "https://finance.naver.com/item/frgn.naver"


def _code_from_symbol(symbol: str) -> str | None:
    m = re.match(r"(\d{6})\.(KS|KQ)$", symbol)
    return m.group(1) if m else None


def _to_float(text: str) -> float | None:
    text = text.replace(",", "").strip()
    if not text or text in ("N/A", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_kr_fundamentals(code: str) -> dict | None:
    try:
        resp = requests.get(NAVER_MAIN_URL, params={"code": code}, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.select_one("table.per_table")
        if table is None:
            return None
        by_id = {em["id"]: _to_float(em.get_text()) for em in table.select("em") if em.get("id")}
        return {
            "per": by_id.get("_per"), "eps": by_id.get("_eps"),
            "forward_pe": by_id.get("_cns_per"), "forward_eps": by_id.get("_cns_eps"),
            "pbr": by_id.get("_pbr"),
        }
    except Exception:
        print(f"failed to fetch naver fundamentals for {code}")
        return None


def _wise_or_frgn_num(text: str) -> float | None:
    text = text.replace(",", "").replace("+", "").replace("%", "").strip()
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_kr_investor_flow(code: str, max_pages: int = 4) -> list[dict]:
    by_date: dict[str, dict] = {}
    try:
        for page in range(1, max_pages + 1):
            resp = requests.get(FRGN_URL, params={"code": code, "page": page}, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "html.parser")
            tables = soup.select("table")
            if len(tables) < 4:
                break
            got_any = False
            for tr in tables[3].select("tr"):
                cells = [td.get_text(strip=True) for td in tr.select("td")]
                if len(cells) < 9 or not re.match(r"^\d{4}\.\d{2}\.\d{2}$", cells[0]):
                    continue
                date = cells[0].replace(".", "-")
                got_any = True
                if date in by_date:
                    continue
                by_date[date] = {
                    "date": date,
                    "volume": _wise_or_frgn_num(cells[4]),
                    "institution_net": _wise_or_frgn_num(cells[5]),
                    "foreign_net": _wise_or_frgn_num(cells[6]),
                    "foreign_shares": _wise_or_frgn_num(cells[7]),
                    "foreign_ratio": _wise_or_frgn_num(cells[8]),
                }
            if not got_any:
                break
    except Exception:
        print(f"failed to fetch naver investor flow for {code}")
    return sorted(by_date.values(), key=lambda r: r["date"])


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


# ---- 시세/재무/분기재무/투자자수급/Forward PE·PBR 캐시 ----
# Everything below mirrors a desktop-app Excel sheet 1:1 (see app/storage/
# excel_store.py's SHEETS), so the app's own merge_*() functions can drop
# these straight into the matching sheet with the same dedup keys it
# already uses for target_price.json above.


def collect_prices(watchlist: list[dict]):
    """One row per (symbol, date) — mirrors the Prices sheet. Only today's
    snapshot; the app's own local collection already backfills a full
    year on a symbol's first run, so the cloud side doesn't need to."""
    existing = _load("prices.json")
    seen = {(r["symbol"], r["date"]) for r in existing}
    added = 0
    for stock in watchlist:
        symbol = stock["symbol"]
        key = (symbol, today())
        if key in seen:
            continue
        quote = fetch_quote(symbol)
        if quote is None:
            continue
        ohlc = fetch_ohlc_last(symbol) or {}
        existing.append(
            {
                "symbol": symbol,
                "date": today(),
                "price": quote["price"],
                "change": quote["change"],
                "change_percent": quote["change_percent"],
                "volume": quote["volume"],
                "currency": quote["currency"],
                "collected_at": now_iso(),
                "open": ohlc.get("open"),
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
            }
        )
        seen.add(key)
        added += 1
        time.sleep(0.3)
    _save("prices.json", existing)
    print(f"prices.json: +{added} rows")


def collect_financials(watchlist: list[dict]):
    """One row per (symbol, date) — the per/pbr/eps/market-cap/roe/fcf
    subset of the Financials sheet (target price/recommendation fields
    stay in collect_target_price()/target_price.json above; the app's
    merge_financials() and merge_cloud_data()'s existing target_price
    branch both backfill into the same Financials row independently, same
    pattern already used for that row today)."""
    existing = _load("financials.json")
    seen = {(r["symbol"], r["date"]) for r in existing}
    added = 0
    for stock in watchlist:
        symbol, market = stock["symbol"], stock.get("market")
        key = (symbol, today())
        if key in seen:
            continue
        bundle = fetch_bundle(symbol)
        per, pbr, eps, forward_eps, forward_pe = (
            bundle["per"], bundle["pbr"], bundle["eps"], bundle["forward_eps"], bundle["forward_pe"],
        )
        if market == "KR":
            code = _code_from_symbol(symbol)
            fund = fetch_kr_fundamentals(code) if code else None
            if fund:
                per, pbr, eps, forward_eps, forward_pe = (
                    fund["per"], fund["pbr"], fund["eps"], fund["forward_eps"], fund["forward_pe"],
                )
        if per is None and pbr is None and eps is None and bundle["market_cap"] is None:
            continue
        existing.append(
            {
                "symbol": symbol,
                "date": today(),
                "per": per,
                "pbr": pbr,
                "eps": eps,
                "forward_eps": forward_eps,
                "forward_pe": forward_pe,
                "market_cap": bundle["market_cap"],
                "roe": bundle["roe"],
                "fcf": bundle["fcf"],
                "collected_at": now_iso(),
            }
        )
        seen.add(key)
        added += 1
        time.sleep(0.3)
    _save("financials.json", existing)
    print(f"financials.json: +{added} rows")


def collect_quarterly(watchlist: list[dict]):
    """quarterly_eps.json + quarterly_financials.json together, since both
    come off the same per-symbol Yahoo calls. Upserted by (symbol,
    quarter_end_date) rather than skip-if-exists — Yahoo can restate a
    recent quarter between runs, same reasoning as the app's own
    QuarterlyEPS/QuarterlyFinancials "revise each cycle" handling."""
    eps_by_key = {(r["symbol"], r["quarter_end_date"]): r for r in _load("quarterly_eps.json")}
    fin_by_key = {(r["symbol"], r["quarter_end_date"]): r for r in _load("quarterly_financials.json")}
    for stock in watchlist:
        symbol = stock["symbol"]
        data = fetch_quote_summary(symbol, "incomeStatementHistoryQuarterly")
        shares = None
        bundle_for_shares = fetch_bundle(symbol)
        shares = bundle_for_shares["shares_outstanding"]
        if data and shares:
            statements = data.get("incomeStatementHistoryQuarterly", {}).get("incomeStatementHistory", [])
            for stmt in statements[:4]:
                net_income = _raw(stmt.get("netIncome"))
                end_date = _raw(stmt.get("endDate"))
                if net_income is None or end_date is None:
                    continue
                q_date = datetime.datetime.utcfromtimestamp(int(end_date)).date().isoformat()
                eps_by_key[(symbol, q_date)] = {
                    "symbol": symbol,
                    "quarter_end_date": q_date,
                    "net_income": net_income,
                    "eps": round(net_income / shares, 4),
                    "collected_at": now_iso(),
                }

        ts_data = fetch_fundamentals_timeseries(
            symbol, "quarterlyFreeCashFlow,quarterlyNetIncome,quarterlyStockholdersEquity"
        )
        fcf_by_date = dict(ts_data.get("quarterlyFreeCashFlow", []))
        income_by_date = dict(ts_data.get("quarterlyNetIncome", []))
        equity_by_date = dict(ts_data.get("quarterlyStockholdersEquity", []))
        for d in sorted(set(fcf_by_date) | set(income_by_date) | set(equity_by_date)):
            fin_by_key[(symbol, d)] = {
                "symbol": symbol,
                "quarter_end_date": d,
                "net_income": income_by_date.get(d),
                "stockholders_equity": equity_by_date.get(d),
                "free_cash_flow": fcf_by_date.get(d),
                "collected_at": now_iso(),
            }
        time.sleep(0.3)
    _save("quarterly_eps.json", list(eps_by_key.values()))
    _save("quarterly_financials.json", list(fin_by_key.values()))
    print(f"quarterly_eps.json: {len(eps_by_key)} rows, quarterly_financials.json: {len(fin_by_key)} rows")


def collect_investor_flow(watchlist: list[dict]):
    """InvestorFlow sheet counterpart — KR symbols only, no US equivalent
    (same as the app's own naver_finance.fetch_kr_investor_flow)."""
    existing = _load("investor_flow.json")
    by_key = {(r["symbol"], r["date"]): r for r in existing}
    added = 0
    for stock in watchlist:
        if stock.get("market") != "KR":
            continue
        code = _code_from_symbol(stock["symbol"])
        if not code:
            continue
        for row in fetch_kr_investor_flow(code):
            key = (stock["symbol"], row["date"])
            if key in by_key:
                continue
            by_key[key] = {
                "symbol": stock["symbol"],
                "date": row["date"],
                "institution_net": row["institution_net"],
                "foreign_net": row["foreign_net"],
                "foreign_shares": row["foreign_shares"],
                "foreign_ratio": row["foreign_ratio"],
                "volume": row.get("volume"),
                "collected_at": now_iso(),
            }
            added += 1
        time.sleep(0.3)
    _save("investor_flow.json", list(by_key.values()))
    print(f"investor_flow.json: +{added} rows")


def collect_chart_cache(watchlist: list[dict]):
    """ChartCache sheet counterpart (forward_pe_quarterly/pbr_quarterly) —
    a full-replace snapshot per (symbol, chart), same semantics as the
    app's own replace_sheet_where() call for this sheet, not an
    accumulating history."""
    by_key = {(r["symbol"], r["chart"], r["label"]): r for r in _load("chart_cache.json")}
    for stock in watchlist:
        symbol = stock["symbol"]
        bundle = fetch_bundle(symbol)
        if stock.get("market") == "KR":
            # Yahoo's own PBR coverage for KRX tickers is frequently empty
            # (same gap collect_financials() works around) — without this,
            # bps_estimate below silently comes out None and every KR
            # symbol loses its pbr chart entirely.
            code = _code_from_symbol(symbol)
            fund = fetch_kr_fundamentals(code) if code else None
            if fund and fund.get("pbr"):
                bundle["pbr"] = fund["pbr"]
        quote = fetch_quote(symbol)
        chart = _fetch_chart(symbol, "1y")
        price_history = []
        if chart:
            q = chart["indicators"]["quote"][0]
            for i, ts in enumerate(chart["timestamp"]):
                c = q["close"][i]
                if c is not None:
                    price_history.append((ts, float(c)))

        def _nearest_price(ts_target):
            if not price_history:
                return None
            return min(price_history, key=lambda p: abs(p[0] - ts_target))[1]

        now = now_iso()
        for k in list(by_key):
            if k[0] == symbol and k[1] in ("forward_pe", "pbr"):
                del by_key[k]

        for rev in sorted(bundle.get("forward_eps_revisions", []), key=lambda r: -r["days_ago"]):
            target_ts = int(time.time()) - rev["days_ago"] * 86400
            price_then = _nearest_price(target_ts)
            if price_then is None or not rev["eps_estimate"]:
                continue
            label = datetime.datetime.utcfromtimestamp(target_ts).strftime("%y.%m.%d")
            by_key[(symbol, "forward_pe", label)] = {
                "symbol": symbol, "chart": "forward_pe", "label": label,
                "value": round(price_then / rev["eps_estimate"], 2), "collected_at": now,
            }

        price_now = quote["price"] if quote else None
        bps_estimate = (price_now / bundle["pbr"]) if price_now and bundle["pbr"] else None
        if bps_estimate:
            data = fetch_quote_summary(symbol, "incomeStatementHistoryQuarterly")
            statements = (
                data.get("incomeStatementHistoryQuarterly", {}).get("incomeStatementHistory", []) if data else []
            )
            for stmt in statements[:4]:
                end_date = _raw(stmt.get("endDate"))
                if end_date is None:
                    continue
                price_then = _nearest_price(int(end_date))
                if price_then is None:
                    continue
                dt = datetime.datetime.utcfromtimestamp(int(end_date))
                label = f"{dt.year % 100:02d}.Q{(dt.month - 1) // 3 + 1}"
                by_key[(symbol, "pbr", label)] = {
                    "symbol": symbol, "chart": "pbr", "label": label,
                    "value": round(price_then / bps_estimate, 2), "collected_at": now,
                }
        time.sleep(0.3)
    _save("chart_cache.json", list(by_key.values()))
    print(f"chart_cache.json: {len(by_key)} rows")


def collect_dram_index():
    """DRAM_Index sheet counterpart — a manually curated, one-off
    historical page (see app/services/memory_index.py), not a live feed,
    so this only actually fetches once (skipped on every run after the
    file already has rows), same as the app's own excel_store check."""
    if _load("dram_index.json"):
        print("dram_index.json: already populated, skipping")
        return
    try:
        resp = requests.get(
            "https://agenticsciences.github.io/memory-price-tracker/", headers=HEADERS, timeout=15
        )
        resp.raise_for_status()
        m = re.search(r"const hist\s*=\s*\[(.*?)\];", resp.text, re.S)
        if not m:
            print("dram_index.json: source page format not matched, skipping")
            return
        entry_re = re.compile(r"\{x:'([^']+)',\s*v:([\d.]+)(,\s*verified:true)?,\s*src:'([^']*)'\}")
        quarter_month = {"Q1": "01", "Q2": "04", "Q3": "07", "Q4": "10"}
        out = []
        for label, value, verified_flag, source in entry_re.findall(m.group(1)):
            date = None
            qm = re.match(r"(\d{4}) (Q[1-4])", label)
            ym = re.match(r"(\d{4})-(\d{2})", label)
            if qm:
                date = f"{qm.group(1)}-{quarter_month[qm.group(2)]}-01"
            elif ym:
                date = f"{ym.group(1)}-{ym.group(2)}-01"
            if date is None:
                continue
            out.append(
                {
                    "date": date, "period_label": label, "value": float(value),
                    "verified": bool(verified_flag), "source": source, "collected_at": now_iso(),
                }
            )
        out.sort(key=lambda r: r["date"])
        _save("dram_index.json", out)
        print(f"dram_index.json: {len(out)} rows")
    except Exception:
        print("failed to fetch DRAM index history")


FX_TICKERS = [
    {"symbol": "KRW=X", "name": "원/달러"},
    {"symbol": "JPY=X", "name": "엔/달러"},
    {"symbol": "EURKRW=X", "name": "원/유로"},
    {"symbol": "CNY=X", "name": "위안화/달러"},
]

INDEX_TICKERS = [
    {"symbol": "^KS11", "name": "KOSPI", "group": "main"},
    {"symbol": "^IXIC", "name": "NASDAQ", "group": "main"},
    {"symbol": "^KS200", "name": "코스피200 (선물 기초지수)", "group": "futures"},
    {"symbol": "^GSPC", "name": "S&P 500", "group": "main"},
    {"symbol": "^DJI", "name": "DOW", "group": "main"},
    {"symbol": "ES=F", "name": "S&P500 선물", "group": "futures"},
    {"symbol": "YM=F", "name": "다우 선물", "group": "futures"},
    {"symbol": "NQ=F", "name": "나스닥100 선물", "group": "futures"},
]


def collect_fx():
    """FXRates sheet counterpart — full-replace snapshot (current quote
    only, no embedded OHLC history; the app already gets real history
    straight from Yahoo on demand, same reasoning as index_data.py/
    fx_data.py's own "no local accumulation needed" note)."""
    out = []
    for t in FX_TICKERS:
        quote = fetch_quote(t["symbol"])
        out.append(
            {
                "symbol": t["symbol"], "name": t["name"],
                "price": quote["price"] if quote else None,
                "change": quote["change"] if quote else None,
                "change_percent": quote["change_percent"] if quote else None,
                "collected_at": now_iso(),
            }
        )
        time.sleep(0.2)
    _save("fx.json", out)
    print(f"fx.json: {len(out)} pairs")


def collect_index():
    """IndexQuotes sheet counterpart — same snapshot approach as
    collect_fx()."""
    out = []
    for t in INDEX_TICKERS:
        quote = fetch_quote(t["symbol"])
        out.append(
            {
                "symbol": t["symbol"], "name": t["name"], "group": t["group"],
                "price": quote["price"] if quote else None,
                "change": quote["change"] if quote else None,
                "change_percent": quote["change_percent"] if quote else None,
                "currency": quote["currency"] if quote else None,
                "collected_at": now_iso(),
            }
        )
        time.sleep(0.2)
    _save("index.json", out)
    print(f"index.json: {len(out)} tickers")


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


# ---- 자기주식(자사주) 취득 현황 — KRX KIND, 직접취득 방식만 ----
# DART의 구조화 API(자기주식 취득 및 처분 현황, DS002)는 분기 단위라 일별
# 데이터가 없다는 걸 확인한 뒤, KRX가 운영하는 별도 공시채널 KIND
# (kind.krx.co.kr)에 이 정확한 데이터가 있는 걸 찾음: 직접취득(신탁이 아닌
# 방식)은 매매 전날 저녁 그 다음 거래일의 신청수량을 미리 공시하고, 거래
# 당일 실제 체결수량/체결율을 공시하는 구조. 회사별 5자리 KIND 코드로
# 필터링 가능 (예: SK하이닉스 = "00066", 종목코드 000660과는 다른 값).
KIND_TREASURY_URL = "https://kind.krx.co.kr/corpgeneral/treasurystk.do"

_TREASURY_BUYBACK_TARGETS = [
    {"symbol": "000660.KS", "name": "SK하이닉스", "kind_code": "00066"},
]


def _kind_treasury_post(method: str, search_gubun: str, kind_code: str, from_date: str, to_date: str) -> str:
    resp = requests.post(
        KIND_TREASURY_URL,
        data={
            "method": method, "pageIndex": "1", "currentPageSize": "100",
            "orderMode": "0", "orderStat": "D", "searchGubun": search_gubun,
            "marketType": "all", "trstkGubun": "all", "acqDispGubun": "all",
            "fromDate": from_date, "toDate": to_date,
            "isurCd": kind_code, "repIsuSrtCd": "", "repIsuCd": "", "corpName": "", "comAbbrv": "",
        },
        headers=HEADERS, timeout=20,
    )
    resp.raise_for_status()
    return resp.text


def _kind_num(text: str) -> float | None:
    text = text.strip().replace(",", "").replace("%", "")
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_treasury_decl(html: str) -> list[dict]:
    """신고내역 — the board-resolution declaration: total declared quantity
    and the program's trading period, plus a live "as of now" cumulative
    executed total (not tied to the declaration date itself — this updates
    on its own as the exchange processes each trading day)."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("table.list tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue
        method_disp = " ".join(tds[2].get_text(" ", strip=True).split())
        method, _, acq_disp = method_disp.partition("/")
        period = tds[3].get_text(" ", strip=True).split("~")
        out.append(
            {
                "decl_date": tds[0].get_text(strip=True),
                "method": method.strip(),
                "acq_disp": acq_disp.strip(),
                "period_start": period[0].strip() if period else None,
                "period_end": period[1].strip() if len(period) > 1 else None,
                "declared_qty": _kind_num(tds[4].get_text(strip=True)),
                "cum_executed_qty": _kind_num(tds[5].get_text(strip=True)),
                "cum_executed_pct": _kind_num(tds[6].get_text(strip=True)),
                "cum_executed_amount": _kind_num(tds[7].get_text(strip=True)),
            }
        )
    return out


def _parse_treasury_appl(html: str) -> list[dict]:
    """신청내역 — filed the trading day BEFORE the day it applies to (this
    is the "tomorrow's planned volume" disclosure): requested quantity for
    that upcoming day, and the program's remaining eligible balance after
    it."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("table.list tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        out.append(
            {
                "date": tds[0].get_text(strip=True),
                "requested_qty": _kind_num(tds[4].get_text(strip=True)),
                "remaining_qty": _kind_num(tds[5].get_text(strip=True)),
            }
        )
    return out


def _parse_treasury_trd(html: str) -> list[dict]:
    """체결내역 — the actual trading day's requested vs. executed quantity
    and fill rate, published once that day's trading concludes (so the
    most recent trading day can lag a day behind the newest 신청내역 row
    until the exchange updates this table)."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("table.list tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        out.append(
            {
                "date": tds[0].get_text(strip=True),
                "requested_qty": _kind_num(tds[3].get_text(strip=True)),
                "executed_qty": _kind_num(tds[4].get_text(strip=True)),
                "fill_rate": _kind_num(tds[5].get_text(strip=True)),
            }
        )
    return out


def collect_treasury_buyback():
    # Keyed by (symbol, decl_date) and seeded from the existing file —
    # same pattern as applications/executions below — so an entry is only
    # replaced when this run's KIND scrape actually returns it again.
    # Previously this list started empty and got saved unconditionally,
    # so a single transient KIND failure (network hiccup, timeout)
    # silently wiped the whole file to [] instead of just leaving that
    # symbol's entries untouched for this run — confirmed happening for
    # real (SK하이닉스's program vanished for one cycle despite the
    # underlying KIND data being fine both before and after).
    programs = {(r["symbol"], r["decl_date"]): r for r in _load("treasury_buyback_programs.json")}
    applications = {(r["symbol"], r["date"]): r for r in _load("treasury_buyback_applications.json")}
    executions = {(r["symbol"], r["date"]): r for r in _load("treasury_buyback_executions.json")}
    today_str = today()
    # A year back covers this program comfortably and any future one of
    # similar length without needing to know the exact start date ahead of
    # time — these tables are small (one company, a few rows/day) so the
    # wider window costs nothing.
    from_str = (datetime.date.today() - datetime.timedelta(days=365)).strftime("%Y-%m-%d")

    for target in _TREASURY_BUYBACK_TARGETS:
        symbol, name, code = target["symbol"], target["name"], target["kind_code"]
        try:
            decl_rows = _parse_treasury_decl(
                _kind_treasury_post("searchDeclOfTreasuryStkAcqDisp", "decl", code, from_str, today_str)
            )
            for r in decl_rows:
                programs[(symbol, r["decl_date"])] = {"symbol": symbol, "name": name, "collected_at": now_iso(), **r}
        except Exception:
            print(f"failed to fetch treasury buyback declaration for {symbol}")

        try:
            appl_rows = _parse_treasury_appl(
                _kind_treasury_post("searchApplOfTreasuryStkAcqDisp", "appl", code, from_str, today_str)
            )
            for r in appl_rows:
                key = (symbol, r["date"])
                applications[key] = {"symbol": symbol, "collected_at": now_iso(), **r}
        except Exception:
            print(f"failed to fetch treasury buyback applications for {symbol}")

        try:
            trd_rows = _parse_treasury_trd(
                _kind_treasury_post("searchTrdOfTreasuryStkAcqDisp", "trd", code, from_str, today_str)
            )
            for r in trd_rows:
                key = (symbol, r["date"])
                executions[key] = {"symbol": symbol, "collected_at": now_iso(), **r}
        except Exception:
            print(f"failed to fetch treasury buyback executions for {symbol}")

        time.sleep(0.3)

    _save("treasury_buyback_programs.json", list(programs.values()))
    _save("treasury_buyback_applications.json", list(applications.values()))
    _save("treasury_buyback_executions.json", list(executions.values()))
    print(
        f"treasury_buyback: {len(programs)} program(s), {len(applications)} application row(s), "
        f"{len(executions)} execution row(s)"
    )


def main():
    with open(ROOT / "watchlist.json", encoding="utf-8") as f:
        watchlist = json.load(f)
    collect_memory_spot()
    collect_bond_yields()
    collect_target_price(watchlist)
    collect_prices(watchlist)
    collect_financials(watchlist)
    collect_quarterly(watchlist)
    collect_investor_flow(watchlist)
    collect_chart_cache(watchlist)
    collect_dram_index()
    collect_fx()
    collect_index()
    collect_treasury_buyback()
    collect_cloud_growth()
    collect_kr_screener()
    collect_us_screener()
    collect_nasdaq100_screener()


if __name__ == "__main__":
    main()
