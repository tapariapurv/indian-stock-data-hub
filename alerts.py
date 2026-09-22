"""
Alerts, the weekly digest, and how they reach you.

Everything is worked out from data the app already holds -- saved analyses,
the filings list, graded guidance and live prices -- so an alert or digest
never states a number the app did not actually see. Each event lands in the
in-app inbox first; delivery (a macOS notification and/or an email sent by
your own Google Apps Script web app) is batched once per refresher cycle, so
a re-analysis of thirty stocks sends one message, not thirty.
"""

import html
import json
import subprocess
import time
from datetime import datetime, timedelta

import requests

import portfolio as pf

DEFAULTS = {"channels": ["mac"], "email_url": "", "email_token": "", "verdict": True, "filing": True,
            "guidance": True, "price": True, "digest": True, "digest_day": 0}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
KIND_LABEL = {"verdict": "Verdict change", "filing": "New filing", "guidance": "Guidance missed",
              "price": "Price alert"}


def prefs(settings: dict) -> dict:
    return {**DEFAULTS, **settings.get("notify", {})}


# --- Inbox -------------------------------------------------------------------

def add_event(ticker: str, kind: str, title: str, body: str) -> None:
    pf._q("INSERT INTO events (ts, ticker, kind, title, body) VALUES (?,?,?,?,?)",
          (time.time(), ticker, kind, title, body))


def events(limit: int = 200, since: float = 0) -> list[dict]:
    return pf._q("SELECT * FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT ?", (since, limit))


def unread() -> int:
    return pf._q("SELECT COUNT(*) AS n FROM events WHERE read = 0")[0]["n"]


def mark_all_read() -> None:
    pf._q("UPDATE events SET read = 1 WHERE read = 0")


# --- Price rules ---------------------------------------------------------------

def rules(ticker: str | None = None) -> list[dict]:
    if ticker:
        return pf._q("SELECT * FROM alert_rules WHERE ticker=? ORDER BY level", (ticker,))
    return pf._q("SELECT * FROM alert_rules ORDER BY ticker, level")


def add_rule(ticker: str, op: str, level: float) -> None:
    pf._q("INSERT INTO alert_rules (ticker, op, level, created) VALUES (?,?,?,?)", (ticker, op, level, time.time()))


def delete_rule(rule_id: int) -> None:
    pf._q("DELETE FROM alert_rules WHERE id=?", (rule_id,))


def check_prices(settings: dict) -> None:
    """Fire each rule once when the price crosses its level; it re-arms when
    the price goes back. Costs nothing unless a rule exists, and asks for
    prices only when they can have moved (the quote cache is per market minute)."""
    all_rules = rules()
    if not all_rules or not prefs(settings)["price"]:
        return
    held = pf.tracked()
    wanted = tuple(sorted({(r["ticker"], (held.get(r["ticker"]) or {}).get("screener_id")) for r in all_rules}))
    quotes = pf.quotes(wanted)
    for r in all_rules:
        q = quotes.get(r["ticker"])
        if not q:
            continue
        crossed = q["price"] >= r["level"] if r["op"] == "above" else q["price"] <= r["level"]
        if crossed and not r["fired"]:
            add_event(r["ticker"], "price", f"{r['ticker']} is {r['op']} ₹{r['level']:,.2f}",
                      f"{r['ticker']} traded at ₹{q['price']:,.2f} ({q['change_pct']:+.2f}% today), "
                      f"{'above' if r['op'] == 'above' else 'below'} your alert level of ₹{r['level']:,.2f}.")
            pf._q("UPDATE alert_rules SET fired=? WHERE id=?", (time.time(), r["id"]))
        elif not crossed and r["fired"]:
            pf._q("UPDATE alert_rules SET fired=NULL WHERE id=?", (r["id"],))


# --- After each analysis -------------------------------------------------------

def _filing_urls(snapshot: dict | None) -> dict[str, set]:
    return {cat: {d["url"] for d in docs if d.get("url")}
            for cat, docs in ((snapshot or {}).get("downloaded") or {}).items()}


