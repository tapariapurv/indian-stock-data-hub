"""Portfolios: named accounts, live value and P&L, and one click to any stock's page."""

from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st

import alerts
import portfolio as pf
import ui
from ui import VERDICT_BADGE, VIEW_WORDS

settings = st.session_state.settings
positions = pf.prefs(settings)["track_positions"]
GOOD, BAD = "#1E8E5A", "#C8453B"


inr = ui.inr


st.title("Portfolio")
st.caption(":material/lock: What you own stays on this computer and is never sent anywhere."
           + (" It's kept in `~/.stock-data-hub`, outside the app folder, so updates never touch it. Prices are "
              "looked up by symbol alone. The AI only ever sees each company's *public* data: with a local AI "
              "nothing leaves this computer; with a paid AI service, that service receives it."
              if ui.tech(settings) else ""))

accounts = pf.accounts()
names = {a["id"]: a["name"] for a in accounts}

with st.container(horizontal=True, vertical_alignment="bottom"):
    view = st.segmented_control("Account", ["All accounts", *names], format_func=lambda k: names.get(k, k),
                                default="All accounts" if names else None, required=bool(names),
                                key="pf_view", label_visibility="collapsed")
    with st.popover("New account", icon=":material/add:"):
        new_name = st.text_input("Name", placeholder="e.g. Long-term, Trading, Mom's account",
                                 help="Accounts let you keep separate groups of stocks — for example, one per broker.")
        if st.button("Create", type="primary", disabled=not new_name.strip()):
            pf.add_account(new_name)
            st.rerun()
    if view in names:
        with st.popover("Manage", icon=":material/edit:"):
            renamed = st.text_input("Rename", value=names[view])
            if st.button("Save name") and renamed.strip():
                pf.rename_account(view, renamed)
                st.rerun()
            if st.button("Delete this account", type="tertiary", icon=":material/delete:"):
                pf.delete_account(view)
                st.session_state.pop("pf_view", None)
                st.rerun()
view = view or "All accounts"  # the control has no value on the run that creates the first account

if not names:
    st.info("Start by creating an account with **New account** above — any name will do, like "
            "*My stocks*. Then add the stocks you own.", icon=":material/info:")
    st.stop()

# --- Add a stock -------------------------------------------------------------
with st.expander("Add a stock", icon=":material/add_circle:", expanded=False):
    query = st.text_input("Find the company", placeholder="Type its name, e.g. HDFC Bank")
    hits = pf.safe_search(query.strip()) if len(query.strip()) >= 2 else []
    if hits is None:
        st.caption("The search service is busy — try again in a few seconds.")
    elif query and not hits:
        st.caption("No matches.")
    if hits:
        c1, c2, c3, c4 = st.columns([3, 2, 1, 1] if positions else [3, 2, 0.01, 0.01], vertical_alignment="bottom")
        pick = c1.selectbox("Company", hits, format_func=lambda h: f"{h['name']}  ·  {h['ticker']}")
        into = c2.selectbox("Account", list(names), format_func=names.get,
                            index=list(names).index(view) if view in names else 0)
        qty = avg = None
        if positions:
            qty = c3.number_input("Shares you own", min_value=0.0, step=1.0, value=None, placeholder="Optional")
            avg = c4.number_input("Average price paid (₹)", min_value=0.0, step=1.0, value=None, placeholder="Optional")
        if st.button("Add to portfolio", type="primary", icon=":material/add:"):
            pf.save_holding(into, pick["ticker"], pick["name"], pick["screener_id"], qty, avg)
            st.toast(f"Added {pick['ticker']}. The app is looking into it now.",
                     icon=":material/check_circle:")
            st.rerun()

