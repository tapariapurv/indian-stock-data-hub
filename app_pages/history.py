"""Past analyses: reopen any saved run and see how a company has moved."""

from datetime import datetime

import pandas as pd
import streamlit as st

import archive
import core
import ui

settings = st.session_state.settings
storage = settings["storage"]

st.caption(":material/history: SAVED ANALYSES")
st.title("History")
st.markdown(":gray[Every analysis is saved locally so you can reopen it later, compare it with the "
            "one before, and watch a ratio move over months.]")

if not settings["features"]["history"]:
    st.info("Saving analyses is turned off on the Settings page.", icon=":material/info:")
    st.stop()

runs = archive.list_runs(limit=500)
if not runs:
    st.info("No analyses saved yet. Run one on the Research page and it will appear here.",
            icon=":material/info:")
    st.stop()

tickers = sorted({r["ticker"] for r in runs})
c1, c2 = st.columns([2, 3])
chosen = c1.selectbox("Company", tickers, key="hist_ticker")
mine = [r for r in runs if r["ticker"] == chosen]
labels = {r["id"]: datetime.fromtimestamp(r["ts"]).strftime("%d %b %Y, %H:%M") for r in mine}
run_id = c2.selectbox("Saved analysis", list(labels), format_func=labels.get, key="hist_run")

kept = "kept forever" if not storage["keep_runs_days"] else f"kept for {storage['keep_runs_days']} days"
st.caption(f"{len(mine)} saved analysis/analyses for {chosen} · {len(runs)} in total · {kept} "
           "(change that on the Settings page).")

run = archive.get_run(run_id)
if not run:
    st.stop()
result = core.restore_snapshot(run["snapshot"])

# --- How this company has moved over the saved runs ---------------------------
history = archive.run_history(chosen, limit=60)
tracked = []
for h in history:
    row = {"When": datetime.fromtimestamp(h["ts"])}
    for name, value in (h["snapshot"].get("metrics") or {}).items():
        number = core._to_number(str(value).replace("₹", "").replace("Cr.", "").replace("%", ""))
        if number is not None:
            row[name] = number
    row["Verdict"] = h["snapshot"].get("ai_verdict")
    tracked.append(row)
trend = pd.DataFrame(tracked).set_index("When") if tracked else pd.DataFrame()

if len(trend) > 1:
    numeric = [c for c in trend.columns if c != "Verdict" and trend[c].notna().sum() > 1]
    if numeric:
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="bottom"):
                st.markdown(f"**{chosen} over time** :gray[· from {len(trend)} saved analyses]",
                            width="stretch")
                metric = st.selectbox("Metric", numeric, label_visibility="collapsed",
                                      key="hist_metric")
            st.line_chart(trend[[metric]].dropna(), y=metric, x_label="", y_label="",
                          color=settings["ui"]["chart_colors"][0], height=260)

# --- What changed between this run and the one before it ----------------------
index = [r["id"] for r in mine].index(run_id)
older = mine[index + 1] if index + 1 < len(mine) else None
if older:
    previous = archive.get_run(older["id"])
    changes = archive.diff_snapshots(previous["snapshot"], run["snapshot"])
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"**Changes since {labels[older['id']]}**", width="stretch")
            st.badge(f"{len(changes)} change(s)", color="orange" if changes else "gray",
                     icon=":material/change_circle:")
        ui.changes_table(changes)

# --- The saved analysis itself ------------------------------------------------
st.markdown(f"### As saved on {labels[run_id]}")
ui.company_card(result, show_news=settings["features"]["news"])

sources = run["snapshot"].get("sources") or []
if not sources and result.get("downloaded"):
    sources = core._sources_of(result)  # an analysis saved before paths were recorded
with st.container(border=True):
    st.markdown("**:material/description: Where this came from**")
    ui.sources_table(sources, key=f"hist_src_{run_id}")

correlated = result.get("correlated")
if correlated is not None and not correlated.empty:
    with st.expander(f"All numbers as they stood then · {len(correlated)} metrics",
                     icon=":material/table_rows:"):
        ui.correlated_table(correlated, settings, key=f"hist_corr_{run_id}")
        st.caption(f"{result.get('figure_count', 0):,} individual figures were extracted from the "
                   "filings in this run; the filings themselves are still on the Archive page.")

qdf = result.get("quarterly_df")
if qdf is not None and not qdf.empty:
    with st.expander("Quarterly results as saved", icon=":material/bar_chart:"):
        st.dataframe(ui.as_text(qdf), hide_index=True,
                     column_config={"Metric": st.column_config.TextColumn(pinned=True)})

with st.container(horizontal=True, horizontal_alignment="right"):
    if correlated is not None and not correlated.empty:
        st.download_button("Download this analysis (CSV)",
                           data=correlated.to_csv(index=False).encode("utf-8"),
                           file_name=f"{chosen}_{labels[run_id].replace(', ', '_').replace(' ', '')}.csv",
                           mime="text/csv", icon=":material/download:", type="tertiary")