def after_analysis(previous: dict | None, result: dict, settings: dict) -> None:
    """Compare a fresh analysis with the one before it and file what changed.
    A stock's first analysis only sets the baseline: nothing to compare yet."""
    if previous is None:
        return
    n, ticker = prefs(settings), result["ticker"]
    name = result.get("company_name") or ticker
    old_v, new_v = previous.get("ai_verdict"), result.get("ai_verdict")
    if n["verdict"] and old_v and new_v and old_v != new_v:
        rank = {"Cautious": 0, "Neutral": 1, "Positive": 2}
        way = "up" if rank.get(new_v, 1) > rank.get(old_v, 1) else "down"
        add_event(ticker, "verdict", f"{ticker}: AI verdict {old_v} → {new_v}",
                  f"The latest analysis of {name} rates it {new_v}, {way} from {old_v}. "
                  f"{(result.get('ai_summary') or '').strip()}".strip())
    before, after = _filing_urls(previous), _filing_urls(result)
    fresh = [cat for cat, urls in after.items() if urls - before.get(cat, set()) and before.get(cat)]
    if n["filing"]:
        for cat in fresh:
            add_event(ticker, "filing", f"{ticker}: new {cat.lower()}",
                      f"{name} has published a new {cat.lower()}. It has been downloaded, its figures "
                      f"extracted, and it is now searchable in the archive.")
    features = settings["features"]
    if n["guidance"] and features.get("guidance") and settings["ai"].get("enabled"):
        import archive
        import core
        started = time.time()
        if "Concall Transcript" in fresh:
            core.harvest_guidance(result, settings)
        core.check_guidance(result, settings)
        archive.init()
        with archive.connect() as conn:
            missed = conn.execute("SELECT quarter, claim, why FROM guidance WHERE ticker=? AND status='Missed' "
                                  "AND checked_at >= ?", (ticker, started)).fetchall()
        for m in missed:
            add_event(ticker, "guidance", f"{ticker}: guidance missed",
                      f"In the {m['quarter']} concall, management said: “{m['claim']}”. "
                      f"Checked against the results reported since, it was missed. {m['why'] or ''}".strip())


# --- Delivery --------------------------------------------------------------------

def _esc(text) -> str:
    return html.escape(str(text or ""))


def email_page(title: str, intro: str, body_html: str) -> str:
    """One clean, inline-styled layout for every email (mail apps ignore <style>)."""
    return f"""<div style="background:#f4f2ee;padding:24px 0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" style="max-width:640px;margin:0 auto;background:#fff;border-radius:12px;border:1px solid #e3e0d8">
<tr><td style="padding:24px 28px 8px">
<div style="font-size:12px;letter-spacing:.08em;color:#6b6f78;text-transform:uppercase">Indian Stock Data Hub</div>
<h1 style="margin:6px 0 4px;font-size:22px;color:#1c1f26;font-weight:600">{_esc(title)}</h1>
<p style="margin:0;color:#3a3f4b;font-size:14px;line-height:1.5">{_esc(intro)}</p></td></tr>
<tr><td style="padding:8px 28px 24px;color:#1c1f26;font-size:14px;line-height:1.5">{body_html}</td></tr>
<tr><td style="padding:14px 28px;border-top:1px solid #eeede9;color:#8a8e97;font-size:12px">
Generated on your computer from your own saved analyses and market data. Not investment advice.</td></tr>
</table></div>"""


def _section(title: str, rows: list[str]) -> str:
    if not rows:
        return ""
    return (f'<h2 style="font-size:15px;margin:20px 0 8px;color:#1f4e79">{_esc(title)}</h2>'
            f'<table width="100%" style="border-collapse:collapse">{"".join(rows)}</table>')


def _row(left: str, right: str = "", note: str = "") -> str:
    return (f'<tr><td style="padding:7px 0;border-bottom:1px solid #f0efeb;vertical-align:top">{left}'
            + (f'<div style="color:#6b6f78;font-size:12.5px">{note}</div>' if note else "")
            + f'</td><td style="padding:7px 0;border-bottom:1px solid #f0efeb;text-align:right;'
              f'white-space:nowrap;vertical-align:top">{right}</td></tr>')


def _pct(x) -> str:
    if x is None:
        return "—"
    return f'<span style="color:{"#1e8e5a" if x >= 0 else "#c8453b"};font-weight:600">{x:+.2f}%</span>'


