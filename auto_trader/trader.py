"""Alpaca paper trading: sell lots past their 5-business-day hold, buy top N.

Same lot mechanics as the local IBKR version. Paper-only by construction:
TradingClient(paper=True) can only ever hit paper-api.alpaca.markets.
"""
import csv
import glob
import json
import logging
import re
from datetime import date

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

import config

log = logging.getLogger("trader")


# ---------------------------------------------------------------- state
def load_positions() -> list[dict]:
    if config.POSITIONS_PATH.exists():
        return json.loads(config.POSITIONS_PATH.read_text())
    return []


def save_positions(lots: list[dict]) -> None:
    config.POSITIONS_PATH.write_text(json.dumps(lots, indent=2))


def log_trade(row: dict) -> None:
    new = not config.TRADES_LOG.exists()
    with open(config.TRADES_LOG, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "action", "ticker", "qty",
                                           "order_id", "status", "note"])
        if new:
            w.writeheader()
        w.writerow(row)


# ---------------------------------------------------------------- signals
def latest_signals() -> tuple[pd.DataFrame, str]:
    files = sorted(glob.glob(str(config.BASE_DIR / "signals_*.csv")))
    if not files:
        raise SystemExit("No signals_*.csv found")
    fp = files[-1]
    asof = re.search(r"signals_(\d{8})\.csv$", fp).group(1)
    return pd.read_csv(fp), asof


# ---------------------------------------------------------------- broker
def _mask_acct(num) -> str:
    """Last 4 digits only — enough to confirm which account, in a CI log that
    may be world-readable. Not a credential, but no reason to publish it."""
    s = str(num or "")
    return f"…{s[-4:]}" if len(s) > 4 else "…"


def connect() -> TradingClient:
    if not config.ALPACA_API_KEY:
        raise SystemExit("ALPACA_API_KEY not set")
    client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
                           paper=True)   # paper endpoint, always
    acct = client.get_account()
    log.info("Connected to Alpaca paper account %s (equity %s)",
             _mask_acct(acct.account_number), acct.equity)
    return client


def _place(client: TradingClient, side: OrderSide, ticker: str, qty: int,
           note: str) -> dict:
    order = client.submit_order(MarketOrderRequest(
        symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY))
    st = str(order.status.value if hasattr(order.status, "value") else order.status)
    log.info("%s %s x%d -> %s", side.value, ticker, qty, st)
    log_trade({"date": date.today().isoformat(), "action": side.value.upper(),
               "ticker": ticker, "qty": qty, "order_id": str(order.id),
               "status": st, "note": note})
    return {"ticker": ticker, "qty": qty, "status": st}


# ---------------------------------------------------------------- main flow
def run_exits(client: TradingClient) -> list[str]:
    today = date.today().isoformat()
    keep, msgs = [], []
    for lot in load_positions():
        if lot["exit_after"] <= today:
            try:
                r = _place(client, OrderSide.SELL, lot["ticker"], lot["qty"],
                           f"exit lot from {lot['entry_date']}")
                msgs.append(f"SELL {lot['ticker']} x{lot['qty']} ({r['status']})")
            except Exception as e:
                log.exception("Exit failed for %s", lot)
                msgs.append(f"SELL {lot['ticker']} FAILED: {e}")
                keep.append(lot)
        else:
            keep.append(lot)
    save_positions(keep)
    return msgs


