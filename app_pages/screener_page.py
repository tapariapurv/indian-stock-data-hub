"""Screener: filter every NSE-listed company with a screener.in-style query."""

import streamlit as st

import portfolio as pf
import screener as sc
import ui

st.title("Screener")
st.caption("Search every company listed on the NSE. Pick a ready-made search on the right, or write your own.")
include_sme = st.checkbox("Include SMEs", key="screen_sme",
                          help="Small and medium companies listed on NSE Emerge. Smaller, riskier and less traded.")
with st.spinner("Loading every listed company — a few seconds, once a day…"):
    df = sc.everything(sc.version(), pf.day_slot(), include_sme)
if df.empty:
    st.info("Couldn't load the list of companies. Check your internet connection, then try again.",
            icon=":material/wifi_off:")
    st.stop()

ss = st.session_state
ss.setdefault("screen_query", sc.PRESETS["Cheap and profitable"])
saved = sc.saved()

left, right = st.columns([3, 1.2], gap="large")
with right:
    st.markdown("**Ready-made searches**")
    preset = st.pills("Presets", list(sc.PRESETS) + list(saved), label_visibility="collapsed", key="screen_preset")
    if preset and ss.get("_last_preset") != preset:
        ss.screen_query = sc.PRESETS.get(preset) or saved.get(preset, "")
        ss._last_preset = preset
    with st.expander("Words you can search by", icon=":material/list:"):
        st.caption("Marked ✓ are known for every listed company; the rest only for companies the app has looked into.")
        st.markdown("\n".join(f"- **{f}**" + (" ✓" if f in sc.MARKET_FIELDS else "")
                              + (f" :gray[({sc.UNITS[f]})]" if f in sc.UNITS else "")
                              + f"  \n  :gray[also: {', '.join(n for n in names[:2])}]" for f, names in sc.FIELDS.items()))
with left:
    query = st.text_area("Search", key="screen_query", height=110, label_visibility="collapsed",
                         placeholder="Market Capitalization > 5000 AND Return on equity > 15")
    st.caption("Write what you want, like **ROE > 15**, and join ideas with **AND** / **OR**. "
               "Words work too, e.g. **Sector = Banks**.")
    with st.container(horizontal=True):
        st.button("Search", type="primary", icon=":material/search:")
        with st.popover("Save this search", icon=":material/bookmark_add:"):
            name = st.text_input("Name", placeholder="e.g. Cheap compounders")
            if st.button("Save", disabled=not name.strip()):
                sc.save(name, query)
                st.rerun()
        if preset in saved and st.button("Delete saved", icon=":material/delete:", type="tertiary"):
            sc.delete(preset)
            st.rerun()

try:
    groups = sc.parse(query)
except ValueError as exc:
    st.error(f"The app didn't understand that search. {exc}", icon=":material/error:")
    st.stop()

found = sc.run(df, groups)
shown = ["Ticker", "Company", *[f for f in sc.fields_used(groups) if f not in ("Ticker", "Company")]]
for extra in ("Current Price", "Market Cap", "P/E", "Price to Book", "Dividend Yield", "1Y Return", "Verdict"):
    if extra not in shown and len(shown) < 9:
        shown.append(extra)
st.markdown(f"**{len(found):,} match{'es' if len(found) != 1 else ''}** :gray[· out of {len(df):,} listed companies]")
own = [f for f in sc.fields_used(groups) if f not in sc.MARKET_FIELDS]
if own:
    st.caption(f":material/info: {', '.join(own)} {'is' if len(own) == 1 else 'are'} known only for the "
               f"{int(df['Analysed'].notna().sum()) if 'Analysed' in df else 0} companies the app has looked into, "
               "so only those can match. Open any company and press **Look into this company** to add it.")
if include_sme and "SME" not in shown:
    shown.append("SME")
# The unit goes in the header and the digits come from the Styler below, which shows an empty cell
# as "—" (a column format would print the word "None").
fmt = {f: st.column_config.NumberColumn(f + (f" ({sc.UNITS[f]})" if f in sc.UNITS else ""))
       for f in shown if f not in ("Ticker", "Company", "Sector", "Verdict", "SME")}
table = found[shown].sort_values(shown[2] if len(shown) > 2 else "Ticker", ascending=False)
numbers = [f for f in fmt if f in table]
styled = table.style.format({f: "{:,.0f}" if f == "Market Cap" else "{:,.2f}" if f == "Current Price" else "{:,.1f}"
                             for f in numbers}, na_rep="—")
if "Verdict" in table:
    table = table.assign(Verdict=table["Verdict"].map(lambda v: ui.VIEW_WORDS.get(v, v)))
fmt["Ticker"] = st.column_config.TextColumn("Symbol")
fmt["Verdict"] = st.column_config.TextColumn("AI view")
fmt["SME"] = st.column_config.CheckboxColumn("SME", width="small")
picked = st.dataframe(styled, hide_index=True, column_config=fmt, height=min(38 * (len(found) + 1) + 4, 600),
                      on_select="rerun", selection_mode="single-row", key="screen_table")
if picked.selection.rows:
    row = table.iloc[picked.selection.rows[0]]
    ui.open_company({"ticker": row["Ticker"], "name": row["Company"],
                     "screener_id": (pf.tracked().get(row["Ticker"]) or {}).get("screener_id")})
st.caption("Prices and valuations are from Yahoo Finance, refreshed daily; the rest from the last time the app "
           "checked each company. Growth compares the latest quarter with the same quarter a year earlier. "
           "Tick the box beside a row to open the company.")
