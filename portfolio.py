"""
Portfolios: named accounts of holdings, live prices, and a background
refresher that re-analyses every held stock on the schedule set in Settings.

Prices come from Yahoo Finance (yfinance, one batched request for the whole
portfolio); a stock Yahoo does not carry falls back to screener.in's own
price series. Everything else -- financials, filings, news, verdicts -- is the
normal screener scrape in core.py, saved as a run in the archive, so a stock
page is just that company's run history.
"""

import random
import re
import sys
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

import archive
import core
import settings as cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, name TEXT UNIQUE, created REAL);
CREATE TABLE IF NOT EXISTS holdings (
    account_id INTEGER, ticker TEXT, name TEXT, screener_id INTEGER,
    qty REAL, avg_price REAL, added REAL,
    PRIMARY KEY (account_id, ticker));
CREATE TABLE IF NOT EXISTS alert_rules (id INTEGER PRIMARY KEY, ticker TEXT, op TEXT, level REAL,
    created REAL, fired REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, ticker TEXT, kind TEXT, title TEXT,
    body TEXT, read INTEGER DEFAULT 0, notified INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS digests (id INTEGER PRIMARY KEY, week TEXT UNIQUE, ts REAL, data TEXT);
CREATE TABLE IF NOT EXISTS screens (name TEXT PRIMARY KEY, query TEXT);
CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, title TEXT, updated REAL, messages TEXT);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, account_id INTEGER, ticker TEXT, side TEXT,
    qty REAL, price REAL, fees REAL DEFAULT 0, date TEXT, note TEXT, created REAL);