with st.expander("Add many at once from a spreadsheet", icon=":material/upload_file:"):
    st.caption("Download the example, fill in one stock per row (only the **ticker** — the stock's short "
               "symbol — is needed), then upload it here. It stays on this computer.")
    st.download_button("Download the example", pf.template(), file_name="portfolio_template.csv", mime="text/csv",
                       icon=":material/download:", type="tertiary")
    up = st.file_uploader("Portfolio file", type=["csv", "xlsx", "xls"], label_visibility="collapsed")
    target = st.selectbox("Put them in", list(names), format_func=names.get,
                          index=list(names).index(view) if view in names else 0, key="import_into")
    if st.button("Import", type="primary", icon=":material/upload:", disabled=up is None):
        with st.spinner("Checking each stock…"):
            saved, problems = pf.import_file(up, target)
        st.session_state.import_report = (saved, problems)
        st.rerun()
    if "import_report" in st.session_state:
        saved, problems = st.session_state.pop("import_report")
        st.success(f"Added {saved} stock(s). The app is looking into the new ones now.",
                   icon=":material/check_circle:") if saved else None
        for p in problems:
            st.warning(p, icon=":material/warning:")

rows = pf.holdings() if view == "All accounts" else [{**h, "account": names[view]} for h in pf.holdings(view)]
if not rows:
    st.caption("No stocks in this account yet — add one above.")
    st.stop()

# --- Numbers -----------------------------------------------------------------
stocks = tuple(sorted({(h["ticker"], h["screener_id"]) for h in rows}))
prices = pf.quotes(stocks)
runs = pf.latest_runs(t for t, _ in stocks)
df = pd.DataFrame(rows)
df[["qty", "avg_price"]] = df[["qty", "avg_price"]].astype(float).replace(0, float("nan"))  # blank = not given
df["Price"] = df["ticker"].map(lambda t: prices.get(t, {}).get("price"))
df["Day %"] = df["ticker"].map(lambda t: prices.get(t, {}).get("change_pct"))
df["Value"] = df["qty"] * df["Price"]
df["Invested"] = df["qty"] * df["avg_price"]
df["P&L"] = df["Value"] - df["Invested"]
df["P&L"] = df["P&L"].where(df["Invested"] > 0)  # no buy price, no P&L
df["P&L %"] = (df["P&L"] / df["Invested"].where(df["Invested"] > 0)) * 100
df["Day P&L"] = df["qty"] * df["ticker"].map(lambda t: prices.get(t, {}).get("change", 0))
df["Verdict"] = df["ticker"].map(lambda t: (runs.get(t) or {}).get("verdict") or "—")
df["Analysed"] = df["ticker"].map(lambda t: datetime.fromtimestamp(runs[t]["ts"]) if t in runs else None)

value, invested, day = df["Value"].sum(), df["Invested"].sum(), df["Day P&L"].sum()
costed = df.loc[df["Invested"] > 0, "Value"].sum()  # P&L only over holdings with a buy price
uniq = df.drop_duplicates("ticker")
moves = uniq.dropna(subset=["Day %"])


def ago(ts) -> str:
    if ts is None or pd.isna(ts):
        return "Not checked yet"
    mins = (datetime.now() - ts).total_seconds() / 60
    return f"Checked {int(mins)} min ago" if mins < 60 else (
        f"Checked {int(mins // 60)} h ago" if mins < 1440 else f"Checked {int(mins // 1440)} days ago")


def signed(x: float, fmt: str) -> str:
    return f":{'green' if x >= 0 else 'red'}[{fmt.format(x)}]"


busy = bool(pf.STATUS["running"] or pf._forced or pf.due(float(pf.prefs(settings)["refresh_hours"])))


@st.fragment(run_every=3 if busy else None)
def live_status():
    """Polls only while work is queued; when a stock finishes, the whole page
    reruns once so its card shows the new verdict."""
    s = pf.STATUS
    if s["running"]:
        total = max(s["total"], 1)
        st.progress(s["done"] / total, text=f":material/sync: Checking **{s['running']}** "
                                            f"({s['done'] + 1} of {total})")
    elif busy:
        st.caption(":material/hourglass_top: About to check your stocks…")
    if s["error"] and ui.tech(settings):
        st.caption(f":material/error: Last background error: {s['error']}")
    seen = st.session_state.get("pf_last_seen", "unset")
    st.session_state.pf_last_seen = s["last"]
    if seen != "unset" and seen != s["last"]:
        st.rerun()


