"""Home: look up any company, see how your stocks are doing, and what needs a look."""

from datetime import datetime

import pandas as pd
import streamlit as st

import alerts
import core
import portfolio as pf
import ui

settings = st.session_state.settings
hour = datetime.now().hour
st.title("Good " + ("morning" if hour < 12 else "afternoon" if hour < 17 else "evening"))
st.caption(datetime.now().strftime("%A, %d %B"))

ui.company_search("home_search")

rows = pf.holdings()
if not rows:
    # First visit: three steps, nothing else competing for attention.
    with st.container(border=True):
        st.markdown("#### Get started in three steps")
        st.markdown("1. **Look up a company** in the box above — its page shows the price, a plain-English "
                    "summary, and anything worrying in its reports.\n"
                    "2. **Add it to your portfolio** from that page, and the app keeps an eye on it for you.\n"
                    "3. **Come back here** to see how your stocks are doing and what needs your attention.")
        st.link_button("Read the short user guide", core.GUIDE_URL, icon=":material/menu_book:", type="tertiary")
    if not core.ai_signature(settings)[0]:
        with st.container(border=True):
            st.markdown("#### :material/auto_awesome: Turn on AI summaries (free)")
            st.markdown("The app can write a short summary of each company and answer your questions. "
                        "It works without this, but it's better with it.")
            st.page_link("app_pages/settings_page.py", label="Set it up in Settings", icon=":material/arrow_forward:")
    st.stop()

# --- Your money ------------------------------------------------------------------
stocks = tuple(sorted({(h["ticker"], h["screener_id"]) for h in rows}))
prices = pf.quotes(stocks)
runs = pf.latest_runs(t for t, _ in stocks)
value = invested = day = 0.0
for h in rows:
    q = prices.get(h["ticker"]) or {}
    if h["qty"] and q.get("price"):
        value += h["qty"] * q["price"]
        day += h["qty"] * (q.get("change") or 0)
        if h["avg_price"]:
            invested += h["qty"] * h["avg_price"]

with st.container(horizontal=True):
    if value:
        st.metric("Worth today", ui.inr(value), f"{ui.inr(day, signed=True)} today", border=True)
        if invested:
            st.metric("Gain or loss so far", ui.inr(value - invested, signed=True),
                      f"{(value / invested - 1) * 100:+.1f}%", border=True)
    up = sum((prices.get(t) or {}).get("change_pct", 0) > 0 for t, _ in stocks)
    st.metric("Your stocks", len(stocks), f"{up} up today", delta_color="off", border=True)

# --- Needs your attention ----------------------------------------------------------
week_ago = datetime.now().timestamp() - 7 * 86400
notes, seen = [], set()
for e in alerts.events(limit=30, since=week_ago):  # the newest message per stock is enough here
    if e["ticker"] not in seen:
        seen.add(e["ticker"])
        notes.append((e["ticker"], e["kind"], ui.plain_title(e["title"].split(": ", 1)[-1]), e["body"]))
for t, _ in stocks:
    for f in (ui.red_flags_of(t) or [])[:1]:
        if f["severity"] == "high":
            notes.append((t, "flag", f"warning sign in its annual report: {f['kind'].lower()}", f["text"]))
KIND_ICON = {"verdict": ":material/psychology:", "filing": ":material/description:", "flag": ":material/flag:",
             "guidance": ":material/handshake:", "price": ":material/price_change:"}
if notes:
    with st.container(border=True):
        st.markdown("**:material/priority_high: Needs your attention**")
        for i, (ticker, kind, title, body) in enumerate(notes[:5]):
            with st.container(horizontal=True, vertical_alignment="center"):
                # One short line each; the detail waits in the tooltip and on the company's page.
                st.markdown(f"{KIND_ICON.get(kind, '')} **{ticker}** :gray[·] {title}", width="stretch",
                            help=body[:400])
                held = pf.tracked().get(ticker)
                if held and st.button("Open", key=f"att_{i}", type="tertiary", icon=":material/arrow_forward:"):
                    ui.open_company({"ticker": ticker, **held})
        if len(notes) > 5:
            st.caption(f"and {len(notes) - 5} more under **Portfolio → Alerts**.")

# --- Your stocks -----------------------------------------------------------------
held = pf.tracked()
table = pd.DataFrame([{
    "Company": held[t]["name"], "Symbol": t,
    "Price": (prices.get(t) or {}).get("price"), "Today": (prices.get(t) or {}).get("change_pct"),
    "Trend": (prices.get(t) or {}).get("spark"),
    "AI view": ui.VIEW_WORDS.get((runs.get(t) or {}).get("verdict"), "Being checked"),
} for t in sorted(held)])
st.markdown("**Your stocks** :gray[· tick the box beside a stock to open it]")
TONE = {"Looks good": "color: #15803D", "Be careful": "color: #DC2626", "Mixed picture": "color: #D97706"}
styled = (table.style.map(lambda v: "" if pd.isna(v) else f"color: {'#15803D' if v >= 0 else '#DC2626'}",
                          subset=["Today"])
          .map(lambda v: TONE.get(v, "color: #6B7280"), subset=["AI view"]))
picked = st.dataframe(
    styled, hide_index=True, on_select="rerun", selection_mode="single-row", key="home_stocks",
    height=min(36 * (len(table) + 1) + 4, 520),
    column_config={"Company": st.column_config.TextColumn(width="large"),
                   "Price": st.column_config.NumberColumn(format="₹%.2f"),
                   "Today": st.column_config.NumberColumn(format="%+.2f%%"),
                   "Trend": st.column_config.LineChartColumn("Last month", width="small")})
if picked.selection.rows:
    t = table.iloc[picked.selection.rows[0]]["Symbol"]
    ui.open_company({"ticker": t, **held[t]})
