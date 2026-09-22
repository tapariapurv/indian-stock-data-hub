"""Portfolios: named accounts, live value and P&L, and one click to any stock's page."""

from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st

import alerts
import portfolio as pf
from ui import VERDICT_BADGE

settings = st.session_state.settings
positions = pf.prefs(settings)["track_positions"]
GOOD, BAD = "#1E8E5A", "#C8453B"


def inr(x: float) -> str:
    return f"₹{x:,.0f}"


st.title(":material/account_balance_wallet: Portfolio")
st.caption(":material/lock: Your accounts" + (", quantities and buy prices" if positions else " and stocks")
           + " are stored only on this computer (in `~/.stock-data-hub`, outside the app folder, so updates never touch them) and are never sent anywhere. "
             "Prices are looked up by ticker symbol alone. The analysis sends each company's *public* data "
             "to the model chosen in **Settings**: with a local model nothing leaves this computer; with a "
             "cloud API key, that provider receives it.")

accounts = pf.accounts()
names = {a["id"]: a["name"] for a in accounts}

with st.container(horizontal=True, vertical_alignment="bottom"):
    view = st.segmented_control("Account", ["All accounts", *names], format_func=lambda k: names.get(k, k),
                                default="All accounts" if names else None, required=bool(names),
                                key="pf_view", label_visibility="collapsed")
    with st.popover("New account", icon=":material/add:"):
        new_name = st.text_input("Name", placeholder="e.g. Long-term, Trading, Mom's account")
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
    st.info("Create an account to start — give it any name, then search for stocks to add.",
            icon=":material/info:")
    st.stop()

# --- Add a stock -------------------------------------------------------------
with st.expander("Add a stock", icon=":material/add_circle:", expanded=False):
    query = st.text_input("Search any NSE or BSE listed company", placeholder="Name or symbol, e.g. HDFC Bank")
    hits = pf.safe_search(query.strip()) if len(query.strip()) >= 2 else []
    if hits is None:
        st.caption("screener.in is busy — try again in a few seconds.")
    elif query and not hits:
        st.caption("No matches.")
    if hits:
        c1, c2, c3, c4 = st.columns([3, 2, 1, 1] if positions else [3, 2, 0.01, 0.01], vertical_alignment="bottom")
        pick = c1.selectbox("Company", hits, format_func=lambda h: f"{h['name']}  ·  {h['ticker']}")
        into = c2.selectbox("Account", list(names), format_func=names.get,
                            index=list(names).index(view) if view in names else 0)
        qty = avg = None
        if positions:
            qty = c3.number_input("Quantity", min_value=0.0, step=1.0, value=None, placeholder="Optional")
            avg = c4.number_input("Avg. buy price", min_value=0.0, step=1.0, value=None, placeholder="Optional")
        if st.button("Add to portfolio", type="primary", icon=":material/add:"):
            pf.save_holding(into, pick["ticker"], pick["name"], pick["screener_id"], qty, avg)
            st.toast(f"Added {pick['ticker']}. Its first full analysis starts in the background.",
                     icon=":material/check_circle:")
            st.rerun()

with st.expander("Import from a CSV or Excel file", icon=":material/upload_file:"):
    st.caption("Only the **ticker** column is required" + (". **quantity** and **avg_price** are optional"
               if positions else "") + "; an **account** column puts each row in that account, creating it if "
               "needed. Rows without one go into the account picked below. Importing a stock that is already "
               "in an account updates it. The file is read on this computer and not uploaded anywhere.")
    st.download_button("Download template", pf.template(), file_name="portfolio_template.csv", mime="text/csv",
                       icon=":material/download:", type="tertiary")
    up = st.file_uploader("Portfolio file", type=["csv", "xlsx", "xls"], label_visibility="collapsed")
    target = st.selectbox("Rows without an account go into", list(names), format_func=names.get,
                          index=list(names).index(view) if view in names else 0, key="import_into")
    if st.button("Import", type="primary", icon=":material/upload:", disabled=up is None):
        with st.spinner("Checking each symbol…"):
            saved, problems = pf.import_file(up, target)
        st.session_state.import_report = (saved, problems)
        st.rerun()
    if "import_report" in st.session_state:
        saved, problems = st.session_state.pop("import_report")
        st.success(f"Imported {saved} holding(s). New stocks are analysed in the background.",
                   icon=":material/check_circle:") if saved else None
        for p in problems:
            st.warning(p, icon=":material/warning:")

