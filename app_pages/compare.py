"""Compare 2–4 companies side by side."""

import pandas as pd
import streamlit as st

import portfolio as pf
import screener as sc
from ui import VERDICT_BADGE

st.markdown("### :material/compare_arrows: Compare")
df = sc.universe(sc.version())
if len(df) < 2:
    st.info("Analyse at least two companies to compare them.", icon=":material/info:")
    st.stop()

names = dict(zip(df["Ticker"], df["Company"]))
held = [t for t in pf.tracked() if t in names]
picked = st.multiselect("Companies", sorted(names), default=held[:3] if len(held) >= 2 else list(names)[:2],
                        max_selections=4, format_func=lambda t: f"{t} · {names[t]}", label_visibility="collapsed",
                        placeholder="Pick two to four companies")
if len(picked) < 2:
    st.caption("Pick at least two.")
    st.stop()

rows = df.set_index("Ticker").loc[picked]
# Higher is better for these, lower for these; used only to highlight the leader.
BETTER_HIGH = ["ROCE", "ROE", "Sales Growth", "Profit Growth", "OPM", "Dividend Yield", "Market Cap"]
BETTER_LOW = ["P/E", "Price to Book"]
metrics = ["Market Cap", "Current Price", "P/E", "Price to Book", "ROCE", "ROE", "OPM", "Sales Growth",
           "Profit Growth", "Dividend Yield"]
cols = st.columns(len(picked))
for col, t in zip(cols, picked):
    with col.container(border=True):
        v = rows.loc[t, "Verdict"]
        st.markdown(f"**{t}**")
        st.caption(f"{names[t]} · {rows.loc[t, 'Sector'] or 'sector n/a'}")
        if v:
            color, icon = VERDICT_BADGE.get(v, ("gray", None))
            st.badge(v, icon=icon, color=color)

table = rows[metrics].T
table.index = [f"{m} ({sc.UNITS[m]})" if m in sc.UNITS else m for m in metrics]


def lead(row):
    name = row.name.split(" (")[0]
    vals = pd.to_numeric(row, errors="coerce")
    if vals.notna().sum() < 2 or name not in BETTER_HIGH + BETTER_LOW:
        return [""] * len(row)
    best = vals.idxmax() if name in BETTER_HIGH else vals.idxmin()
    return ["background-color: #E5F2EA; font-weight: 600" if c == best else "" for c in row.index]


st.dataframe(table.style.apply(lead, axis=1).format("{:,.1f}", na_rep="—"), width="stretch")
st.caption("Green marks the leader on each line (lowest for valuation multiples, highest otherwise). "
           "Figures come from each company's latest saved analysis.")

st.markdown("**Share price, past year** :gray[· rebased to 100]")
held_ids = pf.tracked()
series = {}
for t in picked:
    bars = pf.history(t, (held_ids.get(t) or {}).get("screener_id"), "1Y")
    if bars is not None and not bars.empty:
        series[t] = bars["Close"] / bars["Close"].iloc[0] * 100
if series:
    st.line_chart(pd.DataFrame(series).ffill(), height=320)