# --- Overview ----------------------------------------------------------------
with st.container(border=True):
    st.markdown("#### :material/insights: At a glance")
    m = st.columns(4)
    if positions and value:
        m[0].metric("Worth today", inr(value), f"{inr(day, signed=True)} today", border=True)
        if invested:
            m[1].metric("You paid", inr(invested), border=True)
            m[2].metric("Gain or loss", inr(costed - invested, signed=True),
                        f"{(costed / invested - 1) * 100:+.2f}%", border=True)
        else:  # no buy prices given: nothing to compare against, so show the day instead
            m[1].metric("Up / down today", f"{(moves['Day %'] > 0).sum()} / {(moves['Day %'] < 0).sum()}",
                        border=True, help="Add what you paid for each stock to see your gain or loss.")
            m[2].metric("Average move today", f"{moves['Day %'].mean():+.2f}%" if len(moves) else "—", border=True)
    else:
        m[0].metric("Stocks", len(uniq), border=True)
        m[1].metric("Up / down today", f"{(moves['Day %'] > 0).sum()} / {(moves['Day %'] < 0).sum()}",
                    border=True)
        m[2].metric("Average move today", f"{moves['Day %'].mean():+.2f}%" if len(moves) else "—", border=True)
    m[3].metric("Checked", f"{uniq['Analysed'].notna().sum()} / {len(uniq)}", border=True,
                help="Stocks the app has looked into at least once.")

    o1, o2, o3 = st.columns([1, 1, 1.2] if positions else [1, 1, 1])
    with o1:
        st.markdown("**:material/trending_up: Biggest moves today**")
        for _, r in moves.sort_values("Day %", ascending=False).head(3).iterrows():
            st.markdown(f"{r['ticker']} &nbsp; {signed(r['Day %'], '{:+.2f}%')}")
        for _, r in moves.sort_values("Day %").head(min(3, max(len(moves) - 3, 0))).iterrows():  # never repeat a top mover
            st.markdown(f"{r['ticker']} &nbsp; {signed(r['Day %'], '{:+.2f}%')}")
    with o2:
        st.markdown("**:material/psychology: What the AI thinks**")
        counts = uniq["Verdict"].value_counts()
        with st.container(horizontal=True):
            for verdict, n in counts.items():
                color, icon = VERDICT_BADGE.get(verdict, ("gray", ":material/hourglass_top:"))
                st.badge(f"{VIEW_WORDS.get(verdict, 'Not checked yet')} · {n}", icon=icon, color=color)
        live_status()
    with o3:
        alloc = df.groupby("ticker", as_index=False)["Value"].sum().dropna()
        if positions and alloc["Value"].sum() > 0:
            fig = px.pie(alloc, names="ticker", values="Value", hole=0.62)
            fig.update_layout(margin=dict(t=0, b=0, l=0, r=0), height=190, showlegend=False)
            fig.update_traces(textinfo="label+percent", textposition="inside")
            st.plotly_chart(fig, config={"displayModeBar": False})

# --- Sections: only the one on screen is built -------------------------------------
n_unread = alerts.unread()
SECTIONS = {"Holdings": ":material/view_module:", "Performance": ":material/show_chart:",
            "Allocation": ":material/donut_large:", "Alerts": ":material/notifications:",
            "Digest": ":material/newspaper:"}
SECTION_WORDS = {"Holdings": "My stocks", "Performance": "How it's doing", "Trades": "Buys & sells",
                 "Allocation": "Spread", "Alerts": "Alerts", "Digest": "Weekly summary"}
# Only for people who track quantities. Someone using this as a watchlist
# never sees a tab about capital gains.
if positions:
    SECTIONS = {**{k: SECTIONS[k] for k in ("Holdings", "Performance")},
                "Trades": ":material/receipt_long:",
                **{k: SECTIONS[k] for k in ("Allocation", "Alerts", "Digest")}}
section = st.segmented_control(
    "Section", list(SECTIONS), default="Holdings", required=True, key="pf_section",
    label_visibility="collapsed",
    format_func=lambda s: f"{SECTIONS[s]} {SECTION_WORDS[s]}" + (f" · {n_unread}" if s == "Alerts" and n_unread else ""))