rows = pf.holdings() if view == "All accounts" else [{**h, "account": names[view]} for h in pf.holdings(view)]
if not rows:
    st.caption("No stocks in this account yet.")
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
        return "Awaiting first analysis"
    mins = (datetime.now() - ts).total_seconds() / 60
    return f"Analysed {int(mins)} min ago" if mins < 60 else (
        f"Analysed {int(mins // 60)} h ago" if mins < 1440 else f"Analysed {int(mins // 1440)} d ago")


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
        st.progress(s["done"] / total, text=f":material/sync: Analysing **{s['running']}** "
                                            f"({s['done'] + 1} of {total})")
    elif busy:
        st.caption(":material/hourglass_top: Queued — starting shortly…")
    if s["error"]:
        st.caption(f":material/error: Last refresher error: {s['error']}")
    seen = st.session_state.get("pf_last_seen", "unset")
    st.session_state.pf_last_seen = s["last"]
    if seen != "unset" and seen != s["last"]:
        st.rerun()


# --- Overview ----------------------------------------------------------------
with st.container(border=True):
    st.markdown("#### :material/insights: Overview")
    m = st.columns(4)
    if positions and value:
        m[0].metric("Current value", inr(value), f"{day:+,.0f} today", border=True)
        m[1].metric("Invested", inr(invested), border=True)
        m[2].metric("Total P&L", inr(costed - invested) if invested else "—",
                    f"{(costed / invested - 1) * 100:+.2f}%" if invested else None, border=True)
    else:
        m[0].metric("Stocks tracked", len(uniq), border=True)
        m[1].metric("Advancing / declining", f"{(moves['Day %'] > 0).sum()} / {(moves['Day %'] < 0).sum()}",
                    border=True)
        m[2].metric("Average move today", f"{moves['Day %'].mean():+.2f}%" if len(moves) else "—", border=True)
    m[3].metric("Analysed", f"{uniq['Analysed'].notna().sum()} / {len(uniq)}", border=True,
                help="Stocks with at least one full analysis saved.")

    o1, o2, o3 = st.columns([1, 1, 1.2] if positions else [1, 1, 1])
    with o1:
        st.markdown("**:material/trending_up: Movers today**")
        for _, r in moves.sort_values("Day %", ascending=False).head(3).iterrows():
            st.markdown(f"{r['ticker']} &nbsp; {signed(r['Day %'], '{:+.2f}%')}")
        for _, r in moves.sort_values("Day %").head(3 if len(moves) > 3 else 0).iterrows():
            st.markdown(f"{r['ticker']} &nbsp; {signed(r['Day %'], '{:+.2f}%')}")
    with o2:
        st.markdown("**:material/psychology: AI verdicts**")
        counts = uniq["Verdict"].value_counts()
        with st.container(horizontal=True):
            for verdict, n in counts.items():
                color, icon = VERDICT_BADGE.get(verdict, ("gray", ":material/hourglass_top:"))
                st.badge(f"{verdict if verdict != '—' else 'Pending'} · {n}", icon=icon, color=color)
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
section = st.segmented_control(
    "Section", list(SECTIONS), default="Holdings", required=True, key="pf_section",
    label_visibility="collapsed",
    format_func=lambda s: f"{SECTIONS[s]} {s}" + (f" · {n_unread}" if s == "Alerts" and n_unread else ""))