CREATE INDEX IF NOT EXISTS trades_at ON trades(account_id, ticker, date);
"""
_ready = False
IST = timezone(timedelta(hours=5, minutes=30))

# Also in settings.DEFAULTS; repeated here so a server started before this
# feature existed (holding the old settings module) still works.
DEFAULTS = {"refresh_hours": 24, "track_positions": True, "max_stock_pct": 20, "max_sector_pct": 35}
PERF_DEFAULTS = {"background_refresh": True, "warm_charts": True, "chart_cache": 8,
                 "quote_cache": 200, "search_cache": 500, "rank_passages": True,
                 "cpu_limit_pct": 50, "ram_limit_mb": 2048}


def prefs(settings: dict) -> dict:
    return {**DEFAULTS, **settings.get("portfolio", {})}


def perf(settings: dict | None = None) -> dict:
    return {**PERF_DEFAULTS, **((settings or cfg.load()).get("performance") or {})}


# How many of each thing to keep in memory. Read once, at import, because a
# cache's size is fixed when its decorator runs -- the Settings page says
# these take effect when the app restarts, and offers a "clear caches" button
# that works straight away.
_PERF = perf()


def _q(sql: str, params=()) -> list[dict]:
    global _ready
    if not _ready:
        archive.init()  # the runs table, on a fresh install where Portfolio is the first page opened
    with archive.connect() as conn:
        if not _ready:
            conn.executescript(SCHEMA)
            _ready = True
        return [dict(r) for r in conn.execute(sql, params)]


# --- Accounts & holdings -----------------------------------------------------

def accounts() -> list[dict]:
    return _q("SELECT * FROM accounts ORDER BY created")


def add_account(name: str) -> None:
    _q("INSERT OR IGNORE INTO accounts (name, created) VALUES (?, ?)", (name.strip(), time.time()))


def rename_account(account_id: int, name: str) -> None:
    _q("UPDATE accounts SET name=? WHERE id=?", (name.strip(), account_id))


def delete_account(account_id: int) -> None:
    _q("DELETE FROM holdings WHERE account_id=?", (account_id,))
    _q("DELETE FROM accounts WHERE id=?", (account_id,))


def holdings(account_id: int | None = None) -> list[dict]:
    if account_id is None:
        return _q("SELECT h.*, a.name AS account FROM holdings h JOIN accounts a ON a.id=h.account_id")
    return _q("SELECT * FROM holdings WHERE account_id=? ORDER BY added", (account_id,))


def save_holding(account_id: int, ticker: str, name: str, screener_id, qty: float, avg_price: float) -> None:
    _q("INSERT INTO holdings (account_id, ticker, name, screener_id, qty, avg_price, added) "
       "VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id, ticker) DO UPDATE SET qty=excluded.qty, "
       "avg_price=excluded.avg_price",
       (account_id, ticker.upper(), name, screener_id, qty, avg_price, time.time()))
    wake()  # a new stock is analysed straight away, not at the next tick


def remove_holding(account_id: int, ticker: str) -> None:
    _q("DELETE FROM holdings WHERE account_id=? AND ticker=?", (account_id, ticker))


def tracked() -> dict[str, dict]:
    """Every distinct held stock -> {name, screener_id}."""
    return {r["ticker"]: r for r in _q("SELECT ticker, name, screener_id FROM holdings GROUP BY ticker")}


def latest_runs(tickers) -> dict[str, dict]:
    """ticker -> {ts, verdict} of its newest saved analysis, in one query."""
    tickers = list(tickers)
    if not tickers:
        return {}
    marks = ",".join("?" * len(tickers))
    rows = _q(f"SELECT r.ticker, r.ts, json_extract(r.snapshot, '$.ai_verdict') AS verdict FROM runs r "
              f"JOIN (SELECT ticker, MAX(ts) AS ts FROM runs WHERE ticker IN ({marks}) GROUP BY ticker) m "
              f"ON r.ticker = m.ticker AND r.ts = m.ts", tickers)
    return {r["ticker"]: r for r in rows}


# --- Trades: what you actually bought and sold -------------------------------
#
# Optional, and additive. A holding still works exactly as before if you only
# ever type in a quantity and an average price. Log trades for a stock and the
# same holdings row is recomputed from them instead -- so the grid, the
# allocation treemap, the alerts and the performance chart all keep reading
# the one field they always read, and none of that code had to change.

def trades(account_id: int | None = None, ticker: str | None = None) -> list[dict]:
    sql = "SELECT * FROM trades WHERE 1=1"
    params: list = []
    if account_id is not None:
        sql += " AND account_id=?"
        params.append(account_id)
    if ticker:
        sql += " AND ticker=?"
        params.append(ticker.upper())
    return _q(sql + " ORDER BY date, id", params)


def add_trade(account_id: int, ticker: str, side: str, qty: float, price: float,
              date: str, fees: float = 0.0, note: str = "") -> None:
    _q("INSERT INTO trades (account_id, ticker, side, qty, price, fees, date, note, created) "
       "VALUES (?,?,?,?,?,?,?,?,?)",
       (account_id, ticker.upper(), side, float(qty), float(price), float(fees or 0),
        str(date), note, time.time()))
    sync_from_trades(account_id, ticker)


def delete_trade(trade_id: int) -> None:
    rows = _q("SELECT account_id, ticker FROM trades WHERE id=?", (trade_id,))
    _q("DELETE FROM trades WHERE id=?", (trade_id,))
    if rows:
        sync_from_trades(rows[0]["account_id"], rows[0]["ticker"])


def _lots(rows: list[dict]) -> tuple[list[list], list[dict], float]:
    """Walk the trades oldest first, matching sells against buys FIFO.

    FIFO because that is the basis Indian equity taxation uses, so the
    realised gains here line up with what has to be declared.

    Returns (open lots, realised gains, quantity sold with no buy to match).
    """
    lots: list[list] = []   # [date, qty left, cost per share]
    gains: list[dict] = []
    unmatched = 0.0
    for t in sorted(rows, key=lambda r: (str(r["date"]), r["id"])):
        qty, price, fees = float(t["qty"]), float(t["price"]), float(t.get("fees") or 0)
        if qty <= 0:
            continue
        if str(t["side"]).lower() == "buy":
            # Charges belong in the cost of the shares, not beside it.
            lots.append([str(t["date"]), qty, (qty * price + fees) / qty])
            continue
        left = qty
        net_price = price - (fees / qty if qty else 0)
        while left > 1e-9 and lots:
            lot = lots[0]
            take = min(left, lot[1])
            gains.append({"ticker": t["ticker"], "bought": lot[0], "sold": str(t["date"]),
                          "qty": take, "buy_price": lot[2], "sell_price": net_price,
                          "gain": take * (net_price - lot[2]),
                          "days": _days_between(lot[0], str(t["date"]))})
            lot[1] -= take
            left -= take
            if lot[1] <= 1e-9:
                lots.pop(0)
        unmatched += left  # sold more than was ever bought: flagged, never invented
    return lots, gains, unmatched


def _days_between(start: str, end: str) -> int:
    try:
        return (datetime.fromisoformat(end).date() - datetime.fromisoformat(start).date()).days
    except ValueError:
        return 0


LONG_TERM_DAYS = 365  # listed equity: held more than 12 months


def fifo_gains(rows: list[dict]) -> tuple[list[dict], float]:
    """Realised gains per sale, each tagged short or long term."""
    _, gains, unmatched = _lots(rows)
    for g in gains:
        g["term"] = "Long" if g["days"] > LONG_TERM_DAYS else "Short"
    return gains, unmatched


def position_from_trades(rows: list[dict]) -> tuple[float, float | None]:
    """(quantity still held, average cost of those shares) from the open lots."""
    lots, _, _ = _lots(rows)
    qty = sum(lot[1] for lot in lots)
    if qty <= 1e-9:
        return 0.0, None
    return qty, sum(lot[1] * lot[2] for lot in lots) / qty


def sync_from_trades(account_id: int, ticker: str) -> None:
    """Rewrite the holding from its trades, so the rest of the app is unaware
    trades exist. A stock whose position closes out is left at zero rather
    than deleted -- the trades are still the record of it."""
    rows = trades(account_id, ticker)
    if not rows:
        return
    qty, avg = position_from_trades(rows)
    _q("UPDATE holdings SET qty=?, avg_price=? WHERE account_id=? AND ticker=?",
       (qty, avg, account_id, ticker.upper()))


def xirr(flows: list[tuple], guess_lo: float = -0.9999, guess_hi: float = 10.0) -> float | None:
    """The money-weighted annual return of dated cashflows, as a fraction.

    Money out is negative, money in positive, and today's holding counts as a
    final inflow. Solved by bisection rather than Newton: a couple of hundred
    halvings costs nothing on a list this size and, unlike Newton, it cannot
    shoot off to infinity on an awkward set of flows.

    Returns None when there is no sign change, because then no rate exists.
    """
    flows = sorted((d, float(a)) for d, a in flows if a)
    if len(flows) < 2:
        return None
    amounts = [a for _, a in flows]
    if min(amounts) >= 0 or max(amounts) <= 0:
        return None
    start = flows[0][0]
    years = [(d - start).days / 365.0 for d, _ in flows]

    def npv(rate: float) -> float:
        return sum(a / (1.0 + rate) ** y for y, (_, a) in zip(years, flows))

    lo, hi = guess_lo, guess_hi
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None  # not bracketed: no single rate explains these flows
    for _ in range(100):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def cashflows(rows: list[dict], holding_value: float, today=None) -> list[tuple]:
    """Dated cashflows for XIRR: money out on a buy, in on a sell, and what
    the position is worth now as a final inflow. Charges are money out too."""
    flows = []
    for t in rows:
        qty, price, fees = float(t["qty"]), float(t["price"]), float(t.get("fees") or 0)
        try:
            day = datetime.fromisoformat(str(t["date"])).date()
        except ValueError:
            continue
        gross = qty * price
        flows.append((day, -(gross + fees) if str(t["side"]).lower() == "buy" else gross - fees))
    if holding_value:
        flows.append((today or datetime.now(IST).date(), float(holding_value)))
    return flows


FY_START_MONTH = 4  # Indian financial year runs April to March


def financial_year(day) -> str:
    """The Indian FY a date falls in, e.g. '2026-27'."""
    year = day.year if day.month >= FY_START_MONTH else day.year - 1
    return f"{year}-{str(year + 1)[-2:]}"


# --- CSV import --------------------------------------------------------------

COLUMNS = {"ticker": {"ticker", "tickers", "symbol", "scrip", "code", "nse code"},
           "qty": {"quantity", "qty", "shares", "units"},
           "avg_price": {"avg_price", "avg price", "average price", "avg. buy price", "buy price", "price"},
           "account": {"account", "portfolio"}}


def template() -> bytes:
    return pd.DataFrame({"account": ["Long term", "Long term", "Trading"], "ticker": ["RELIANCE", "TCS", "INFY"],
                         "quantity": [10, 5, ""], "avg_price": [1200, 3100, ""]}).to_csv(index=False).encode()


def _num(v) -> float | None:
    v = pd.to_numeric(str(v).replace(",", "").replace("₹", "").strip(), errors="coerce")
    return None if pd.isna(v) or v <= 0 else float(v)


def import_file(uploaded, default_account: int) -> tuple[int, list[str]]:
    """Holdings from a CSV/Excel file. Only ticker is required; quantity, buy
    price and account are optional (a new account name is created). Every
    symbol is checked against screener.in so the stock page has a real company.
    Returns (holdings saved, problems)."""
    try:
        name = (getattr(uploaded, "name", "") or "").lower()
        df = pd.read_excel(uploaded) if name.endswith((".xlsx", ".xls")) else pd.read_csv(uploaded, dtype=str)
    except Exception as exc:
        return 0, [f"Could not read that file: {exc}"]
    cols = {str(c).strip().lower(): c for c in df.columns}
    pick = {k: next((cols[a] for a in aliases if a in cols), None) for k, aliases in COLUMNS.items()}
    pick["ticker"] = pick["ticker"] or df.columns[0]
    account_ids = {a["name"].lower(): a["id"] for a in accounts()}
    saved, problems = 0, []
    for _, row in df.iterrows():
        raw = str(row[pick["ticker"]] if pd.notna(row[pick["ticker"]]) else "").strip().upper()
        if not raw:
            continue
        try:
            match = resolve(raw)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            problems.append(f"{raw}: could not check it ({exc}) — import the file again to retry")
            continue
        if not match:
            problems.append(f"{raw}: not listed on screener.in")
            continue
        raw = match["ticker"]
        acct = default_account
        acct_name = str(row[pick["account"]]).strip() if pick["account"] and pd.notna(row[pick["account"]]) else ""
        if acct_name:
            if acct_name.lower() not in account_ids:
                add_account(acct_name)
                account_ids = {a["name"].lower(): a["id"] for a in accounts()}
            acct = account_ids[acct_name.lower()]
        qty = _num(row[pick["qty"]]) if pick["qty"] else None
        avg = _num(row[pick["avg_price"]]) if pick["avg_price"] else None
        save_holding(acct, raw, match["name"], match["screener_id"], qty, avg)
        saved += 1
    return saved, problems


# --- Search & prices ---------------------------------------------------------

@st.cache_data(ttl=86400, max_entries=_PERF["search_cache"], show_spinner=False)
def search(query: str) -> list[dict]:
    """Every NSE and BSE listing screener.in knows, by name or symbol.

    Raises when screener.in is rate-limiting or unreachable: an exception is
    never cached, so a busy moment is not remembered as "no such company"."""
    for attempt in range(4):
        resp = core.SESSION.get("https://www.screener.in/api/company/search/",
                                params={"q": query}, headers=core._headers(), timeout=core.REQUEST_TIMEOUT)
        if resp.status_code == 429:  # too many requests: wait as told, then back off
            time.sleep(min(float(resp.headers.get("Retry-After") or 0) or 2 ** attempt * 2, 30))
            continue
        resp.raise_for_status()
        out = []
        for row in resp.json():
            parts = row.get("url", "").strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "company":
                out.append({"ticker": parts[1].upper(), "name": row["name"], "screener_id": row.get("id")})
        return out
    raise RuntimeError("screener.in is limiting requests right now")


def safe_search(query: str) -> list[dict] | None:
    """search() for the UI: None means "could not ask", not "no matches"."""
    try:
        return search(query)
    except (requests.RequestException, ValueError, RuntimeError):
        return None


def resolve(symbol: str) -> dict | None:
    """One exact symbol -> {ticker, name, screener_id}, or None if it is not listed."""
    symbol = re.sub(r"^(NSE|BSE):|\.(NS|BO)$", "", symbol.strip().upper())
    match = next((h for h in search(symbol) if h["ticker"] == symbol), None)
    if match:
        return match
    # Search ranks by name, so a short symbol (LT, ITC) can miss its own top
    # results. The company page is the authority -- and the analysis fetches
    # it anyway, so this request is not wasted.
    html = core.fetch_company_page(symbol)
    if not html:
        return None
    name = re.search(r"<h1[^>]*>\s*([^<]+?)\s*</h1>", html)
    sid = re.search(r'data-company-id="(\d+)"', html)
    return {"ticker": symbol, "name": name.group(1) if name else symbol,
            "screener_id": int(sid.group(1)) if sid else None}


def _yahoo(ticker: str) -> str:
    if ticker.startswith("^"):
        return ticker  # an index, e.g. ^NSEI (Nifty 50)
    return f"{ticker}.BO" if ticker.isdigit() else f"{ticker}.NS"  # screener uses BSE codes for BSE-only stocks


def _screener_prices(screener_id, days: int) -> pd.DataFrame | None:
    """Close and volume from screener.in's price chart -- the fallback source."""
    if not screener_id:
        return None
    try:
        resp = core.SESSION.get(f"https://www.screener.in/api/company/{screener_id}/chart/",
                                params={"q": "Price-Volume", "days": days}, headers=core._headers(),
                                timeout=core.REQUEST_TIMEOUT)
        sets = {d["metric"]: d["values"] for d in resp.json()["datasets"]}
        df = pd.DataFrame(sets["Price"], columns=["Date", "Close"]).astype({"Close": float})
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")
        if "Volume" in sets:
            vol = pd.DataFrame([v[:2] for v in sets["Volume"]], columns=["Date", "Volume"])
            df["Volume"] = pd.to_numeric(vol.set_index(pd.to_datetime(vol["Date"]))["Volume"], errors="coerce")
        df["Open"] = df["High"] = df["Low"] = df["Close"]
        return df
    except Exception:
        return None


