"""S&P500 저PER/저PBR/상승률 TOP20 screener — same ranking logic as
kr_screener.py, but sourced from Yahoo Finance for the US market.
Constituent list comes from SPDR's public SPY holdings file (an ETF
that tracks the S&P 500) since Yahoo/Nasdaq don't offer a free bulk
"whole index" listing endpoint the way Naver does for KOSPI."""

import io
import re
from concurrent.futures import ThreadPoolExecutor

import openpyxl
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SPY_HOLDINGS_URL = (
    "https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)
QUOTE_SUMMARY_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"

_MAX_WORKERS = 12
TOP_N = 20
COMBO_N = 5

_TICKER_RE = re.compile(r"^[A-Z]{1,6}(\.[A-Z])?$")


def _fetch_sp500_tickers() -> list[tuple[str, str]]:
    """(ticker, name) pairs from SPY's published holdings — filters out
    the occasional non-equity row (cash, a contra/settlement line with a
    CUSIP-like "ticker") these files carry alongside the real 500."""
    try:
        resp = requests.get(SPY_HOLDINGS_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        wb = openpyxl.load_workbook(io.BytesIO(resp.content), data_only=True)
        ws = wb.active
        out = []
        for row in ws.iter_rows(min_row=6, values_only=True):
            name, ticker = row[0], row[1]
            if not ticker or not isinstance(ticker, str) or not _TICKER_RE.match(ticker):
                continue
            out.append((ticker, name or ticker))
        return out
    except Exception:
        print("failed to fetch SPY holdings")
        return []


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


def _fetch_stock(ticker: str, name: str) -> dict | None:
    session, crumb = _get_session_and_crumb()
    try:
        resp = session.get(
            QUOTE_SUMMARY_URL.format(symbol=ticker),
            params={"modules": "summaryDetail,defaultKeyStatistics,price", "crumb": crumb},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()["quoteSummary"]["result"]
        if not result:
            return None
        data = result[0]
        sd = data.get("summaryDetail", {})
        dks = data.get("defaultKeyStatistics", {})
        price = data.get("price", {})
        per = _raw(sd.get("trailingPE"))
        pbr = _raw(dks.get("priceToBook"))
        change_percent = _raw(price.get("regularMarketChangePercent"))
        market_cap = _raw(price.get("marketCap"))
        if per is None and pbr is None:
            return None
        return {
            "code": ticker,
            "name": name,
            "per": per,
            "pbr": pbr,
            "change_percent": change_percent * 100 if change_percent is not None else None,
            "market_cap": market_cap,
        }
    except Exception:
        print(f"failed to fetch quoteSummary for {ticker}")
        return None


def _fetch_all_sp500() -> list[dict]:
    tickers = _fetch_sp500_tickers()
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        results = list(pool.map(lambda t: _fetch_stock(*t), tickers))
    return [r for r in results if r is not None]


def screen_us_market() -> dict:
    """Same output shape as kr_screener.screen_kr_market(): TOP 20 lowest
    PER, TOP 20 lowest PBR, TOP 20 highest daily % change (all among
    S&P500 members), plus 4 short (COMBO_N) overlap-highlight lists."""
    all_stocks = _fetch_all_sp500()

    per_ranked = sorted(
        (s for s in all_stocks if s["per"] is not None and s["per"] > 0), key=lambda s: s["per"]
    )[:TOP_N]

    pbr_ranked = sorted(
        (s for s in all_stocks if s["pbr"] is not None and s["pbr"] > 0), key=lambda s: s["pbr"]
    )[:TOP_N]

    rise_ranked = sorted(
        (s for s in all_stocks if s["change_percent"] is not None),
        key=lambda s: s["change_percent"],
        reverse=True,
    )[:TOP_N]

    by_code = {s["code"]: s for s in all_stocks}
    per_codes = [s["code"] for s in per_ranked]
    pbr_codes = [s["code"] for s in pbr_ranked]
    rise_codes = [s["code"] for s in rise_ranked]

    def combo(primary: list[str], *others: list[str]) -> list[dict]:
        common = set(primary)
        for o in others:
            common &= set(o)
        return [by_code[c] for c in primary if c in common][:COMBO_N]

    per_pbr = combo(per_codes, pbr_codes)
    per_rise = combo(per_codes, rise_codes)
    pbr_rise = combo(pbr_codes, rise_codes)
    triple = combo(per_codes, pbr_codes, rise_codes)

    return {
        "per": per_ranked,
        "pbr": pbr_ranked,
        "rise": rise_ranked,
        "triple": triple,
        "per_pbr": per_pbr,
        "per_rise": per_rise,
        "pbr_rise": pbr_rise,
    }