def notify(title: str, text: str, settings: dict, html_body: str | None = None) -> list[str]:
    """Send through every chosen channel. Returns the problems, if any."""
    n, problems = prefs(settings), []
    if "mac" in n["channels"]:
        script = f"display notification {json.dumps(text[:230], ensure_ascii=False)} " \
                 f"with title {json.dumps(title[:80], ensure_ascii=False)}"
        try:
            subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append(f"macOS notification: {exc}")
    if "email" in n["channels"]:
        if not (n["email_url"] and n["email_token"]):
            problems.append("email: not set up (Settings → Notifications)")
        else:
            try:
                resp = requests.post(n["email_url"], timeout=30, json={
                    "token": n["email_token"], "subject": title, "text": text,
                    "html": html_body or email_page(title, text, "")})
                if resp.text.strip() != "ok":
                    problems.append(f"email: the web app replied “{resp.text.strip()[:120]}”")
            except requests.RequestException as exc:
                problems.append(f"email: {exc}")
    return problems


def flush(settings: dict) -> None:
    """Deliver every event not yet sent, as one message."""
    pending = pf._q("SELECT * FROM events WHERE notified = 0 ORDER BY ts")
    if not pending:
        return
    if len(pending) == 1:
        e = pending[0]
        title, text = e["title"], e["body"]
    else:
        title = f"{len(pending)} new portfolio alerts"
        text = "; ".join(e["title"] for e in pending)
    rows = [_row(f"<b>{_esc(e['title'])}</b>", _esc(KIND_LABEL.get(e["kind"], "")), _esc(e["body"])) for e in pending]
    notify(title, text, settings, email_page(title, "Here is what changed in your portfolio.",
                                             _section("Alerts", rows)))
    pf._q(f"UPDATE events SET notified = 1 WHERE id IN ({','.join('?' * len(pending))})",
          [e["id"] for e in pending])


# --- Weekly digest -----------------------------------------------------------------