def pe_series(prices: list, eps: list) -> pd.Series | None:
    """A P/E for every price date, from screener.in's price and TTM EPS series.

    EPS is reported quarterly and the price is weekly, so each EPS figure
    applies until the next one supersedes it -- which is what a trailing P/E
    means. Dates before the first EPS figure have nothing to divide by and
    are dropped, as are loss-making periods, where a P/E is meaningless
    rather than merely large.
    """
    if not prices or not eps:
        return None
    p = pd.Series({pd.to_datetime(d): float(v) for d, v in
                   ((row[0], row[1]) for row in prices)}).sort_index()
    e = pd.Series({pd.to_datetime(d): float(v) for d, v in
                   ((row[0], row[1]) for row in eps)}).sort_index()
    e = e[e > 0]
    if p.empty or e.empty:
        return None
    aligned = e.reindex(p.index.union(e.index)).ffill().reindex(p.index)
    band = (p / aligned).dropna()
    return band if not band.empty else None


def pe_position(band: pd.Series | None) -> dict | None:
    """Where today's P/E sits in its own history: the number that turns
    "P/E 28" into something you can act on."""
    if band is None or len(band) < 12:   # under a year of points says nothing
        return None
    now = float(band.iloc[-1])
    return {"now": now, "low": float(band.min()), "high": float(band.max()),
            "median": float(band.median()), "years": round(len(band) / 52.0, 1),
            "percentile": float((band <= now).mean() * 100)}