if section == "Performance":
    qty = df.groupby("ticker")["qty"].sum(min_count=1).fillna(0) if positions else pd.Series(0, index=uniq["ticker"])
    perf = pf.performance(tuple(sorted((t, float(q)) for t, q in qty.items())), pf.day_slot())
    if perf is None or perf.empty:
        st.caption("Past prices aren't available right now — try again shortly.")
        st.stop()
    cols = st.columns(5)
    for col, (label, days) in zip(cols, (("1 week", 5), ("1 month", 21), ("3 months", 63), ("6 months", 126),
                                         ("1 year", len(perf) - 1))):
        if len(perf) > days:
            p = (perf["Your holdings"].iloc[-1] / perf["Your holdings"].iloc[-1 - days] - 1) * 100
            n = (perf["Nifty 50"].iloc[-1] / perf["Nifty 50"].iloc[-1 - days] - 1) * 100
            col.metric(label, f"{p:+.1f}%", f"{p - n:+.1f} points vs the market", border=True)
    st.line_chart(perf, color=settings["ui"]["chart_colors"], height=360, y_label="₹100 invested a year ago")
    st.caption("What ₹100 put into the stocks you hold **today** a year ago would be worth now"
               + (", in the amounts you hold them" if positions and qty.sum() else ", split equally")
               + ", compared with the whole market (the Nifty 50).")
    st.stop()