def run_entries(client: TradingClient, allow_buys: bool) -> list[str]:
    if not allow_buys:
        return ["Buys skipped (campaign ended or signals stale)."]
    sig, asof = latest_signals()
    lots = load_positions()
    entry = date.today()
    if any(l["entry_date"] == entry.isoformat() for l in lots):
        return ["Buys skipped (today's tranche already placed)."]

    # The notebook writes a valid but EMPTY signal file when there is nothing to
    # score (see the live-scoring guard in py_study_day10.ipynb). Say so plainly
    # rather than reporting "0 checked, only 0/3 lots placed".
    if len(sig) == 0:
        return [f"No signals in signals_{asof}.csv — signal file is empty "
                f"(nothing scored today) — nothing to buy."]

    # Single walk down the ranking until TOP_N lots are actually FILLED. A name
    # rejected by the screen, priced above the budget, or failing at the broker
    # does NOT shorten the tranche — the walk continues to the next candidate.
    candidates = sig.sort_values("rank").head(config.SHARIAH_MAX_CANDIDATES)
    exit_after = pd.bdate_range(entry, periods=config.HOLD_BDAYS + 1)[-1].date()
    if config.SHARIAH_SCREEN:
        import shariah

    placed, n_screened, n_rejected, msgs = 0, 0, 0, []
    for _, row in candidates.iterrows():
        if placed >= config.TOP_N:
            break
        tk = row["ticker"]

        px = float(row["last_adj_close"])
        qty = min(int(config.BUDGET_PER_NAME // px), config.MAX_ORDER_QTY)
        if qty < 1:
            msgs.append(f"↷ {tk} (rank {int(row['rank'])}): price {px:.2f} "
                        f"> ${config.BUDGET_PER_NAME:.0f} budget")
            continue

        if config.SHARIAH_SCREEN:
            n_screened += 1
            ok, reason = shariah.is_compliant(tk)
            if not ok:
                n_rejected += 1
                msgs.append(f"⛔ {tk} (rank {int(row['rank'])}): {reason}")
                continue

        try:
            r = _place(client, OrderSide.BUY, tk, qty, f"top{config.TOP_N} asof {asof}")
            lots.append({"ticker": tk, "qty": qty,
                         "entry_date": entry.isoformat(),
                         "exit_after": exit_after.isoformat()})
            placed += 1
            msgs.append(f"BUY {tk} x{qty} @~{px:.2f} "
                        f"rank {int(row['rank'])} ({r['status']})")
        except Exception as e:
            log.exception("Entry failed for %s", tk)
            msgs.append(f"BUY {tk} FAILED: {e}")

    if config.SHARIAH_SCREEN:
        msgs.insert(0, f"☪️ Shariah screen: {n_screened} checked, "
                       f"{n_screened - n_rejected} passed, {n_rejected} rejected")
    if placed < config.TOP_N:
        msgs.append(f"⚠️ only {placed}/{config.TOP_N} lots placed from the top "
                    f"{len(candidates)} names")
    save_positions(lots)
    return msgs


def log_equity(row: dict) -> None:
    """Append a daily account snapshot — the input to the cross-book comparison."""
    new = not config.EQUITY_LOG.exists()
    with open(config.EQUITY_LOG, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "strategy", "equity", "last_equity",
                                           "cash", "long_mv", "short_mv", "n_positions",
                                           "positions"])
        if new:
            w.writeheader()
        w.writerow(row)


def account_snapshot(client: TradingClient) -> str:
    """Print-friendly snapshot; also appends a row to equity_log.csv."""
    try:
        acct = client.get_account()
        pos = client.get_all_positions()
        long_mv = sum(float(p.market_value) for p in pos if float(p.qty) > 0)
        short_mv = sum(float(p.market_value) for p in pos if float(p.qty) < 0)
        pos_str = ", ".join(f"{p.symbol}:{p.qty}" for p in pos) or "flat"
        log_equity({
            "date": date.today().isoformat(),
            "strategy": config.STRATEGY,
            "equity": float(acct.equity),
            "last_equity": float(acct.last_equity),
            "cash": float(acct.cash),
            "long_mv": round(long_mv, 2),
            "short_mv": round(short_mv, 2),
            "n_positions": len(pos),
            "positions": pos_str,
        })
        return f"Equity ${float(acct.equity):,.0f} | positions: {pos_str}"
    except Exception:
        log.exception("account snapshot failed")
        return "account snapshot unavailable"