if section == "Performance":
    qty = df.groupby("ticker")["qty"].sum(min_count=1).fillna(0) if positions else pd.Series(0, index=uniq["ticker"])
    perf = pf.performance(tuple(sorted((t, float(q)) for t, q in qty.items())), pf.day_slot())
    if perf is None or perf.empty:
        st.caption("Price history is unavailable right now — try again shortly.")
        st.stop()
    cols = st.columns(5)
    for col, (label, days) in zip(cols, (("1 week", 5), ("1 month", 21), ("3 months", 63), ("6 months", 126),
                                         ("1 year", len(perf) - 1))):
        if len(perf) > days:
            p = (perf["Your holdings"].iloc[-1] / perf["Your holdings"].iloc[-1 - days] - 1) * 100
            n = (perf["Nifty 50"].iloc[-1] / perf["Nifty 50"].iloc[-1 - days] - 1) * 100
            col.metric(label, f"{p:+.1f}%", f"{p - n:+.1f} pts vs Nifty", border=True)
    st.line_chart(perf, color=["#1F4E79", "#C08A3E"], height=360, y_label="Rebased to 100")
    st.caption("How the stocks you hold **today** have performed over the past year"
               + (", weighted by your quantities" if positions and qty.sum() else ", equally weighted")
               + ", against the Nifty 50. Buys and sells in between are not modelled.")
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
        st.warning(f"**{r['ticker']}** is {r['Weight']:.1f}% of the portfolio — above your "
                   f"{limits['max_stock_pct']}% single-stock limit.", icon=":material/warning:")
    for name, w in crowded.items():
        st.warning(f"**{name}** is {w:.1f}% of the portfolio — above your {limits['max_sector_pct']}% sector limit.",
                   icon=":material/warning:")
    if heavy.empty and crowded.empty:
        st.success(f"Nothing above your limits ({limits['max_stock_pct']}% per stock, "
                   f"{limits['max_sector_pct']}% per sector — change them in Settings).", icon=":material/verified:")
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
        st.bar_chart(sizes, horizontal=True, color="#1F4E79", height=170)
        st.markdown("**By sector**")
        st.dataframe(sectors.rename("Weight %").round(1), height=210)
    st.caption(("Weighted by current value" if weights is not None else "Equally weighted (no quantities)")
               + ". Box colour is today's move. Sector comes from screener.in; size is approximate "
                 "(large ≥ ₹1 lakh Cr, mid ≥ ₹30,000 Cr). Stocks not yet analysed show as Unknown.")
    st.stop()

if section == "Alerts":
    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown(f"**Inbox** :gray[· {n_unread} unread]", width="content")
        st.space("stretch")
        if n_unread and st.button("Mark all read", icon=":material/done_all:", type="tertiary"):
            alerts.mark_all_read()
            st.rerun()
    inbox = alerts.events(60)
    if not inbox:
        st.caption("No alerts yet. You'll see verdict changes, new filings, missed guidance and price alerts here "
                   "— and get them as notifications or email, whichever you pick in Settings → Notifications.")
    icons = {"verdict": ":material/psychology:", "filing": ":material/description:",
             "guidance": ":material/handshake:", "price": ":material/price_change:"}
    with st.container(border=bool(inbox)):
        for e in inbox:
            dot = ":blue[●] " if not e["read"] else ""
            st.markdown(f"{dot}{icons.get(e['kind'], '')} **{e['title']}** :gray[· "
                        f"{datetime.fromtimestamp(e['ts']).strftime('%d %b, %H:%M')}]  \n:gray[{e['body']}]")
    st.markdown("**Price alerts**")
    rules = alerts.rules()
    for r in rules:
        c1, c2 = st.columns([6, 1], vertical_alignment="center")
        c1.markdown(f"**{r['ticker']}** {r['op']} ₹{r['level']:,.2f}"
                    + (" :orange-badge[triggered]" if r["fired"] else " :gray-badge[watching]"))
        if c2.button("", icon=":material/delete:", key=f"rule_{r['id']}", type="tertiary"):
            alerts.delete_rule(r["id"])
            st.rerun()
    with st.form("new_rule", border=False):
        a1, a2, a3, a4 = st.columns([2, 1, 1, 1], vertical_alignment="bottom")
        t = a1.selectbox("Stock", sorted(uniq["ticker"]))
        op = a2.selectbox("When price is", ["above", "below"])
        level = a3.number_input("₹", min_value=0.0, value=float(prices.get(t, {}).get("price") or 0), step=1.0)
        if a4.form_submit_button("Add alert", icon=":material/add_alert:"):
            alerts.add_rule(t, op, level)
            st.rerun()
    st.stop()

