"""Web version's API: reuses the app's own scrapers, on one Vercel function.

Stateless on purpose: each user's data lives in their own Firestore documents,
and Vercel's CDN does the caching that st.cache_data does on the desktop app.
Anything that costs real work (analysis, AI, exports) needs a verified sign-in.
"""
import base64
import copy
import functools
import hmac
import io
import ipaddress
import json
import math
import socket
import threading
from datetime import datetime
import os
import re
import sys
import types
from http.server import BaseHTTPRequestHandler
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

os.environ.setdefault("STOCK_HUB_DATA", "/tmp/stock-hub")  # the only writable disk on Vercel
HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[2])]  # deployed bundle, then the repo

# ponytail: stands in for Streamlit so the shared modules import without its
# ~300 MB. They only use it for cache decorators here. Drop this once those
# modules stop importing streamlit.
if find_spec("streamlit") is None:
    _st = types.ModuleType("streamlit")
    _st.cache_data = lambda *a, **k: (lambda fn: fn)
    _ctx = types.ModuleType("streamlit.runtime.scriptrunner")
    _ctx.add_script_run_ctx = _ctx.get_script_run_ctx = lambda *a, **k: None
    sys.modules.update({"streamlit": _st, "streamlit.runtime": types.ModuleType("streamlit.runtime"),
                        "streamlit.runtime.scriptrunner": _ctx})

import pandas as pd  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

import alerts  # noqa: E402
import archive  # noqa: E402
import core  # noqa: E402
import disclosures  # noqa: E402
import llm  # noqa: E402
import portfolio  # noqa: E402
import screener  # noqa: E402
import settings as cfg  # noqa: E402

FIREBASE_KEY = "AIzaSyCvD_Jv5rKuBvus2e6dipnl62HOBa_S9kM"  # public web key, as in app.js
LOOKUP_URL = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={FIREBASE_KEY}"
MAX_BODY = 4 * 1024 * 1024
MAX_SNAPSHOT = 900_000  # Firestore refuses a document over 1 MiB



def _no_worker_pool():
    """A spawned worker re-runs the host's startup script, which here is Vercel's
    server. core.parse_filing falls back to parsing in-process when this raises."""
    raise RuntimeError("no worker processes on Vercel")


core._parse_pool = _no_worker_pool

TICKER_RE = re.compile(r"^[A-Z0-9&_-]{1,20}$")
DAY = 86400


class BadRequest(ValueError):
    pass


class Unauthorized(Exception):
    pass


def verify_user(authorization: str) -> str:
    """The signed-in user's id, checked with Firebase."""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise Unauthorized
    resp = requests.post(LOOKUP_URL, json={"idToken": token}, timeout=10)
    users = resp.json().get("users") if resp.ok else None
    if not users:
        raise Unauthorized
    return users[0]["localId"]


def safe_base_url(url: str) -> str:
    """A user-supplied AI server must be a public address, so nobody can point
    this function at Vercel's or anyone's private network.
    ponytail: checked once, so DNS rebinding and redirects can still slip past;
    move AI calls to the browser if this ever holds secrets worth protecting."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BadRequest("The AI server address must start with http:// or https://.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except OSError:
        raise BadRequest("That AI server address can't be found.") from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise BadRequest("Use an AI server address that's reachable from the internet, not localhost "
                             "or a private network.")
    return url.rstrip("/")


def run_settings(body: dict) -> dict:
    """The app's settings for one request. The user's AI key is used and forgotten."""
    s = copy.deepcopy(cfg.DEFAULTS)
    ai = body.get("ai") or {}
    provider, model = ai.get("provider"), str(ai.get("model") or "")[:200]
    s["budget"]["enabled"] = False  # the spend ledger is per server instance here, so it would mislead
    data = body.get("data") or {}

    def bounded(key, low, high, default):
        try:
            return max(low, min(high, int(data.get(key, default))))
        except (TypeError, ValueError):
            return default
    # ponytail: 80 pages keeps a run inside the 300 s limit; users can raise it and may time out on big reports
    s["data"].update(max_pdf_pages=bounded("max_pdf_pages", 1, 150, 80), max_file_mb=bounded("max_file_mb", 1, 50, 25),
                     doc_workers=bounded("doc_workers", 1, 8, 4), news_items=bounded("news_items", 1, 20, 8))
    s["budget"]["max_tokens_per_run"] = bounded("per_run_tokens", 100, 1_000_000, 50_000)
    if provider in cfg.PROVIDERS and model:
        base = safe_base_url(str(ai.get("base_url") or cfg.PROVIDERS[provider][2]))
        s["ai"].update(enabled=True, provider=provider, model=model, timeout=90)
        s["providers"][provider].update(base_url=base, api_key=str(ai.get("api_key") or ""))
    else:
        s["ai"]["enabled"] = False
    return s


def clean(value):
    """JSON a browser can read: NaN and infinity become null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def _ticker_of(body: dict) -> str:
    return _ticker({"t": [str(body.get("ticker") or "")]})