if section == "Trades":
    st.caption("Write down each time you buy or sell, and the app works out how many shares you have, "
               "what you paid on average, and the profit on anything you've sold.")
    log = pf.trades(None if view == "All accounts" else view)

    with st.expander("Add a buy or sell", icon=":material/add_circle:", expanded=not log):
        held_now = sorted({h["ticker"] for h in rows})
        t1, t2, t3 = st.columns([2, 1, 1], vertical_alignment="bottom")
        into = t1.selectbox("Account", list(names), format_func=names.get,
                            index=list(names).index(view) if view in names else 0, key="tr_acct")
        which = t2.selectbox("Stock", held_now, key="tr_ticker")
        side = t3.selectbox("Bought or sold?", ["buy", "sell"], key="tr_side",
                            format_func={"buy": "Bought", "sell": "Sold"}.get)
        t4, t5, t6, t7 = st.columns(4, vertical_alignment="bottom")
        qty = t4.number_input("Shares", min_value=0.0, step=1.0, value=None, key="tr_qty")
        price = t5.number_input("Price per share (₹)", min_value=0.0, step=1.0, value=None, key="tr_price")
        when = t6.date_input("Date", value=datetime.now(pf.IST).date(),
                             max_value=datetime.now(pf.IST).date(), key="tr_date")
        fees = t7.number_input("Fees (₹)", min_value=0.0, step=1.0, value=0.0, key="tr_fees",
                               help="What your broker charged, including taxes on the trade.")
        if st.button("Add", type="primary", icon=":material/add:",
                     disabled=not (which and qty and price)):
            pf.add_trade(into, which, side, qty, price, when.isoformat(), fees)
            st.toast(f"Saved: {'bought' if side == 'buy' else 'sold'} {qty:,.0f} {which}.", icon=":material/check_circle:")
            st.rerun()

    if not log:
        st.info("Nothing written down yet. Until you add some, the share counts and prices you typed "
                "in are used as they are.", icon=":material/info:")
        st.stop()

    gains, unmatched = pf.fifo_gains(log)
    if unmatched:
        st.warning(f"{unmatched:,.0f} share(s) were sold, but there's no record of buying them, so their "
                   "profit can't be worked out. Add the missing buy and it will fix itself.",
                   icon=":material/warning:")

    # Money-weighted return across everything on screen.
    value_now = float(df["Value"].sum(skipna=True) or 0)
    rate = pf.xirr(pf.cashflows(log, value_now))
    realised = sum(g["gain"] for g in gains)
    x1, x2, x3 = st.columns(3)
    x1.metric("Yearly return", f"{rate * 100:+.1f}% a year" if rate is not None else "—",
              border=True, help="What your money has earned per year, counting when you put it in and "
                                "took it out.")
    x2.metric("Profit on sold shares", inr(realised) if gains else "—", border=True,
              help="Profit on shares you've actually sold. The oldest shares count as sold first.")
    x3.metric("Buys and sells", len(log), border=True)

    if gains:
        st.markdown("**Profit on sold shares, by financial year**")
        gdf = pd.DataFrame(gains)
        gdf["FY"] = gdf["sold"].map(lambda d: pf.financial_year(datetime.fromisoformat(d).date()))
        years = sorted(gdf["FY"].unique(), reverse=True)
        pick_fy = st.segmented_control("Financial year", years, default=years[0], required=True,
                                       key="tr_fy", label_visibility="collapsed")
        year_rows = gdf[gdf["FY"] == pick_fy]
        short = year_rows[year_rows["term"] == "Short"]["gain"].sum()
        long_ = year_rows[year_rows["term"] == "Long"]["gain"].sum()
        f1, f2 = st.columns(2)
        f1.metric(f"Short term, {pick_fy}", inr(short), border=True,
                  help="Shares held 12 months or less.")
        f2.metric(f"Long term, {pick_fy}", inr(long_), border=True,
                  help="Shares held more than 12 months.")
        show = year_rows[["ticker", "bought", "sold", "qty", "buy_price", "sell_price", "days",
                          "term", "gain"]].rename(columns={
                              "ticker": "Stock", "bought": "Bought", "sold": "Sold", "qty": "Qty",
                              "buy_price": "Buy ₹", "sell_price": "Sell ₹", "days": "Days held",
                              "term": "Term", "gain": "Gain ₹"})
        st.dataframe(show.style.map(lambda v: "" if pd.isna(v) else
                                    f"color: {GOOD if v >= 0 else BAD}; font-weight: 600",
                                    subset=["Gain ₹"])
                     .format({"Qty": "{:,.0f}", "Buy ₹": "₹{:,.2f}", "Sell ₹": "₹{:,.2f}",
                              "Gain ₹": lambda v: inr(v, signed=True), "Days held": "{:,.0f}"}),
                     hide_index=True)
        st.download_button("Download this table", show.to_csv(index=False).encode(),
                           file_name=f"realised_gains_{pick_fy}.csv", mime="text/csv",
                           icon=":material/download:", type="tertiary")
        st.caption("Worked out the way Indian tax rules count it (oldest shares sold first, fees included). "
                   "Held over 12 months counts as long term. This is a record, not tax advice.")

    with st.expander("Everything you've written down", icon=":material/list:"):
        for t in sorted(log, key=lambda r: str(r["date"]), reverse=True):
            c1, c2 = st.columns([8, 1], vertical_alignment="center")
            c1.markdown(f"**{t['ticker']}** · {t['side']} {t['qty']:,.0f} @ ₹{t['price']:,.2f} "
                        f":gray[· {t['date']}" + (f" · charges ₹{t['fees']:,.0f}" if t["fees"] else "") + "]")
            if c2.button("", icon=":material/delete:", key=f"tr_del_{t['id']}", type="tertiary"):
                pf.delete_trade(t["id"])
                st.rerun()
    st.stop()

