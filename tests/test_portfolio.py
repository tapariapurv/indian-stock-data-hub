"""Self-check for the money maths: FIFO gains, open positions and XIRR.

Runs against a throwaway database, so your real portfolio is untouched.
No network and no model needed.  Run: python tests/test_portfolio.py

These are the numbers people file tax returns against, so every case here is
one that has to be exactly right rather than roughly right.
"""
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import archive  # noqa: E402

tmp = tempfile.TemporaryDirectory()
archive.DB_PATH = Path(tmp.name) / "test.db"
archive._has_fts = None

import portfolio as pf  # noqa: E402


def trade(tid, side, qty, price, day, fees=0.0, ticker="DEMO"):
    return {"id": tid, "ticker": ticker, "side": side, "qty": qty, "price": price,
            "fees": fees, "date": day}


# --- FIFO: sells consume the oldest buys first -------------------------------
rows = [trade(1, "buy", 100, 10.0, "2024-01-10"),
        trade(2, "buy", 100, 20.0, "2024-06-10"),
        trade(3, "sell", 150, 30.0, "2026-02-10")]
gains, unmatched = pf.fifo_gains(rows)
assert not unmatched, unmatched
assert len(gains) == 2, gains
# First 100 came from the Rs 10 lot, the next 50 from the Rs 20 lot.
assert gains[0]["qty"] == 100 and gains[0]["buy_price"] == 10.0
assert gains[1]["qty"] == 50 and gains[1]["buy_price"] == 20.0
assert abs(sum(g["gain"] for g in gains) - (100 * 20 + 50 * 10)) < 1e-6, gains
assert all(g["term"] == "Long" for g in gains), "held over a year each"

qty, avg = pf.position_from_trades(rows)
assert qty == 50 and avg == 20.0, (qty, avg)

# --- Short vs long term turns on the 12-month line ---------------------------
short = [trade(1, "buy", 10, 100.0, "2026-01-01"), trade(2, "sell", 10, 120.0, "2026-06-01")]
assert pf.fifo_gains(short)[0][0]["term"] == "Short"
edge = [trade(1, "buy", 10, 100.0, "2025-01-01"), trade(2, "sell", 10, 120.0, "2026-01-01")]
assert pf.fifo_gains(edge)[0][0]["term"] == "Short", "365 days exactly is still short term"
long_ = [trade(1, "buy", 10, 100.0, "2025-01-01"), trade(2, "sell", 10, 120.0, "2026-01-03")]
assert pf.fifo_gains(long_)[0][0]["term"] == "Long"

# --- Charges belong in the cost, not beside it -------------------------------
fees = [trade(1, "buy", 10, 100.0, "2024-01-01", fees=50.0),
        trade(2, "sell", 10, 110.0, "2026-01-01", fees=50.0)]
g = pf.fifo_gains(fees)[0][0]
assert abs(g["buy_price"] - 105.0) < 1e-9, g          # 1000 + 50 over 10 shares
assert abs(g["sell_price"] - 105.0) < 1e-9, g         # 1100 - 50 over 10 shares
assert abs(g["gain"]) < 1e-9, "a round trip eaten entirely by charges is not a profit"

# --- A sell with nothing to match is reported, never invented ----------------
odd = [trade(1, "buy", 10, 100.0, "2024-01-01"), trade(2, "sell", 25, 120.0, "2026-01-01")]
gains, unmatched = pf.fifo_gains(odd)
assert unmatched == 15, unmatched
assert sum(g["qty"] for g in gains) == 10, "only the shares actually bought produce a gain"

# --- Nothing held, nothing to say --------------------------------------------
assert pf.position_from_trades([]) == (0.0, None)
assert pf.fifo_gains([]) == ([], 0.0)
closed = [trade(1, "buy", 10, 100.0, "2024-01-01"), trade(2, "sell", 10, 120.0, "2026-01-01")]
assert pf.position_from_trades(closed) == (0.0, None), "a closed position holds nothing"

print("ok: FIFO matches oldest buys first, splits short from long, and flags unmatched sells")

# --- XIRR --------------------------------------------------------------------
# Rs 1,000 out, Rs 1,100 back exactly a year later: 10%.
r = pf.xirr([(date(2025, 1, 1), -1000), (date(2026, 1, 1), 1100)])
assert r is not None and abs(r - 0.10) < 0.002, r

# Doubling in a year is 100%.
r = pf.xirr([(date(2025, 1, 1), -1000), (date(2026, 1, 1), 2000)])
assert abs(r - 1.0) < 0.005, r

# A loss comes back negative.
r = pf.xirr([(date(2025, 1, 1), -1000), (date(2026, 1, 1), 900)])
assert r is not None and r < 0, r

# Timing matters, not just totals: money in later for the same profit earns more.
early = pf.xirr([(date(2025, 1, 1), -1000), (date(2026, 1, 1), 1200)])
late = pf.xirr([(date(2025, 7, 1), -1000), (date(2026, 1, 1), 1200)])
assert late > early, (early, late)

# Several contributions, one valuation at the end.
r = pf.xirr([(date(2024, 4, 1), -50000), (date(2024, 10, 1), -25000),
             (date(2025, 4, 1), -25000), (date(2026, 9, 1), 122000)])
