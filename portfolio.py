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
"""
_ready = False

# Also in settings.DEFAULTS; repeated here so a server started before this
# feature existed (holding the old settings module) still works.
DEFAULTS = {"refresh_hours": 24, "track_positions": True}


def prefs(settings: dict) -> dict:
    return {**DEFAULTS, **settings.get("portfolio", {})}


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
    rows = _q(f"SELECT ticker, MAX(ts) AS ts, json_extract(snapshot, '$.ai_verdict') AS verdict "
              f"FROM runs WHERE ticker IN ({marks}) GROUP BY ticker", tickers)
    return {r["ticker"]: r for r in rows}


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

@st.cache_data(ttl=86400, max_entries=2000, show_spinner=False)
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


def _yf_frame(data: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    if data is None or data.empty:
        return None
    df = data[symbol] if isinstance(data.columns, pd.MultiIndex) else data
    df = df.dropna(subset=["Close"])
    return df if not df.empty else None


@st.cache_data(ttl=60, show_spinner=False)
def quotes(stocks: tuple[tuple[str, int | None], ...]) -> dict[str, dict]:
    """ticker -> {price, change, change_pct, spark}; one Yahoo request for them all.
    spark is the last month of closes, for the card sparklines."""
    import yfinance as yf
    symbols = {t: _yahoo(t) for t, _ in stocks}
    try:
        data = yf.download(list(symbols.values()), period="1mo", interval="1d", group_by="ticker",
                           progress=False, auto_adjust=False, threads=False)
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
                       "spark": df["Close"].round(2).tolist()}
    return out


PERIOD_DAYS = {"1M": 31, "6M": 183, "1Y": 366, "5Y": 1830, "Max": 10000}


@st.cache_data(ttl=900, show_spinner=False)
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


# --- Analysis & the background refresher ------------------------------------

def analyze(ticker: str, settings: dict, label: str = "Portfolio refresh") -> dict:
    """The Research page's full analysis for one stock, saved to its history."""
    categories = tuple(settings["data"]["categories"])
    per_cat = core.TIMEFRAME_OPTIONS.get(settings["data"]["timeframe"], 1)
    r = core.scrape_all([ticker], settings, categories, per_cat)[0]
    if r["ok"]:
        core.save_run(r, label=label)
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
        except Exception as exc:  # the refresher must outlive any one bad run
            STATUS.update(running=None, error=str(exc))
        _WAKE.wait(60)
        _WAKE.clear()


_start_lock = threading.Lock()


def start_refresher() -> threading.Thread:
    """One live refresher per server process, however many tabs are open.
    Checked on every rerun: after a code reload the old thread exits, and
    this starts the new module's own (a cached handle would stay dead)."""
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