@st.cache_data(ttl=86400, max_entries=200, show_spinner=False)
def pe_history(screener_id, day: str) -> pd.Series | None:
    """Five years of trailing P/E, in one request a day per company.

    Same chart endpoint the price fallback already uses, so this adds a
    source of nothing -- only a second question to a page we talk to anyway.
    """
    if not screener_id:
        return None
    try:
        resp = core.SESSION.get(f"https://www.screener.in/api/company/{screener_id}/chart/",
                                params={"q": "Price-EPS", "days": 1830}, headers=core._headers(),
                                timeout=core.REQUEST_TIMEOUT)
        if not resp.ok:
            return None
        sets = {d["metric"]: d["values"] for d in resp.json().get("datasets", [])}
        return pe_series(sets.get("Price") or [], sets.get("EPS") or [])
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def _yf_frame(data: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    if data is None or data.empty:
        return None
    df = data[symbol] if isinstance(data.columns, pd.MultiIndex) else data
    df = df.dropna(subset=["Close"])
    return df if not df.empty else None


def _price_slot() -> str:
    """A cache key that changes every minute while NSE is open (09:15-15:30
    IST, Mon-Fri) and only once per session outside it -- closed-market views
    cost no requests and load instantly."""
    now = datetime.now(IST)
    open_ = now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 35)
    return now.strftime("%Y-%m-%d %H:%M" if open_ else "%Y-%m-%d closed@") + ("" if open_ else (
        "pre" if now.time() < dtime(9, 15) else "post"))