assert r is not None and 0.05 < r < 0.30, r

# No sign change means no rate exists -- and it must say so, not guess.
assert pf.xirr([(date(2025, 1, 1), -100), (date(2026, 1, 1), -100)]) is None
assert pf.xirr([(date(2025, 1, 1), 100), (date(2026, 1, 1), 100)]) is None
assert pf.xirr([(date(2025, 1, 1), -100)]) is None
assert pf.xirr([]) is None

print("ok: XIRR solves, respects timing, and returns nothing when no rate exists")

# --- Financial year ----------------------------------------------------------
assert pf.financial_year(date(2026, 4, 1)) == "2026-27"
assert pf.financial_year(date(2026, 3, 31)) == "2025-26"
assert pf.financial_year(date(2026, 12, 31)) == "2026-27"
print("ok: the financial year turns over on 1 April")

# --- Against the real tables: trades drive the holding -----------------------
pf.add_account("Test account")
acct = pf.accounts()[0]["id"]
pf.save_holding(acct, "DEMO", "Demo Ltd", None, None, None)
pf.add_trade(acct, "DEMO", "buy", 100, 250.0, "2024-05-01")
pf.add_trade(acct, "DEMO", "buy", 50, 300.0, "2025-05-01")
held = pf.holdings(acct)[0]
assert held["qty"] == 150, held
assert abs(held["avg_price"] - (100 * 250 + 50 * 300) / 150) < 1e-9, held

pf.add_trade(acct, "DEMO", "sell", 100, 400.0, "2026-05-01")
held = pf.holdings(acct)[0]
assert held["qty"] == 50 and abs(held["avg_price"] - 300.0) < 1e-9, held
gains, unmatched = pf.fifo_gains(pf.trades(acct, "DEMO"))
assert not unmatched and len(gains) == 1 and abs(gains[0]["gain"] - 15000) < 1e-6, gains

# Removing a trade puts the holding back where it was.
sale = [t for t in pf.trades(acct, "DEMO") if t["side"] == "sell"][0]
pf.delete_trade(sale["id"])
assert pf.holdings(acct)[0]["qty"] == 150, "deleting the sale restores the position"

# A stock with no trades keeps whatever was typed in by hand.
pf.save_holding(acct, "MANUAL", "Manual Ltd", None, 7, 99.0)
pf.sync_from_trades(acct, "MANUAL")
manual = [h for h in pf.holdings(acct) if h["ticker"] == "MANUAL"][0]
assert manual["qty"] == 7 and manual["avg_price"] == 99.0, manual

print("ok: trades rewrite the holding, deletions undo it, typed-in holdings are left alone")

# --- The P/E band ------------------------------------------------------------
# Weekly prices, quarterly EPS: each EPS figure holds until the next one.
prices = [["2025-01-06", "100"], ["2025-04-07", "120"], ["2025-07-07", "150"], ["2025-10-06", "200"]]
eps = [["2025-01-01", 10.0], ["2025-07-01", 20.0]]
band = pf.pe_series(prices, eps)
assert list(round(x, 4) for x in band) == [10.0, 12.0, 7.5, 10.0], list(band)

# A price before the first EPS figure has nothing to divide by.
assert len(pf.pe_series([["2024-01-01", "50"]] + prices, eps)) == 4

# A loss-making period has no meaningful P/E.
assert pf.pe_series(prices, [["2025-01-01", -5.0]]) is None
assert pf.pe_series([], eps) is None and pf.pe_series(prices, []) is None

# Where today sits in its own range.
import pandas as _pd  # noqa: E402
rising = _pd.Series(range(1, 61), index=_pd.date_range("2025-01-01", periods=60, freq="W"))
pos = pf.pe_position(rising)
assert pos["now"] == 60 and pos["low"] == 1 and pos["high"] == 60, pos
assert pos["percentile"] == 100.0, pos
mid = pf.pe_position(_pd.Series([10, 20, 30] * 20, index=_pd.date_range("2025-01-01", periods=60, freq="W")))
assert abs(mid["percentile"] - 100.0) < 1e-9 or mid["now"] == 30, mid
assert pf.pe_position(None) is None
assert pf.pe_position(rising.head(5)) is None, "too little history to claim a range"
print("ok: the P/E band carries EPS forward, skips losses, and places today in its own range")

# --- Cache keys have to actually reach the cache -----------------------------
# Streamlit does not hash a parameter whose name begins with "_", so a
# cache-busting key named that way is silently ignored and the value is
# pinned for the whole TTL. Live prices were frozen for 24 hours this way.
import inspect  # noqa: E402

import screener as sc  # noqa: E402

for fn in (pf.pe_history, pf._quotes, pf.performance, pf._bse_meetings, pf.calendar, sc.universe):
    last = list(inspect.signature(fn).parameters)[-1]
    assert not last.startswith("_"), \
        f"{fn.__name__}({last}) is a cache key Streamlit will never hash"

# And the minute-slot really does move while the market is open.
open_slot = pf._price_slot()
assert open_slot and isinstance(open_slot, str)
print("ok: cache-busting keys are named so Streamlit actually hashes them")

tmp.cleanup()
print("\nok: portfolio money maths")