if section == "Digest":
    past = alerts.digests()
    n = alerts.prefs(settings)
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(f"A digest is made every {alerts.DAYS[int(n['digest_day'])]} morning"
                   + (" and sent by " + " and ".join({'mac': 'notification', 'email': 'email'}[c] for c in n["channels"])
                      if n["channels"] else "") + ". Change this in Settings → Notifications.")
        st.space("stretch")
        if st.button("Make one now", icon=":material/refresh:"):
            with st.spinner("Putting this week together…"):
                alerts.make_digest(settings, send=False)
            st.rerun()
    if not past:
        st.caption("No digest yet — make one now, or wait for the weekly one.")
        st.stop()
    pick = st.selectbox("Week", past, format_func=lambda d: f"{d['week']} · made "
                        f"{datetime.fromtimestamp(d['ts']).strftime('%a %d %b, %H:%M')}", label_visibility="collapsed")
    st.html(alerts.digest_html(pick["data"]))
    if "email" in n["channels"] and st.button("Email this digest to me", icon=":material/mail:"):
        problems = alerts.notify(f"Your weekly digest · {pick['week']}", "Your weekly digest is ready.",
                                 {**settings, "notify": {**n, "channels": ["email"]}}, alerts.digest_html(pick["data"]))
        st.toast(problems[0] if problems else "Sent.", icon=":material/error:" if problems else ":material/mail:")
    st.stop()

# --- Holdings grid -------------------------------------------------------------
with st.container(horizontal=True, vertical_alignment="center"):
    st.markdown("#### :material/view_module: Holdings", width="content")
    layout = st.segmented_control("View", [":material/grid_view: Grid", ":material/table_rows: Table"],
                                  default=":material/grid_view: Grid", required=True, key="pf_layout",
                                  label_visibility="collapsed")
    sorts = (["Value", "P&L %"] if positions else []) + ["Day %", "Name"]
    order = st.segmented_control("Sort by", sorts, default=sorts[0], required=True, key="pf_sort",
                                 label_visibility="collapsed")
    st.space("stretch")
    if st.button("Re-analyse all", icon=":material/autorenew:", disabled=bool(pf._forced),
                 help="Queues every held stock for a fresh full analysis. It runs in the background, one "
                      "stock at a time; anything unchanged since the last run (filings, parsed numbers, AI "
                      "answers) is reused instead of redone."):
        st.toast(f"Queued {pf.reanalyse_all()} stock(s) for re-analysis.", icon=":material/autorenew:")
key_col = {"Value": "Value", "P&L %": "P&L %", "Day %": "Day %", "Name": "ticker"}[order]
cards = df.sort_values(key_col, ascending=order == "Name", na_position="last")