def quotes(stocks: tuple[tuple[str, int | None], ...]) -> dict[str, dict]:
    return _quotes(stocks, _price_slot())


@st.cache_data(ttl=86400, max_entries=_PERF["quote_cache"], show_spinner=False)
def _quotes(stocks: tuple[tuple[str, int | None], ...], slot: str) -> dict[str, dict]:
    """ticker -> {price, change, change_pct, spark}; one Yahoo request for them all.
    spark is the last month of closes, for the card sparklines."""
    import yfinance as yf
    symbols = {t: _yahoo(t) for t, _ in stocks}
    try:
        data = yf.download(list(symbols.values()), period="1mo", interval="1d", group_by="ticker",
                           progress=False, auto_adjust=False, threads=4)  # 4 network waits in parallel: 4.2 s -> 1 s for 30
    except Exception:
        data = None
    out = {}
    for ticker, sid in stocks:
        df = _yf_frame(data, symbols[ticker]) if data is not None else None
        if df is None:
            df = _screener_prices(sid, 31)
        if df is None or df.empty:
            continue
        last = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2]) if len(df) > 1 else last
        out[ticker] = {"price": last, "change": last - prev, "change_pct": (last / prev - 1) * 100 if prev else 0.0,
                       "spark": df["Close"].round(2).tolist(), "asof": pd.Timestamp(df.index[-1]).strftime("%d %b %Y")}
    return out