if section == "Allocation":
    prof = pf.profiles(uniq["ticker"])
    alloc = uniq[["ticker", "name", "Day %"]].copy()
    weights = df.groupby("ticker")["Value"].sum(min_count=1) if positions and value else None
    alloc["Weight"] = alloc["ticker"].map(weights).fillna(0) if weights is not None else 1.0
    alloc["Weight"] = alloc["Weight"] / alloc["Weight"].sum() * 100
    alloc["Sector"] = alloc["ticker"].map(lambda t: (prof.get(t) or {}).get("sector", "Unknown"))
    alloc["Size"] = alloc["ticker"].map(lambda t: pf.cap_bucket((prof.get(t) or {}).get("market_cap")))
    limits = pf.prefs(settings)
    heavy = alloc[alloc["Weight"] > limits["max_stock_pct"]]
    sectors = alloc.groupby("Sector")["Weight"].sum().sort_values(ascending=False)
    crowded = sectors[sectors > limits["max_sector_pct"]]
    for _, r in heavy.iterrows():
        st.warning(f"**{r['ticker']}** is {r['Weight']:.1f}% of everything you own — more than the "
                   f"{limits['max_stock_pct']}% you set as the most for one stock.", icon=":material/warning:")
    for name, w in crowded.items():
        st.warning(f"**{name}** companies are {w:.1f}% of everything you own — more than the "
                   f"{limits['max_sector_pct']}% you set as the most for one industry.",
                   icon=":material/warning:")
    if heavy.empty and crowded.empty:
        st.success(f"Nicely spread: no stock over {limits['max_stock_pct']}% and no industry over "
                   f"{limits['max_sector_pct']}% (you can change these in Settings).", icon=":material/verified:")
    left, right = st.columns([2, 1])
    with left:
        fig = px.treemap(alloc, path=["Sector", "ticker"], values="Weight", color="Day %",
                         color_continuous_scale=["#C8453B", "#F1EFEA", "#1E8E5A"], color_continuous_midpoint=0)
        fig.update_traces(texttemplate="%{label}<br>%{value:.1f}%", hovertemplate="%{label}: %{value:.1f}%")
        fig.update_layout(margin=dict(t=0, b=0, l=0, r=0), height=420, coloraxis_showscale=False)
        st.plotly_chart(fig, config={"displayModeBar": False})
    with right:
        sizes = alloc.groupby("Size")["Weight"].sum().reindex(["Large cap", "Mid cap", "Small cap", "Unknown"]).dropna()
        st.markdown("**By company size**")
        st.bar_chart(sizes, horizontal=True, color=settings["ui"]["chart_colors"][0], height=170)
        st.markdown("**By industry**")
        st.dataframe(sectors.rename("Share %").round(1), height=210)
    st.caption(("Sized by what each holding is worth" if weights is not None else "Every stock counted equally")
               + ". Green boxes are up today, red are down. Stocks not checked yet show as Unknown.")
    st.stop()

if section == "Alerts":
    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown(f"**Messages** :gray[· {n_unread} new]", width="content")
        st.space("stretch")
        if n_unread and st.button("Mark all read", icon=":material/done_all:", type="tertiary"):
            alerts.mark_all_read()
            st.rerun()
    inbox = alerts.events(60)
    if not inbox:
        st.caption("Nothing yet. You'll be told here when the AI's view changes, a company publishes something "
                   "important, it breaks a promise, or a price you're watching is reached.")
    icons = {"verdict": ":material/psychology:", "filing": ":material/description:",
             "guidance": ":material/handshake:", "price": ":material/price_change:"}
    with st.container(border=bool(inbox)):
        for e in inbox:
            dot = ":blue[●] " if not e["read"] else ""
            st.markdown(f"{dot}{icons.get(e['kind'], '')} **{ui.plain_title(e['title'])}** :gray[· "
                        f"{datetime.fromtimestamp(e['ts']).strftime('%d %b, %H:%M')}]  \n:gray[{e['body']}]")
    st.markdown("**Price alerts**")
    rules = alerts.rules()
    for r in rules:
        c1, c2 = st.columns([6, 1], vertical_alignment="center")
        c1.markdown(f"**{r['ticker']}** {r['op']} ₹{r['level']:,.2f}"
                    + (" :orange-badge[reached]" if r["fired"] else " :gray-badge[watching]"))
        if c2.button("", icon=":material/delete:", key=f"rule_{r['id']}", type="tertiary"):
            alerts.delete_rule(r["id"])
            st.rerun()
    with st.form("new_rule", border=False):
        a1, a2, a3, a4 = st.columns([2, 1, 1, 1], vertical_alignment="bottom")
        t = a1.selectbox("Stock", sorted(uniq["ticker"]))
        op = a2.selectbox("Tell me when it goes", ["above", "below"])
        level = a3.number_input("₹", min_value=0.0, value=float(prices.get(t, {}).get("price") or 0), step=1.0)
        if a4.form_submit_button("Add alert", icon=":material/add_alert:"):
            alerts.add_rule(t, op, level)
            st.rerun()
    st.stop()