def _week_key(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def build_digest(settings: dict) -> dict:
    """Everything the digest says, computed from stored data only."""
    held, rows = pf.tracked(), pf.holdings()
    stocks = tuple(sorted((t, v["screener_id"]) for t, v in held.items()))
    quotes = pf.quotes(stocks + (("^NSEI", None),))
    week = {}
    for t, _ in stocks + (("^NSEI", None),):
        spark = (quotes.get(t) or {}).get("spark") or []
        week[t] = (spark[-1] / spark[-6] - 1) * 100 if len(spark) >= 6 and spark[-6] else None
    now_value = then_value = 0.0
    for h in rows:
        spark = (quotes.get(h["ticker"]) or {}).get("spark") or []
        if h.get("qty") and len(spark) >= 6:
            now_value += h["qty"] * spark[-1]
            then_value += h["qty"] * spark[-6]
    moves = [w for t, w in week.items() if t != "^NSEI" and w is not None]
    runs = pf.latest_runs(held)
    since = time.time() - 7 * 86400
    today = datetime.now(pf.IST).date()
    upcoming = []
    for t, v in held.items():
        for e in pf.calendar(t, v["name"], pf.day_slot()):
            if e["date"] <= today + timedelta(days=14):
                upcoming.append({"ticker": t, **e, "date": e["date"].isoformat()})
    return {
        "week": _week_key(datetime.now(pf.IST)), "made": time.time(),
        "asof": next((q["asof"] for q in quotes.values() if q.get("asof")), ""),
        "value": now_value or None, "value_change": (now_value / then_value - 1) * 100 if then_value else None,
        "basket_change": sum(moves) / len(moves) if moves else None, "nifty_change": week.get("^NSEI"),
        "movers": sorted(({"ticker": t, "name": held[t]["name"], "week": w,
                           "price": (quotes.get(t) or {}).get("price")}
                          for t, w in week.items() if t in held and w is not None), key=lambda m: -m["week"]),
        "events": [dict(e) for e in events(500, since)],
        "upcoming": sorted(upcoming, key=lambda e: e["date"]),
        "verdicts": {**{v: sum(1 for r in runs.values() if r.get("verdict") == v)
                        for v in ("Positive", "Neutral", "Cautious")},
                     "Pending": len(held) - sum(1 for r in runs.values() if r.get("verdict"))},
        "stocks": len(held),
    }


def digest_html(d: dict) -> str:
    change = d["value_change"] if d["value_change"] is not None else d["basket_change"]
    what = "Your portfolio" if d["value_change"] is not None else "Your holdings, on average,"
    intro = (f"{what} moved {change:+.2f}% this week" if change is not None else "Prices were unavailable this week") \
        + (f", against {d['nifty_change']:+.2f}% for the Nifty 50" if d["nifty_change"] is not None else "") \
        + (f". Prices as of the close on {d['asof']}." if d["asof"] else ".")
    head = (f'<table width="100%" style="border-collapse:collapse;margin:8px 0"><tr>'
            + "".join(f'<td style="padding:10px;background:#f7f6f2;border-radius:8px;text-align:center">'
                      f'<div style="color:#6b6f78;font-size:12px">{k}</div><div style="font-size:18px;font-weight:600">{v}</div></td>'
                      f'<td style="width:8px"></td>'
                      for k, v in (("Value", f"₹{d['value']:,.0f}" if d["value"] else "—"),
                                   ("This week", _pct(change)), ("Nifty 50", _pct(d["nifty_change"])),
                                   ("Stocks", d["stocks"])))
            + "</tr></table>")
    movers = d["movers"]
    best = [_row(f"<b>{_esc(m['ticker'])}</b>", _pct(m["week"]), _esc(m["name"])) for m in movers[:5]]
    worst = [_row(f"<b>{_esc(m['ticker'])}</b>", _pct(m["week"]), _esc(m["name"]))
             for m in reversed(movers[-5:]) if len(movers) > 5]
    by_kind = {}
    for e in d["events"]:
        by_kind.setdefault(e["kind"], []).append(_row(f"<b>{_esc(e['title'])}</b>",
                                                      datetime.fromtimestamp(e["ts"]).strftime("%a %d %b"),
                                                      _esc(e["body"])))
    upcoming = [_row(f"<b>{_esc(e['ticker'])}</b> · {_esc(e['event'])}",
                     datetime.fromisoformat(e["date"]).strftime("%a %d %b"), _esc(e["detail"])) for e in d["upcoming"]]
    mix = " · ".join(f"{k} {v}" for k, v in d["verdicts"].items() if v)
    body = (head + _section("Best this week", best) + _section("Worst this week", worst)
            + _section("Verdict changes", by_kind.get("verdict", [])) + _section("New filings", by_kind.get("filing", []))
            + _section("Guidance missed", by_kind.get("guidance", [])) + _section("Price alerts", by_kind.get("price", []))
            + _section("Coming up in the next two weeks", upcoming)
            + _section("AI verdicts across your holdings", [_row(_esc(mix))]))
    return email_page(f"Your weekly digest · {d['week']}", intro, body)


def make_digest(settings: dict, send: bool = True) -> dict:
    d = build_digest(settings)
    pf._q("INSERT INTO digests (week, ts, data) VALUES (?,?,?) ON CONFLICT(week) DO UPDATE SET "
          "ts=excluded.ts, data=excluded.data", (d["week"], d["made"], json.dumps(d, default=str)))
    if send:
        n = prefs(settings)
        if "mac" in n["channels"]:
            notify("Your weekly digest is ready", "Open the Portfolio page → Digest to read it.",
                   {**settings, "notify": {**n, "channels": ["mac"]}})
        if "email" in n["channels"]:
            notify(f"Your weekly digest · {d['week']}", "Your weekly digest is ready.",
                   {**settings, "notify": {**n, "channels": ["email"]}}, digest_html(d))
    return d


def digests(limit: int = 12) -> list[dict]:
    return [{**r, "data": json.loads(r["data"])} for r in
            pf._q("SELECT * FROM digests ORDER BY ts DESC LIMIT ?", (limit,))]


def maybe_digest(settings: dict) -> None:
    n, now = prefs(settings), datetime.now(pf.IST)
    if not n["digest"] or now.weekday() != int(n["digest_day"]) or now.hour < 8 or not pf.tracked():
        return
    if not pf._q("SELECT 1 FROM digests WHERE week=?", (_week_key(now),)):
        make_digest(settings)


def tick(settings: dict) -> None:
    """Once per refresher cycle."""
    check_prices(settings)
    maybe_digest(settings)
    flush(settings)