def analyze(body: dict) -> dict:
    ticker, s = _ticker_of(body), run_settings(body)
    categories = [c for c in body.get("categories") or cfg.DEFAULTS["data"]["categories"] if c in core.WANTED_CATEGORIES]
    per = core.TIMEFRAME_OPTIONS.get(body.get("timeframe"), 1)
    r = core.scrape_all([ticker], s, tuple(categories), per)[0]
    if not r["ok"]:
        return {"ok": False, "ticker": ticker, "error": r.get("error_friendly") or "Couldn't load this company."}
    snap = clean(json.loads(json.dumps(core.snapshot_of(r), default=str)))
    pages = archive.document_pages(ticker, "Annual Report")
    snap["red_flags"] = disclosures.red_flags(pages) if pages else None
    figures = clean(json.loads(json.dumps(r.get("figures") or [], default=str)))
    previous = body.get("previous") if isinstance(body.get("previous"), dict) else None
    if previous:
        snap["changes"] = clean(archive.diff_snapshots(previous, snap))
    snap["screen"] = screen_fields(snap)
    snap["events"] = events_after(previous, r, s)
    snap["figures"] = None
    room = MAX_SNAPSHOT - len(json.dumps(snap))
    if room < 0:  # the least-needed go first when it won't fit
        snap["documents"] = None
        room = MAX_SNAPSHOT - len(json.dumps(snap))
    if room < 0:
        snap["correlated"] = None
        room = MAX_SNAPSHOT - len(json.dumps(snap))
    size = max(len(json.dumps(f)) for f in figures) if figures else 1  # worst case, so it always fits
    snap["figures"] = figures[:max(room, 0) // (size + 2)]
    return snap


_EVENT_LOCK = threading.Lock()


def events_after(previous: dict | None, r: dict, s: dict) -> list[dict]:
    """What the desktop app would file after this analysis, handed to the browser to save."""
    found = []
    with _EVENT_LOCK:
        original, alerts.add_event = alerts.add_event, lambda ticker, kind, title, body: found.append(
            {"ticker": ticker, "kind": kind, "title": title, "body": body})
        try:
            alerts.after_analysis(previous, r, s)
        except Exception:
            pass  # a message is never worth losing the analysis over
        finally:
            alerts.add_event = original
    return clean(json.loads(json.dumps(found, default=str)))


def screen_fields(snap: dict) -> dict:
    """The Screener's row for an analysed company, as screener.universe builds it."""
    m, q = snap.get("metrics") or {}, snap.get("quarterly_df")
    price, book = screener._num(m.get("Current Price")), screener._num(m.get("Book Value"))
    opm, qdf = None, core._df_from_json(q)
    if qdf is not None:
        hit = qdf[qdf.iloc[:, 0].astype(str).str.upper().str.startswith("OPM")]
        opm = screener._num(hit.iloc[0, -1]) if not hit.empty else None
    return clean({"Ticker": snap["ticker"], "Company": snap.get("company_name") or snap["ticker"],
                  "Market Cap": screener._num(m.get("Market Cap")), "Current Price": price,
                  "P/E": screener._num(m.get("Stock P/E")), "Price to Book": price / book if price and book else None,
                  "Book Value": book, "Dividend Yield": screener._num(m.get("Dividend Yield")),
                  "ROCE": screener._num(m.get("ROCE")), "ROE": screener._num(m.get("ROE")),
                  "Sales Growth": screener._yoy(q, "sales") or screener._yoy(q, "revenue"),
                  "Profit Growth": screener._yoy(q, "net profit"), "OPM": opm,
                  "Face Value": screener._num(m.get("Face Value")), "Sector": snap.get("sector") or "",
                  "Verdict": snap.get("ai_verdict") or ""})


_GUIDE_LOCK = threading.Lock()


def guidance(body: dict) -> dict:
    """Find or grade promises. The server's archive is temporary, so a user's
    promises are loaded in for the job and handed straight back."""
    ticker, s = _ticker_of(body), run_settings(body)
    if not s["ai"]["enabled"]:
        raise BadRequest("Choose an AI in Settings first.")
    harvest = body.get("action") == "harvest"
    with _GUIDE_LOCK:  # ponytail: one at a time, because the temporary archive is shared on this instance
        r = core.scrape_all([ticker], s, ("Concall Transcript",) if harvest else (), 1)[0]
        if not r["ok"]:
            raise BadRequest("Couldn't load this company. Try again in a minute.")
        archive.delete_claims(ticker)
        kept = {}
        for c in (body.get("claims") or [])[:100]:
            if c.get("status"):
                kept[c.get("claim")] = c  # already graded: returned untouched, never re-graded
            archive.save_claims(ticker, str(c.get("quarter") or ""), [c])
        if harvest:
            core.harvest_guidance(r, s)
        else:
            core.check_guidance(r, s)
        claims = archive.claims_for(ticker)
        archive.delete_claims(ticker)
    return {"claims": clean([kept.get(c.get("claim")) or c for c in claims])}


def search_reports(body: dict) -> dict:
    """Search inside a company's reports: read them now, then search that text."""
    ticker, question = _ticker_of(body), str(body.get("q") or "").strip()[:200]
    if not question:
        raise BadRequest("Type what you're looking for.")
    wanted = [c for c in body.get("categories") or [] if c in core.WANTED_CATEGORIES] or None
    limit = max(1, min(int(body.get("limit") or 40), 100))
    r = core.scrape_all([ticker], run_settings({"data": body.get("data")}), tuple(wanted or cfg.DEFAULTS["data"]["categories"]), 1)[0]
    if not r["ok"]:
        raise BadRequest("Couldn't load this company. Try again in a minute.")
    hits = archive.search(question, [ticker], wanted, limit)
    return {"hits": clean(json.loads(json.dumps([{k: h.get(k) for k in ("ticker", "category", "page", "snippet", "text")} for h in hits], default=str)))}


def export(body: dict) -> dict:
    import exports
    snaps = body.get("snapshots")
    if not isinstance(snaps, list) or not 1 <= len(snaps) <= 20:
        raise BadRequest("Nothing to download.")
    build = exports.build_word_document if body.get("kind") == "docx" else exports.build_excel_workbook
    return {"file": base64.b64encode(build([core.restore_snapshot(s) for s in snaps])).decode()}


def ask_stream(body: dict):
    """The answer as it is written. Checks run first, so a bad request gets a proper error."""
    question = str(body.get("question") or "").strip()[:2000]
    s = run_settings(body)
    if not question:
        raise BadRequest("Type a question.")
    if not s["ai"]["enabled"]:
        raise BadRequest("Choose an AI in Settings first.")
    passages = []
    for raw in (body.get("filings") or [])[:2]:  # the reports of companies the question names
        ticker = _ticker({"t": [str(raw)]})
        if core.scrape_all([ticker], run_settings({"data": body.get("data")}), tuple(cfg.DEFAULTS["data"]["categories"]), 1)[0]["ok"]:
            passages += [f"[{h['ticker']} {h['category']} p{h['page']}] {str(h.get('text'))[:1200]}"
                         for h in archive.search(question, [ticker], None, 5)]
    context = str(body.get("context") or "")[:12000] + ("\n\nFrom their reports:\n" + "\n".join(passages) if passages else "")
    messages = [{"role": "user" if m.get("role") == "user" else "assistant", "content": str(m.get("text"))[:2000]}
                for m in (body.get("history") or [])[-6:]]
    messages.append({"role": "user", "content": f"SOURCES:\n{context}\n\nQUESTION: {question}"})

    def chunks():
        sent = False
        for chunk in llm.stream(messages, "Answer plainly using the sources, and name the report and page when you use "
                                          "one. Say so when the sources don't cover the question. Never give "
                                          "personalised buy or sell instructions.",
                                s["ai"]["tokens_chat"], core._wide_context(s), "Chat"):
            sent = True
            yield chunk
        if not sent:
            yield llm.friendly(llm.LAST_ERROR)
    return chunks()


def trades_summary(body: dict) -> dict:
    """Profit, tax year and yearly return from a user's trades, by the desktop app's own rules."""
    rows = []
    for i, t in enumerate((body.get("trades") or [])[:5000]):
        try:
            rows.append({"id": i, "ticker": str(t["ticker"]), "account": str(t.get("account") or ""),
                         "side": "sell" if t.get("side") == "sell" else "buy", "qty": float(t["qty"]),
                         "price": float(t["price"]), "date": str(t["date"])[:10], "fees": float(t.get("fees") or 0)})
        except (KeyError, TypeError, ValueError):
            raise BadRequest("A saved trade couldn't be read.") from None
    gains, unmatched = portfolio.fifo_gains(rows)
    for g in gains:
        g["fy"] = portfolio.financial_year(datetime.fromisoformat(g["sold"]).date())
    groups, positions = {}, []
    for r in rows:
        groups.setdefault((r["account"], r["ticker"]), []).append(r)
    for (account, ticker), group in groups.items():
        qty, avg = portfolio.position_from_trades(group)
        positions.append({"account": account, "ticker": ticker, "qty": qty, "avg_price": avg or 0})
    rate = portfolio.xirr(portfolio.cashflows(rows, float(body.get("value") or 0))) if rows else None
    return clean({"gains": gains, "unmatched": unmatched, "rate": rate, "positions": positions})


def upload(body: dict) -> dict:
    """Read an uploaded watchlist or portfolio. Nothing is kept."""
    try:
        raw = base64.b64decode(str(body.get("file") or ""), validate=True)
    except ValueError:
        raise BadRequest("That file couldn't be read.") from None
    name = str(body.get("name") or "").lower()
    if not name.endswith((".csv", ".xlsx")):
        raise BadRequest("Upload a .csv or .xlsx file.")
    handle = io.BytesIO(raw)
    handle.name = name
    if body.get("kind") == "tickers":
        tickers, error = core.tickers_from_upload(handle)
        if error:
            raise BadRequest(error)
        return {"tickers": tickers[:50]}
    try:
        df = pd.read_csv(handle) if name.endswith(".csv") else pd.read_excel(handle, engine="openpyxl")
    except Exception:
        raise BadRequest("That file couldn't be read.") from None
    cols = {str(c).strip().lower(): c for c in df.columns}
    pick = {k: next((cols[a] for a in names if a in cols), None) for k, names in portfolio.COLUMNS.items()}
    pick["ticker"] = pick["ticker"] or df.columns[0]
    found, problems = [], []
    for _, row in df.head(200).iterrows():
        symbol = str(row[pick["ticker"]] if pd.notna(row[pick["ticker"]]) else "").strip().upper()
        if not symbol:
            continue
        try:
            match = portfolio.resolve(symbol)
        except Exception:
            problems.append(f"{symbol}: couldn't check it right now. Import the file again to retry.")
            continue
        if not match:
            problems.append(f"{symbol}: no listed company has this symbol.")
            continue
        value = lambda key: row[pick[key]] if pick.get(key) is not None and pd.notna(row[pick[key]]) else None
        found.append({**match, "account": str(value("account") or "")[:60], "qty": portfolio._num(value("qty")) or 0,
                      "avg_price": portfolio._num(value("avg_price")) or 0})
    return clean({"rows": found, "problems": problems})


def diff(body: dict) -> dict:
    return {"changes": clean(archive.diff_snapshots(body.get("old") or {}, body.get("new") or {}))}


def notify(body: dict) -> dict:
    """Email through the user's own Google Apps Script, as on the desktop app."""
    url = safe_base_url(str(body.get("url") or ""))
    try:
        reply = requests.post(url, timeout=30, json={"token": str(body.get("token") or ""), "subject": str(body.get("subject"))[:200],
                                                     "text": str(body.get("text"))[:5000], "html": alerts.email_page(str(body.get("subject"))[:200], str(body.get("text"))[:5000], "")})
    except requests.RequestException:
        raise BadRequest("Couldn't reach that email address setup.") from None
    if reply.text.strip() != "ok":
        raise BadRequest("The email setup didn't accept that. Check the web address and password.")
    return {"sent": True}


def screen_own(body: dict) -> dict:
    allowed = {"Ticker", "Company", *screener.FIELDS}
    own = [{k: v for k, v in r.items() if k in allowed} for r in (body.get("own") or [])[:500] if isinstance(r, dict) and r.get("Ticker")]
    return _screen({"q": [str(body.get("q") or "")], "sme": ["1" if body.get("sme") else "0"]}, own)


def models(body: dict) -> dict:
    ai = body.get("ai") or {}
    provider = ai.get("provider")
    if provider not in cfg.PROVIDERS:
        raise BadRequest("Choose an AI provider.")
    base = safe_base_url(str(ai.get("base_url") or cfg.PROVIDERS[provider][2]))
    return {"models": llm.list_models(provider, base, str(ai.get("api_key") or ""))}


# --- The daily job: price alerts while nobody has the site open -----------------

def _firestore_token() -> tuple[str, str]:
    """(access token, base URL), from the service account in Vercel's settings."""
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    info = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/datastore"])
    creds.refresh(Request())
    return creds.token, f"https://firestore.googleapis.com/v1/projects/{info['project_id']}/databases/(default)/documents"


def _value(field: dict):
    """A plain value from Firestore's typed JSON."""
    kind, value = next(iter(field.items()))
    if kind in ("integerValue", "doubleValue"):
        return float(value)
    return None if kind == "nullValue" else value


def _fields(document: dict) -> dict:
    return {k: _value(v) for k, v in (document.get("fields") or {}).items()}


def _latest_close(rule: dict) -> float | None:
    sid = rule.get("screener_id")
    if not sid:
        match = portfolio.resolve(str(rule.get("ticker")))
        sid = match and match.get("screener_id")
    df = portfolio._screener_prices(int(sid), 10) if sid else None
    return None if df is None or df.empty else float(df["Close"].iloc[-1])


def run_alert_job() -> dict:
    """Fire every price alert that has been reached. Returns counts only, never anyone's data."""
    token, base = _firestore_token()
    headers = {"Authorization": f"Bearer {token}"}
    # No filter on purpose: a filter on all users' alerts needs a special index set up by hand.
    found = requests.post(f"{base}:runQuery", headers=headers, timeout=30, json={
        "structuredQuery": {"from": [{"collectionId": "alerts", "allDescendants": True}], "limit": 5000}})
    found.raise_for_status()  # a refused query must not look like "no alerts"
    closes, fired, failed, checked = {}, 0, 0, 0
    for row in found.json():
        document = row.get("document")
        if not document:
            continue
        checked += 1
        rule = _fields(document)
        if rule.get("fired"):
            continue
        try:
            ticker = str(rule["ticker"])
            if ticker not in closes:
                closes[ticker] = _latest_close(rule)
            price, level = closes[ticker], float(rule["level"])
            if price is None or not (price >= level if rule.get("op") == "above" else price <= level):
                continue
            requests.patch(f"https://firestore.googleapis.com/v1/{document['name']}?updateMask.fieldPaths=fired", headers=headers,
                           timeout=15, json={"fields": {"fired": {"booleanValue": True}}}).raise_for_status()
            # .../documents/users/<uid>/alerts/<id>
            uid = document["name"].split("/documents/users/", 1)[1].split("/", 1)[0]
            title, body = f"{ticker}: price went {rule.get('op')} ₹{level:,.2f}", f"Its last close was ₹{price:,.2f}."
            requests.post(f"{base}/users/{uid}/events", headers=headers, timeout=15, json={"fields": {
                "ts": {"integerValue": str(int(datetime.now().timestamp() * 1000))}, "ticker": {"stringValue": ticker},
                "kind": {"stringValue": "price"}, "title": {"stringValue": title}, "body": {"stringValue": body},
                "read": {"booleanValue": False}}}).raise_for_status()
            fired += 1
            _email_user(base, headers, uid, title, body)
        except Exception:
            failed += 1  # one bad alert must not stop everyone else's
    print(f"alert job: checked={checked} fired={fired} failed={failed}", flush=True)
    return {"fired": fired, "failed": failed}


def _email_user(base: str, headers: dict, uid: str, title: str, body: str) -> None:
    """Only for users who chose to keep their email setup on the server."""
    saved = requests.get(f"{base}/users/{uid}/settings/email", headers=headers, timeout=15)
    if not saved.ok:
        return
    setup = _fields(saved.json())
    if setup.get("url") and setup.get("token"):
        notify({"url": setup["url"], "token": setup["token"], "subject": title, "text": body})


def cron(authorization: str) -> tuple[int, dict]:
    """Only Vercel's scheduler knows the secret. With no secret set, nothing runs."""
    secret = os.environ.get("CRON_SECRET")
    if not secret or not hmac.compare_digest(authorization.encode(), f"Bearer {secret}".encode()):
        return 401, {"error": "Not allowed."}
    if not os.environ.get("FIREBASE_SERVICE_ACCOUNT"):
        return 503, {"error": "The service account isn't set up yet."}
    try:
        return 200, run_alert_job()
    except Exception:
        return 502, {"error": "The alert check failed."}


POST_ROUTES = {"analyze": analyze, "guidance": guidance, "search-reports": search_reports, "export": export,
               "models": models, "trades": trades_summary, "upload": upload, "diff": diff, "notify": notify,
               "screen": screen_own}


def _ticker(q: dict) -> str:
    ticker = (q.get("t", [""])[0]).strip().upper()
    if not TICKER_RE.match(ticker):
        raise BadRequest("Invalid ticker.")
    return ticker


def _table(soup, section) -> dict | None:
    try:
        df = core.sanitize_df_strings(core._extract_table(soup, section))
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return {"columns": [str(c) for c in df.columns], "rows": df.fillna("").astype(str).values.tolist()}


def company(q: dict) -> dict | None:
    ticker = _ticker(q)
    html = core.fetch_company_page(ticker)
    if html is None:
        return None
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one("h1")
    metrics = {}
    for li in soup.select("#top-ratios li"):
        name, value = li.select_one(".name"), li.select_one(".value")
        if name and value:
            metrics[core.sanitize_text(name.get_text(strip=True))] = core.sanitize_text(value.get_text(strip=True))
    return {"ticker": ticker, "name": core.sanitize_text(title.get_text(strip=True)) if title else ticker,
            "metrics": metrics,
            "pros": [core.sanitize_text(li.get_text(strip=True)) for li in soup.select("div.pros li")],
            "cons": [core.sanitize_text(li.get_text(strip=True)) for li in soup.select("div.cons li")],
            "quarters": _table(soup, "quarters")}


def search(q: dict) -> list[dict]:
    query = q.get("q", [""])[0].strip()
    if not 1 <= len(query) <= 60:
        raise BadRequest("Search must be 1 to 60 characters.")
    return portfolio.search(query)


def prices(q: dict) -> list | None:
    raw = q.get("id", [""])[0]
    if not raw.isdigit():
        raise BadRequest("Invalid company id.")
    days = q.get("days", ["366"])[0]
    if not days.isdigit() or not 1 <= int(days) <= 10000:
        raise BadRequest("Invalid period.")
    df = portfolio._screener_prices(int(raw), int(days))
    if df is None or df.empty:
        return None
    volume = df["Volume"] if "Volume" in df else [0] * len(df)
    return [[d.strftime("%Y-%m-%d"), float(c), 0 if v != v else float(v)]
            for d, c, v in zip(df.index, df["Close"], volume)]


def news(q: dict) -> list[dict]:
    ticker = _ticker(q)
    items = core.fetch_company_news(ticker, q.get("name", [ticker])[0][:120])
    return [{"title": n.get("title") or "", "link": n.get("url") or "", "source": n.get("source") or ""}
            for n in items[:15]]


def providers(q: dict) -> list[dict]:
    return [{"id": pid, "label": v[0], "base_url": v[2], "needs_key": v[3]} for pid, v in cfg.PROVIDERS.items()]


def meta(q: dict) -> dict:
    return {"categories": core.WANTED_CATEGORIES, "words": core.CATEGORY_WORDS, "timeframes": list(core.TIMEFRAME_OPTIONS),
            "statements": core.STATEMENT_TABLES, "presets": screener.PRESETS,
            "fields": {f: names[:2] for f, names in screener.FIELDS.items()}, "market": sorted(screener.MARKET_FIELDS), "units": screener.UNITS}


def _sid(q: dict) -> int:
    raw = q.get("id", [""])[0]
    if not raw.isdigit():
        raise BadRequest("Invalid company id.")
    return int(raw)


def valuation(q: dict) -> dict:
    return {"band": clean(portfolio.pe_position(portfolio.pe_history(_sid(q), portfolio.day_slot())) or None)}


def calendar(q: dict) -> list[dict]:
    ticker = _ticker(q)
    rows = portfolio.calendar(ticker, q.get("name", [ticker])[0][:120], portfolio.day_slot())
    return [{"date": e["date"].isoformat(), "event": e["event"], "detail": e.get("detail") or ""} for e in rows[:4]]


@functools.lru_cache(maxsize=4)
def _market(day: str, sme: bool):
    return screener.everything(screener.version(), day, sme)


def screen(q: dict) -> dict:
    return _screen(q, [])


def _screen(q: dict, own: list[dict]) -> dict:
    sme = q.get("sme", ["0"])[0] == "1"
    df = _market(portfolio.day_slot(), sme)
    if own:  # what this user's own checks know fills the gaps; today's market figure still wins
        df = df.set_index("Ticker").combine_first(pd.DataFrame(own).set_index("Ticker")).reset_index()
        df["SME"] = df["SME"].fillna(False).astype(bool)
    try:
        groups = screener.parse(q.get("q", [""])[0][:500])
    except ValueError as exc:
        raise BadRequest(f"The app didn't understand that search. {exc}") from None
    found = screener.run(df, groups)
    shown = ["Ticker", "Company", *[f for f in screener.fields_used(groups) if f not in ("Ticker", "Company")]]
    for extra in ("Current Price", "Market Cap", "P/E", "Price to Book", "Dividend Yield", "1Y Return", "Verdict"):
        if extra not in shown and len(shown) < 9:
            shown.append(extra)
    shown = [c for c in shown if c in found]
    if len(shown) > 2:  # biggest first on the first measure, before the list is cut to 300
        found = found.sort_values(shown[2], ascending=False)
    return clean({"total": len(df), "count": len(found), "columns": shown, "units": screener.UNITS,
                  "has_market": bool(df["Current Price"].notna().any()),
                  "rows": found[shown].astype(object).where(found[shown].notna(), None).head(300).values.tolist()})


def listed(q: dict) -> list:
    """Every NSE company, for the search box to filter as you type."""
    df = screener.listed(portfolio.day_slot())
    return df[["Ticker", "Company", "SME"]].values.tolist()


def performance(q: dict) -> dict | None:
    """₹100 in today's holdings a year ago, against the Nifty 50."""
    holds = []
    for part in q.get("h", [""])[0].split(",")[:100]:
        ticker, _, qty = part.partition(":")
        if TICKER_RE.match(ticker) and qty.replace(".", "", 1).isdigit():
            holds.append((ticker, float(qty)))
    if not holds:
        raise BadRequest("No holdings.")
    df = portfolio.performance(tuple(sorted(holds)), portfolio.day_slot())
    if df is None or df.empty:
        return None
    return clean({"dates": [d.strftime("%Y-%m-%d") for d in df.index], "you": df["Your holdings"].astype(float).tolist(),
                  "nifty": df["Nifty 50"].astype(float).tolist()})


def bars(q: dict) -> list | None:
    """Candles with both averages, as on the desktop app."""
    period = q.get("period", ["1Y"])[0]
    if period not in portfolio.PERIOD_DAYS:
        raise BadRequest("Invalid period.")
    raw = q.get("id", [""])[0]
    df = portfolio.history(_ticker(q), int(raw) if raw.isdigit() else None, period)
    if df is None or df.empty:
        return None
    return clean([[d.strftime("%Y-%m-%d"), *[float(x) for x in row]] for d, row in
                  zip(df.index, df[["Open", "High", "Low", "Close", "Volume", "SMA50", "SMA200"]].values)])


ROUTES = {"bars": (bars, 900), "listed": (listed, DAY), "performance": (performance, 3600), "search": (search, DAY), "company": (company, 900), "prices": (prices, 900), "news": (news, 900),
          "providers": (providers, DAY), "meta": (meta, DAY), "valuation": (valuation, DAY),
          "calendar": (calendar, DAY), "screen": (screen, 3600)}


def respond(path: str, query: str) -> tuple[int, dict | list, int]:
    """(status, body, seconds to cache). Failures are never cached."""
    route = ROUTES.get(path.rstrip("/").rsplit("/", 1)[-1])
    if not route:
        return 404, {"error": "Not found."}, 0
    try:
        body = route[0](parse_qs(query))
    except BadRequest as exc:
        return 400, {"error": str(exc)}, 0
    except Exception:
        return 502, {"error": "The data source did not answer. Try again shortly."}, 0
    if body is None:
        return 404, {"error": "Nothing found."}, 0
    return 200, body, route[1]


def respond_post(route: str, authorization: str, raw: bytes) -> tuple[int, dict]:
    fn = POST_ROUTES.get(route)
    if not fn:
        return 404, {"error": "Not found."}
    try:
        verify_user(authorization)
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise BadRequest("Bad request.")
        return 200, fn(body)
    except Unauthorized:
        return 401, {"error": "Please sign in again."}
    except (BadRequest, json.JSONDecodeError) as exc:
        return 400, {"error": str(exc)}
    except Exception:
        return 502, {"error": "That didn't work. Try again in a minute."}


class handler(BaseHTTPRequestHandler):
    def _ask(self, raw: bytes):
        """Streams the reply. (status, error) for a failure, (200, None) once sent."""
        try:
            verify_user(self.headers.get("Authorization", ""))
            chunks = ask_stream(json.loads(raw or b"{}"))
        except Unauthorized:
            return 401, {"error": "Please sign in again."}
        except (BadRequest, json.JSONDecodeError) as exc:
            return 400, {"error": str(exc)}
        except Exception:
            return 502, {"error": "That didn't work. Try again in a minute."}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for chunk in chunks:
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except Exception:
            self.wfile.write("\n\nThe answer stopped early. Please try again.".encode())
        return 200, None

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            status, body = 413, {"error": "That request is too large."}
        else:
            route = parse_qs(url.query).get("route", [url.path])[0].rstrip("/").rsplit("/", 1)[-1]
            raw = self.rfile.read(length)
            if route == "ask":
                status, body = self._ask(raw)
                if body is None:
                    return
            else:
                status, body = respond_post(route, self.headers.get("Authorization", ""), raw)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        url = urlparse(self.path)
        if parse_qs(url.query).get("route", [url.path])[0].rstrip("/").rsplit("/", 1)[-1] == "cron":
            status, body = cron(self.headers.get("Authorization", ""))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
            return
        # vercel.json's rewrite hides the original path, so it passes the route along.
        status, body, ttl = respond(parse_qs(url.query).get("route", [url.path])[0], url.query)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", f"public, s-maxage={ttl}" if ttl else "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())
