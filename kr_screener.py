"""KOSPI 저PER/저PBR/상승률 TOP20 screener — ported from the desktop app's
app/services/kr_screener.py (+ the PBR bits of app/services/naver_finance.py)
so it can run here on a GitHub Actions schedule instead of on the user's own
PC. The runner has no GUI thread to starve, so this uses a higher worker
count than the desktop app's version (which caps at 5 specifically to avoid
GIL contention with the Qt event loop)."""

import re
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
MARKET_SUM_URL = "https://finance.naver.com/sise/sise_market_sum.naver"
FUNDAMENTALS_URL = "https://finance.naver.com/item/main.naver"

_MAX_WORKERS = 12
PBR_CANDIDATE_POOL = 300
TOP_N = 20
COMBO_N = 5
_MAX_PAGES_PER_MARKET = 65
_MARKETS = (0,)  # sosok=0 is KOSPI


def _to_float(text: str) -> float | None:
    text = text.replace(",", "").replace("%", "").strip()
    if not text or text in ("N/A", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fetch_market_sum_page(sosok: int, page: int) -> list[dict]:
    try:
        resp = requests.get(MARKET_SUM_URL, params={"sosok": sosok, "page": page}, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table", class_="type_2")
        if table is None:
            return []
        out = []
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 13:
                continue
            link = cells[1].find("a")
            if link is None:
                continue
            m = re.search(r"code=(\d{6})", link.get("href", ""))
            if not m:
                continue
            per = _to_float(cells[10].get_text())
            roe = _to_float(cells[11].get_text())
            if per is None and roe is None:
                continue
            out.append(
                {
                    "code": m.group(1),
                    "name": link.get_text(strip=True),
                    "change_percent": _to_float(cells[4].get_text()),
                    "market_cap": _to_float(cells[6].get_text()),
                    "per": per,
                }
            )
        return out
    except Exception:
        print(f"failed to fetch market sum page sosok={sosok} page={page}")
        return []


def _fetch_all_market_sum() -> list[dict]:
    tasks = [(sosok, page) for sosok in _MARKETS for page in range(1, _MAX_PAGES_PER_MARKET + 1)]
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        results = pool.map(lambda t: _fetch_market_sum_page(*t), tasks)
        out = [row for rows in results for row in rows]
    return out


def _fetch_pbr_value(code: str) -> float | None:
    try:
        resp = requests.get(FUNDAMENTALS_URL, params={"code": code}, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.select_one("table.per_table")
        if table is None:
            return None
        for em in table.select("em"):
            if em.get("id") == "_pbr":
                return _to_float(em.get_text())
        return None
    except Exception:
        print(f"failed to fetch pbr for {code}")
        return None


def _fetch_pbr(stock: dict) -> None:
    stock["pbr"] = _fetch_pbr_value(stock["code"])


def screen_kr_market() -> dict:
    """Same output shape as the desktop app's screen_kr_market(): TOP 20
    lowest PER, TOP 20 lowest PBR (among the PBR_CANDIDATE_POOL largest by
    market cap), TOP 20 highest daily % change, plus 4 short (COMBO_N)
    overlap-highlight lists."""
    all_stocks = _fetch_all_market_sum()

    per_ranked = sorted(
        (s for s in all_stocks if s["per"] is not None and s["per"] > 0), key=lambda s: s["per"]
    )[:TOP_N]

    rise_ranked = sorted(
        (s for s in all_stocks if s["change_percent"] is not None),
        key=lambda s: s["change_percent"],
        reverse=True,
    )[:TOP_N]

    cap_ranked = sorted(
        (s for s in all_stocks if s["market_cap"] is not None), key=lambda s: s["market_cap"], reverse=True
    )
    pbr_candidates = cap_ranked[:PBR_CANDIDATE_POOL]
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        list(pool.map(_fetch_pbr, pbr_candidates))
    pbr_ranked = sorted(
        (s for s in pbr_candidates if s.get("pbr") is not None and s["pbr"] > 0), key=lambda s: s["pbr"]
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