PERIOD_DAYS = {"1M": 31, "6M": 183, "1Y": 366, "5Y": 1830, "Max": 10000}


@st.cache_data(ttl=900, max_entries=_PERF["chart_cache"], show_spinner=False)  # years of daily bars each
def history(ticker: str, screener_id, period: str) -> pd.DataFrame | None:
    """Daily OHLCV for the chart, with a year of warm-up so the 200-day line starts on screen."""
    import yfinance as yf
    days = PERIOD_DAYS[period]
    try:
        df = _yf_frame(yf.Ticker(_yahoo(ticker)).history(period="max" if days > 3000 else f"{days + 300}d",
                                                         auto_adjust=False), _yahoo(ticker))
    except Exception:
        df = None
    if df is None:
        df = _screener_prices(screener_id, days + 300)
    if df is None:
        return None
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df["SMA50"] = df["Close"].rolling(50).mean()
    df["SMA200"] = df["Close"].rolling(200).mean()
    return df[df.index >= df.index[-1] - pd.Timedelta(days=days)]


def day_slot() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


@st.cache_data(ttl=86400, max_entries=10, show_spinner=False)
def performance(holdings: tuple[tuple[str, float], ...], day: str) -> pd.DataFrame | None:
    """A year of daily value for the current holdings next to the Nifty 50,
    both rebased to 100. One batched request a day. Weighted by quantity when
    known, equally otherwise."""
    import yfinance as yf
    symbols = {_yahoo(t): (t, q) for t, q in holdings}
    try:
        data = yf.download([*symbols, "^NSEI"], period="1y", interval="1d", group_by="ticker",
                           progress=False, auto_adjust=True, threads=4)
    except Exception:
        return None
    closes = pd.DataFrame({sym: data[sym]["Close"] for sym in [*symbols, "^NSEI"]
                           if sym in data.columns.get_level_values(0)}).ffill().dropna(how="all")
    held = [s for s in symbols if s in closes and closes[s].notna().any()]
    if not held or "^NSEI" not in closes:
        return None
    closes = closes.dropna(subset=held + ["^NSEI"], how="any")
    weights = {s: symbols[s][1] or 0 for s in held}
    if sum(weights.values()) <= 0:  # no quantities: an equal-weight basket
        weights = {s: 100 / float(closes[s].iloc[0]) for s in held}
    value = sum(closes[s] * w for s, w in weights.items())
    return pd.DataFrame({"Your holdings": value / value.iloc[0] * 100,
                         "Nifty 50": closes["^NSEI"] / closes["^NSEI"].iloc[0] * 100})