if section == "Digest":
    past = alerts.digests()
    n = alerts.prefs(settings)
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(f"A summary of your week is made every {alerts.DAYS[int(n['digest_day'])]} morning"
                   + (" and sent by " + " and ".join({'mac': 'notification', 'email': 'email'}[c] for c in n["channels"])
                      if n["channels"] else "") + ". Change this in Settings → Alerts.")
        st.space("stretch")
        if st.button("Make one now", icon=":material/refresh:"):
            with st.spinner("Putting this week together…"):
                alerts.make_digest(settings, send=False)
            st.rerun()
    if not past:
        st.caption("No summary yet — make one now, or wait for the weekly one.")
        st.stop()
    pick = st.selectbox("Week", past, format_func=lambda d: f"{d['week']} · made "
                        f"{datetime.fromtimestamp(d['ts']).strftime('%a %d %b, %H:%M')}", label_visibility="collapsed")
    st.html(alerts.digest_html(pick["data"]))
    if "email" in n["channels"] and st.button("Email this to me", icon=":material/mail:"):
        problems = alerts.notify(f"Your weekly digest · {pick['week']}", "Your weekly digest is ready.",
                                 {**settings, "notify": {**n, "channels": ["email"]}}, alerts.digest_html(pick["data"]))
        st.toast(problems[0] if problems else "Sent.", icon=":material/error:" if problems else ":material/mail:")
    st.stop()

# --- Holdings grid -------------------------------------------------------------
with st.container(horizontal=True, vertical_alignment="center"):
    st.markdown("#### :material/view_module: My stocks", width="content")
    layout = st.segmented_control("View", [":material/grid_view: Cards", ":material/table_rows: Table"],
                                  default=":material/grid_view: Cards", required=True, key="pf_layout",
                                  label_visibility="collapsed")
    sorts = (["Value", "P&L %"] if positions else []) + ["Day %", "Name"]
    SORT_WORDS = {"Value": "Biggest", "P&L %": "Best gain", "Day %": "Today's move", "Name": "A–Z"}
    order = st.segmented_control("Sort by", sorts, default=sorts[0], required=True, key="pf_sort",
                                 label_visibility="collapsed", format_func=SORT_WORDS.get)
    st.space("stretch")
    if st.button("Check them all again", icon=":material/autorenew:", disabled=bool(pf._forced),
                 help="Looks into every stock again, one at a time, while you carry on."):
        st.toast(f"Checking {pf.reanalyse_all()} stock(s) again.", icon=":material/autorenew:")
key_col = {"Value": "Value", "P&L %": "P&L %", "Day %": "Day %", "Name": "ticker"}[order]
cards = df.sort_values(key_col, ascending=order == "Name", na_position="last")

if layout.endswith("Table"):
    cols = ["ticker", "name", "account", "qty", "avg_price", "Price", "Day %", "Value", "P&L", "P&L %",
            "Verdict", "Analysed"]
    if not positions:
        cols = [c for c in cols if c not in ("qty", "avg_price", "Value", "P&L", "P&L %")]
    table = cards[cols].rename(columns={"ticker": "Ticker", "name": "Company", "account": "Account",
                                        "qty": "Shares", "avg_price": "Avg price", "Day %": "Today",
                                        "P&L": "Gain/loss", "P&L %": "Gain/loss %", "Verdict": "AI view",
                                        "Analysed": "Checked"})
    table["AI view"] = table["AI view"].map(lambda v: VIEW_WORDS.get(v, v))
    tone = lambda v: "" if pd.isna(v) else f"color: {GOOD if v >= 0 else BAD}; font-weight: 600"
    verdict_tone = lambda v: {"green": f"color: {GOOD}", "red": f"color: {BAD}",
                              "orange": "color: #C08A3E"}.get(VERDICT_BADGE.get(v, ("",))[0], "")
    words = {w: v for v, w in VIEW_WORDS.items()}
    styled = (table.style.map(tone, subset=[c for c in ("Today", "Gain/loss", "Gain/loss %") if c in table])
              .map(lambda w: verdict_tone(words.get(w)), subset=["AI view"])
              .format({"Shares": "{:,.0f}", "Avg price": "₹{:,.2f}", "Price": "₹{:,.2f}", "Today": "{:+.2f}%",
                       "Value": inr, "Gain/loss": lambda v: inr(v, signed=True), "Gain/loss %": "{:+.2f}%"}, na_rep="—"))
    picked = st.dataframe(styled, hide_index=True, on_select="rerun", selection_mode="single-row",
                          column_config={"Checked": st.column_config.DatetimeColumn(format="distance")},
                          key="pf_table")
    st.caption("Tick the box beside a row to open that company's page.")
    if picked.selection.rows:
        row = cards.iloc[picked.selection.rows[0]]
        ui.open_company({"ticker": row["ticker"], "name": row["name"], "screener_id": row["screener_id"]})

