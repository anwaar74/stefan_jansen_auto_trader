"""Approximate AAOIFI-style Shariah screen using free yfinance fundamentals.

Identical to the de Prado book's screen — deliberately so. Keeping both books
in the same compliant universe means the strategy comparison isolates the
*strategy*, not the investable universe.

Two layers:
  1. Business screen — industry/sector keyword exclusions.
  2. Financial ratios — interest-bearing debt and cash+interest-bearing
     securities each must be < 33% of market capitalisation.

LIMITATION: the non-compliant-income (<5% of revenue) test needs data that
free sources don't provide, so this is an approximation of the standard,
not a certified screening. Missing data => treated as NOT compliant
(conservative). For real-money decisions use a certified screener
(Zoya, Musaffa) or a certified universe (SPUS/HLAL holdings).
"""
import logging
import time

import yfinance as yf

import config

log = logging.getLogger("shariah")

# Yahoo rate-limits `.info` hard. When it does, it does NOT raise — it returns an
# empty/partial dict, which the ratio tests below read as "no market cap data" and
# reject as non-compliant. That silently threw away perfectly good names (CRL and
# BBY were rejected this way on 2026-07-30) and pushed the book down the ranking
# into picks the model never chose. Retry before believing the data is missing.
_RETRIES = 3
_PAUSE_S = 2.0
_CORE_FIELDS = ("marketCap", "totalDebt", "totalCash")

# Industry/sector substrings (lowercase) that fail the business screen.
HARAM_KEYWORDS = [
    # Conventional Finance & Riba (Interest)
    "bank", "insurance", "capital markets", "credit services",
    "financial conglomerates", "mortgage", "asset management",
    "pawn", "brokerage", "lending", "conventional finance",
    "stock exchanges",           # catches "Financial Data & Stock Exchanges"
    # Vice & Intoxicants
    "alcohol", "brewer", "distill", "winer", "liquor", "spirits",
    "tobacco", "gambling", "casino", "lotter", "betting",
    "adult", "porn", "cannabis",
    # Non-Permissible Food & Agriculture
    "pork", "swine", "non-halal", "haram meat",
    # Weapons & Defense
    "aerospace & defense", "defense", "weapon", "firearm", "military",
    # Entertainment & Media (high risk of non-compliant content)
    "cinema", "broadcasting", "music", "nightclub", "entertainment",
    # Hospitality & Leisure (high risk of alcohol/casino revenue)
    "hotel", "resort", "cruise",
    "lodging",                   # yfinance's industry name for hotels
    "travel services",           # yfinance's industry for cruise/booking firms
]

_cache: dict[str, tuple[bool, str]] = {}


def is_compliant(ticker: str) -> tuple[bool, str]:
    """Return (compliant, reason). Conservative on missing data.

    Transient failures are NOT cached — a rate-limited lookup must not poison the
    verdict for the rest of the run.
    """
    if ticker in _cache:
        return _cache[ticker]
    result, transient = _screen(ticker)
    if not transient:
        _cache[ticker] = result
    return result


def _fetch_info(ticker: str) -> dict | None:
    """`.info` with backoff. None means "genuinely could not fetch", as distinct
    from "fetched, and the company really has no such field"."""
    sym = ticker.replace(".", "-")
    partial = None
    for attempt in range(_RETRIES):
        try:
            info = yf.Ticker(sym).info or {}
            # A complete payload has the fundamentals. Throttling often returns
            # the *quote* half (sector/industry) with the financials missing —
            # which reads as "no market cap data" and looks like a compliance
            # failure. AMD and MU were rejected that way on 2026-08-03. Keep
            # retrying for the full payload; only fall back to a partial one on
            # the last attempt, when it really may just be a sparse listing.
            if all(info.get(f) is not None for f in _CORE_FIELDS):
                return info
            if info.get("sector") or info.get("industry"):
                partial = info
        except Exception as e:
            log.warning("%s: .info raised %s (attempt %d/%d)",
                        ticker, type(e).__name__, attempt + 1, _RETRIES)
        if attempt < _RETRIES - 1:
            time.sleep(_PAUSE_S * (2 ** attempt))
    if partial is not None:
        log.warning("%s: only a partial payload after %d tries — screening on "
                    "what we have", ticker, _RETRIES)
    return partial


def _screen(ticker: str) -> tuple[tuple[bool, str], bool]:
    """Returns ((compliant, reason), transient_failure)."""
    info = _fetch_info(ticker)
    if info is None:
        # Conservative (still not bought) but flagged, so the Telegram message
        # says "rate limited" rather than implying the company failed the screen.
        return (False, f"⏳ data unavailable after {_RETRIES} tries "
                       f"(rate limited?) — not a compliance failure"), True

    ok, reason = _screen_info(info)
    return (ok, reason), False


def _screen_info(info: dict) -> tuple[bool, str]:
    sector = str(info.get("sector", "")).lower()
    industry = str(info.get("industry", "")).lower()
    if not sector and not industry:
        return False, "no sector/industry data"
    for kw in HARAM_KEYWORDS:
        if kw in industry or kw in sector:
            return False, f"business screen: {kw}"

    mcap = info.get("marketCap")
    if not mcap:
        return False, "no market cap data"

    debt = info.get("totalDebt")
    if debt is None:
        return False, "no debt data"
    if debt / mcap > config.SHARIAH_MAX_DEBT_RATIO:
        return False, f"debt {debt/mcap:.0%} of mcap (max {config.SHARIAH_MAX_DEBT_RATIO:.0%})"

    cash = info.get("totalCash")
    if cash is None:
        return False, "no cash data"
    if cash / mcap > config.SHARIAH_MAX_CASH_RATIO:
        return False, f"cash {cash/mcap:.0%} of mcap (max {config.SHARIAH_MAX_CASH_RATIO:.0%})"

    return True, "ok"