def cap_bucket(market_cap_cr) -> str:
    """ponytail: fixed rupee cut-offs approximate SEBI's rank-based classes; adjust if they drift."""
    if market_cap_cr is None:
        return "Unknown"
    return "Large cap" if market_cap_cr >= 100_000 else "Mid cap" if market_cap_cr >= 30_000 else "Small cap"


def profiles(tickers) -> dict[str, dict]:
    """ticker -> {sector, industry, market_cap} from each newest analysis."""
    tickers = list(tickers)
    if not tickers:
        return {}
    marks = ",".join("?" * len(tickers))
    rows = _q(f"SELECT r.ticker, json_extract(r.snapshot, '$.sector') AS sector, "
              f"json_extract(r.snapshot, '$.industry') AS industry, "
              f"json_extract(r.snapshot, '$.metrics.\"Market Cap\"') AS mcap FROM runs r "
              f"JOIN (SELECT ticker, MAX(ts) AS ts FROM runs WHERE ticker IN ({marks}) GROUP BY ticker) m "
              f"ON r.ticker = m.ticker AND r.ts = m.ts", tickers)
    return {r["ticker"]: {"sector": r["sector"] or "Unknown", "industry": r["industry"] or "",
                          "market_cap": core._to_number(str(r["mcap"] or "").replace("₹", "").replace("Cr.", ""))}
            for r in rows}


@st.cache_data(ttl=86400, show_spinner=False)
def _bse_meetings(day: str) -> list[dict]:
    """Every upcoming results board meeting on BSE -- one request a day for all companies."""
    try:
        resp = core.SESSION.get("https://api.bseindia.com/BseIndiaAPI/api/Corpforthresults/w",
                                headers={**core._headers(), "Referer": "https://www.bseindia.com/"},
                                timeout=core.REQUEST_TIMEOUT)
        return resp.json() if resp.ok else []
    except (requests.RequestException, ValueError):
        return []


@st.cache_data(ttl=43200, max_entries=200, show_spinner=False)
def calendar(ticker: str, name: str, day: str) -> list[dict]:
    """Upcoming dated events for one stock, soonest first: results (with the
    analyst consensus when Yahoo has one), board meetings, dividends."""
    import yfinance as yf
    today = datetime.now(IST).date()
    out = []
    try:
        cal = yf.Ticker(_yahoo(ticker)).calendar or {}
    except Exception:
        cal = {}
    for day in cal.get("Earnings Date") or []:
        eps, rev = cal.get("Earnings Average"), cal.get("Revenue Average")
        note = " · ".join(x for x in (f"EPS est. ₹{eps:,.2f}" if eps else "",
                                      f"revenue est. ₹{rev / 1e7:,.0f} Cr" if rev else "") if x)
        out.append({"date": day, "event": "Quarterly results", "detail": note})
    for key, label in (("Ex-Dividend Date", "Ex-dividend"), ("Dividend Date", "Dividend paid")):
        if cal.get(key):
            out.append({"date": cal[key], "event": label, "detail": ""})
    first = name.split()[0].lower() if name else ""
    for m in _bse_meetings(day):
        if m.get("short_name", "").upper() == ticker or (first and m.get("Long_Name", "").lower().startswith(first + " ")
                                                         and len(first) > 3):
            try:
                out.append({"date": datetime.strptime(m["meeting_date"], "%d %b %Y").date(),
                            "event": "Board meeting (results)", "detail": "Filed on BSE"})
            except (KeyError, ValueError):
                pass
    seen, unique = set(), []
    for e in sorted((e for e in out if e["date"] >= today), key=lambda e: e["date"]):
        if (e["date"], e["event"].split()[0]) not in seen:
            seen.add((e["date"], e["event"].split()[0]))
            unique.append(e)
    return unique


# --- Analysis & the background refresher ------------------------------------

def analyze(ticker: str, settings: dict, label: str = "Portfolio refresh") -> dict:
    """The Research page's full analysis for one stock, saved to its history."""
    categories = tuple(settings["data"]["categories"])
    per_cat = core.TIMEFRAME_OPTIONS.get(settings["data"]["timeframe"], 1)
    previous = archive.previous_run(ticker.upper(), time.time())
    r = core.scrape_all([ticker], settings, categories, per_cat)[0]
    if r["ok"]:
        core.save_run(r, label=label)
        import alerts  # late: alerts imports this module
        alerts.after_analysis(previous["snapshot"] if previous else None, r, settings)
    return r