if layout.endswith("Table"):
    cols = ["ticker", "name", "account", "qty", "avg_price", "Price", "Day %", "Value", "P&L", "P&L %",
            "Verdict", "Analysed"]
    if not positions:
        cols = [c for c in cols if c not in ("qty", "avg_price", "Value", "P&L", "P&L %")]
    table = cards[cols].rename(columns={"ticker": "Ticker", "name": "Company", "account": "Account",
                                        "qty": "Qty", "avg_price": "Avg price"})
    tone = lambda v: "" if pd.isna(v) else f"color: {GOOD if v >= 0 else BAD}; font-weight: 600"
    verdict_tone = lambda v: {"green": f"color: {GOOD}", "red": f"color: {BAD}",
                              "orange": "color: #C08A3E"}.get(VERDICT_BADGE.get(v, ("",))[0], "")
    styled = (table.style.map(tone, subset=[c for c in ("Day %", "P&L", "P&L %") if c in table])
              .map(verdict_tone, subset=["Verdict"])
              .format({"Qty": "{:,.0f}", "Avg price": "₹{:,.2f}", "Price": "₹{:,.2f}", "Day %": "{:+.2f}%",
                       "Value": "₹{:,.0f}", "P&L": "₹{:+,.0f}", "P&L %": "{:+.2f}%"}, na_rep="—"))
    picked = st.dataframe(styled, hide_index=True, on_select="rerun", selection_mode="single-row",
                          column_config={"Analysed": st.column_config.DatetimeColumn(format="distance")},
                          key="pf_table")
    st.caption("Click a row to open that stock's page.")
    if picked.selection.rows:
        st.session_state.stock = table.iloc[picked.selection.rows[0]]["Ticker"]
        st.switch_page("app_pages/stock.py")

PER_ROW = 3
for start in range(0, len(cards) if layout.endswith("Grid") else 0, PER_ROW):
    for col, (_, h) in zip(st.columns(PER_ROW), cards.iloc[start:start + PER_ROW].iterrows()):
        q = prices.get(h["ticker"], {})
        with col.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"### {h['ticker']}", width="content")
                if h["Verdict"] != "—":
                    color, icon = VERDICT_BADGE.get(h["Verdict"], ("gray", None))
                    st.badge(h["Verdict"], icon=icon, color=color)
            st.caption(h["name"] + (f" · {h['account']}" if view == "All accounts" else ""))
            st.metric("Price", f"₹{q['price']:,.2f}" if q else "—",
                      f"{q['change']:+,.2f} ({q['change_pct']:+.2f}%)" if q else None,
                      chart_data=q.get("spark"), chart_type="area", label_visibility="collapsed")
            if positions and pd.notna(h["qty"]):
                a, b, c = st.columns(3)
                a.markdown(f":gray[Qty]  \n**{h['qty']:,.0f}**")
                b.markdown(f":gray[Value]  \n**{inr(h['Value'])}**" if pd.notna(h["Value"]) else ":gray[Value]  \n—")
                c.markdown(f":gray[P&L]  \n**{signed(h['P&L'], '₹{:+,.0f}')}**  \n"
                           f"{signed(h['P&L %'], '{:+.1f}%')}" if pd.notna(h["P&L"]) else ":gray[P&L]  \n—")
            with st.container(horizontal=True, vertical_alignment="center"):
                st.caption(ago(h["Analysed"]))
                if st.button("Open", key=f"open_{h['account_id']}_{h['ticker']}", icon=":material/arrow_forward:",
                             type="tertiary"):
                    st.session_state.stock = h["ticker"]
                    st.switch_page("app_pages/stock.py")

if view in names:
    with st.expander("Edit or remove a holding" if positions else "Remove a stock", icon=":material/edit_note:"):
        h = st.selectbox("Holding", rows, format_func=lambda r: f"{r['ticker']} · {r['name']}")
        q2, a2 = h["qty"], h["avg_price"]
        if positions:
            e1, e2 = st.columns(2)
            q2 = e1.number_input("Quantity", min_value=0.0, value=q2, placeholder="Optional", key=f"q_{h['ticker']}")
            a2 = e2.number_input("Avg. buy price", min_value=0.0, value=a2, placeholder="Optional",
                                 key=f"a_{h['ticker']}")
        b1, b2 = st.columns(2)
        if positions and b1.button("Save", type="primary", width="stretch"):
            pf.save_holding(view, h["ticker"], h["name"], h["screener_id"], q2, a2)
            st.rerun()
        if b2.button("Remove", icon=":material/delete:", width="stretch"):
            pf.remove_holding(view, h["ticker"])
            st.rerun()