PER_ROW = 3
for start in range(0, len(cards) if layout.endswith("Cards") else 0, PER_ROW):
    for col, (_, h) in zip(st.columns(PER_ROW), cards.iloc[start:start + PER_ROW].iterrows()):
        q = prices.get(h["ticker"], {})
        with col.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"### {h['ticker']}", width="content")
                if h["Verdict"] != "—":
                    color, icon = VERDICT_BADGE.get(h["Verdict"], ("gray", None))
                    st.badge(VIEW_WORDS.get(h["Verdict"], h["Verdict"]), icon=icon, color=color)
            st.caption(h["name"] + (f" · {h['account']}" if view == "All accounts" else ""))
            st.metric("Price", f"₹{q['price']:,.2f}" if q else "—",
                      f"{q['change']:+,.2f} ({q['change_pct']:+.2f}%)" if q else None,
                      chart_data=q.get("spark"), chart_type="area", label_visibility="collapsed")
            if positions and pd.notna(h["qty"]):
                a, b, c = st.columns(3)
                a.markdown(f":gray[Shares]  \n**{h['qty']:,.0f}**")
                b.markdown(f":gray[Value]  \n**{inr(h['Value'])}**" if pd.notna(h["Value"]) else ":gray[Value]  \n—")
                c.markdown(f":gray[Gain/loss]  \n**:{'green' if h['P&L'] >= 0 else 'red'}[{inr(h['P&L'], signed=True)}]**  \n"
                           f"{signed(h['P&L %'], '{:+.1f}%')}" if pd.notna(h["P&L"]) else ":gray[Gain/loss]  \n—")
            with st.container(horizontal=True, vertical_alignment="center"):
                st.caption(ago(h["Analysed"]))
                if st.button("Open", key=f"open_{h['account_id']}_{h['ticker']}", icon=":material/arrow_forward:",
                             type="tertiary"):
                    ui.open_company({"ticker": h["ticker"], "name": h["name"], "screener_id": h["screener_id"]})

if view in names:
    with st.expander("Change or remove a stock" if positions else "Remove a stock", icon=":material/edit_note:"):
        h = st.selectbox("Stock", rows, format_func=lambda r: f"{r['ticker']} · {r['name']}")
        q2, a2 = h["qty"], h["avg_price"]
        if positions:
            e1, e2 = st.columns(2)
            q2 = e1.number_input("Shares you own", min_value=0.0, value=q2, placeholder="Optional", key=f"q_{h['ticker']}")
            a2 = e2.number_input("Average price paid (₹)", min_value=0.0, value=a2, placeholder="Optional",
                                 key=f"a_{h['ticker']}")
        b1, b2 = st.columns(2)
        if positions and b1.button("Save", type="primary", width="stretch"):
            pf.save_holding(view, h["ticker"], h["name"], h["screener_id"], q2, a2)
            st.rerun()
        if b2.button("Remove", icon=":material/delete:", width="stretch"):
            pf.remove_holding(view, h["ticker"])
            st.rerun()