STATUS = {"running": None, "last": None, "error": None, "queued": 0, "done": 0, "total": 0}
_forced: set[str] = set()  # "re-analyse everything" requests, served first
_WAKE = threading.Event()
_failed: dict[str, float] = {}  # ticker -> when it last failed, so a bad symbol is not retried every minute


def wake() -> None:
    _WAKE.set()


def reanalyse_all() -> int:
    """Queue every held stock for a fresh analysis, in the background."""
    held = tracked()
    _forced.update(held)
    wake()
    return len(held)


def due(refresh_hours: float) -> list[str]:
    """Never-analysed stocks first, then the stalest past the refresh interval."""
    now, held = time.time(), tracked()
    runs = latest_runs(held)
    new = [t for t in held if t not in runs and now - _failed.get(t, 0) > 3600]
    stale = sorted((runs[t]["ts"], t) for t in held if t in runs and refresh_hours
                   and now - runs[t]["ts"] > refresh_hours * 3600 and now - _failed.get(t, 0) > 3600)
    forced = [t for t in held if t in _forced]
    return forced + [t for t in new + [t for _, t in stale] if t not in _forced]


_THIS = sys.modules[__name__]


def _current() -> bool:
    """False once a code change has reloaded this module: the old thread then
    exits and the new module's refresher takes over."""
    return sys.modules.get(__name__) is _THIS


def _loop() -> None:
    # Heavy first imports (yfinance ~2 s, altair ~5 s for the first chart) are
    # paid here in the background at server start, not by whoever opens a page.
    # Altair alone is ~30 MB resident, so it is a choice: off, the first chart
    # takes a few seconds and the app idles lighter.
    if perf().get("warm_charts", True):
        import altair, yfinance  # noqa: F401, E401
    while _current():
        try:
            s = cfg.load()
            queue = due(float(prefs(s)["refresh_hours"]))
            for i, ticker in enumerate(queue):
                if not _current():
                    return
                STATUS.update(running=ticker, queued=len(queue) - i - 1, done=i, total=len(queue))
                started = time.time()
                if not analyze(ticker, s)["ok"]:
                    _failed[ticker] = time.time()
                _forced.discard(ticker)
                STATUS.update(running=None, last=(ticker, time.time()))
                # Same courtesy pause as the Research page, so a long queue is
                # not rate-limited by screener.in; a fully cached stock skips it.
                if time.time() - started > 1 and i < len(queue) - 1:
                    time.sleep(random.uniform(s["data"]["delay_min"], s["data"]["delay_max"]))
            STATUS.update(queued=0, done=0, total=0, error=None)
            import alerts
            alerts.tick(s)  # price alerts, the weekly digest, and sending anything pending
        except Exception as exc:  # the refresher must outlive any one bad run
            STATUS.update(running=None, error=str(exc))
        _WAKE.wait(60)
        _WAKE.clear()


_start_lock = threading.Lock()


def start_refresher() -> threading.Thread | None:
    """One live refresher per server process, however many tabs are open.
    Checked on every rerun: after a code reload the old thread exits, and
    this starts the new module's own (a cached handle would stay dead).

    Returns None when background work is switched off in Settings, in which
    case nothing is analysed unless you ask for it on a page."""
    if not perf().get("background_refresh", True):
        return None
    with _start_lock:
        for t in threading.enumerate():
            if t.name == "portfolio-refresh" and getattr(t, "owner", None) is _THIS and t.is_alive():
                return t
        thread = threading.Thread(target=_loop, name="portfolio-refresh", daemon=True)
        thread.owner = _THIS
        thread.start()
        return thread


if __name__ == "__main__":
    assert _yahoo("RELIANCE") == "RELIANCE.NS" and _yahoo("500325") == "500325.BO"
    hits = search("reliance ind")
    assert hits and hits[0]["ticker"] == "RELIANCE", hits
    q = quotes((("RELIANCE", 2726), ("TMPV", 3370)))
    assert q["RELIANCE"]["price"] > 0, q
    h = history("RELIANCE", 2726, "1Y")
    assert h is not None and len(h) > 200 and h["SMA200"].notna().any()
    print("ok", q)
